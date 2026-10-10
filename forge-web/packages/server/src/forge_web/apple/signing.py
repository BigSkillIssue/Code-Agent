"""Signing for the App Store (W22b): identifiers, certificates and profiles through Apple's
Provisioning API, all on the server.

A certificate's private key is made here (RSA 2048, as Apple asks), kept encrypted with the
vault for its user and team, and leaves the server only inside the one-time signing material of
an export job: a .p12 with a fresh password. Profiles are made anew for each release.
"""

import base64
import json
import secrets
import time
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from forge_macworker.wire import ExportParams, Profile, SigningMaterial
from forge_web.apple.asc_client import AscClient, AscError
from forge_web.db.engine import Database
from forge_web.db.models import AppleCertificate
from forge_web.vault import Vault

DISTRIBUTION, INSTALLER = "DISTRIBUTION", "MAC_INSTALLER_DISTRIBUTION"
PROFILE_TYPES = {"ios": "IOS_APP_STORE", "macos": "MAC_APP_STORE"}
BUNDLE_PLATFORMS = {"ios": "IOS", "macos": "MAC_OS"}
RENEW_BEFORE_S = 7 * 86_400  # a certificate this close to its end is replaced
TOO_MANY = (
    "Apple allows only a few distribution certificates per team: revoke one you no longer use "
    "(developer.apple.com → Certificates) and start the release again"
)


class SigningProblem(Exception):
    """Signing cannot be set up; with a hint for the user."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Certificate:
    """A certificate with its private key."""

    asc_id: str
    key_pem: str
    der: bytes


@dataclass(frozen=True)
class Owner:
    """Whose certificates: a user of this server and the team of their key."""

    db: Database
    vault: Vault
    user_id: str
    team_id: str


async def registered(asc: AscClient, identifier: str, platform: str) -> str:
    """Apple's id of a bundle identifier, registered now if it is new."""
    if found := await bundle_id(asc, identifier):
        return found
    name = "Forge " + identifier.replace(".", " ").replace("-", " ")  # Apple allows no dots
    attributes = {"identifier": identifier, "name": name[:60],
                  "platform": BUNDLE_PLATFORMS[platform]}  # fmt: skip
    try:
        made = await asc.post("/v1/bundleIds", {"data": {"type": "bundleIds",
                                                         "attributes": attributes}})  # fmt: skip
    except AscError as err:  # registered elsewhere a moment ago (Xcode, say)
        if err.status == 409 and (found := await bundle_id(asc, identifier)):
            return found
        raise
    return str(made["data"]["id"])


async def bundle_id(asc: AscClient, identifier: str) -> str:
    """Apple's id of a registered identifier ("" when it is not registered)."""
    found = await asc.get("/v1/bundleIds", **{"filter[identifier]": identifier, "limit": 200})
    for item in found.get("data", []):
        if item.get("attributes", {}).get("identifier") == identifier:  # the filter is not exact
            return str(item["id"])
    return ""


async def certificate(owner: Owner, asc: AscClient, kind: str) -> Certificate:
    """The team's certificate of this kind that Forge made; a new one if there is none, it
    runs out soon, or Apple no longer knows it (revoked)."""
    async with owner.db.session() as session:
        rows = await session.scalars(
            select(AppleCertificate).where(
                AppleCertificate.user_id == owner.user_id,
                AppleCertificate.team_id == owner.team_id, AppleCertificate.kind == kind,
            ).order_by(AppleCertificate.created_at.desc())
        )  # fmt: skip
        stored = list(rows)
    for row in stored:
        if row.expires_at - time.time() > RENEW_BEFORE_S and await still_valid(asc, row.id):
            secret = json.loads(owner.vault.decrypt(row.secret))
            return Certificate(row.id, secret["key"], base64.b64decode(secret["der"]))
        await forget(owner.db, row.id)
    return await new_certificate(owner, asc, kind)


async def still_valid(asc: AscClient, asc_id: str) -> bool:
    """Whether Apple still has the certificate (a revoked one is gone)."""
    try:
        await asc.get(f"/v1/certificates/{asc_id}")
    except AscError as err:
        if err.status == 404:
            return False
        raise
    return True


async def forget(db: Database, asc_id: str) -> None:
    async with db.session() as session, session.begin():
        row = await session.get(AppleCertificate, asc_id)
        if row is not None:
            await session.delete(row)


async def new_certificate(owner: Owner, asc: AscClient, kind: str) -> Certificate:
    """A key made here, a certificate for it from Apple, both kept encrypted."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Forge Web")]))
           .sign(key, hashes.SHA256()))  # fmt: skip
    body = {
        "data": {
            "type": "certificates",
            "attributes": {
                "certificateType": kind,
                "csrContent": csr.public_bytes(serialization.Encoding.PEM).decode(),
            },
        }
    }
    try:
        made = await asc.post("/v1/certificates", body)  # fmt: skip
    except AscError as err:
        if err.status == 409 and "certificate" in str(err).lower():
            raise SigningProblem(str(err), TOO_MANY) from None
        raise
    der = base64.b64decode(made["data"]["attributes"]["certificateContent"])
    expires = x509.load_der_x509_certificate(der).not_valid_after_utc.timestamp()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()  # fmt: skip
    secret = owner.vault.encrypt(json.dumps({"key": pem, "der": base64.b64encode(der).decode()}))
    async with owner.db.session() as session, session.begin():
        session.add(AppleCertificate(id=str(made["data"]["id"]), user_id=owner.user_id,
                                     team_id=owner.team_id, kind=kind, secret=secret,
                                     expires_at=expires, created_at=time.time()))  # fmt: skip
    return Certificate(str(made["data"]["id"]), pem, der)


async def profile(
    asc: AscClient, platform: str, identifier: str, bundle: str, cert: Certificate
) -> Profile:
    """A fresh App Store profile for one bundle (an older one of Forge's is removed first)."""
    name = f"Forge {identifier} {platform}"
    found = await asc.get("/v1/profiles", **{"filter[name]": name})
    for old in found.get("data", []):
        if old.get("attributes", {}).get("name") == name:
            await asc.delete(f"/v1/profiles/{old['id']}")
    body = {
        "data": {
            "type": "profiles",
            "attributes": {"name": name, "profileType": PROFILE_TYPES[platform]},
            "relationships": {
                "bundleId": {"data": {"type": "bundleIds", "id": bundle}},
                "certificates": {"data": [{"type": "certificates", "id": cert.asc_id}]},
            },
        }
    }
    made = (await asc.post("/v1/profiles", body))["data"]["attributes"]  # fmt: skip
    return Profile(name=made["name"], uuid=made["uuid"], data_b64=made["profileContent"])


def p12(cert: Certificate, password: str) -> str:
    """The certificate and its key as a .p12 (base64) in the form macOS's `security` imports."""
    key = serialization.load_pem_private_key(cert.key_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise SigningProblem("a stored signing key is not an RSA key")
    legacy = (serialization.PrivateFormat.PKCS12.encryption_builder().kdf_rounds(50_000)
              .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
              .hmac_hash(hashes.SHA1()).build(password.encode()))  # fmt: skip
    data = pkcs12.serialize_key_and_certificates(
        b"Forge Web", key, x509.load_der_x509_certificate(cert.der), None, legacy
    )
    return base64.b64encode(data).decode()


async def signing_for(
    owner: Owner, asc: AscClient, platform: str, bundles: tuple[str, ...]
) -> tuple[ExportParams, SigningMaterial]:
    """Everything one export needs: identifiers registered, certificates, a profile for each
    bundle, and the .p12 files with a password made for this export only."""
    dist = await certificate(owner, asc, DISTRIBUTION)
    installer = await certificate(owner, asc, INSTALLER) if platform == "macos" else None
    profiles = []
    for identifier in bundles:
        bundle = await registered(asc, identifier, platform)
        profiles.append(await profile(asc, platform, identifier, bundle, dist))
    password = secrets.token_urlsafe(24)
    params = ExportParams(
        platform=platform,
        team_id=owner.team_id,
        profiles={i: p.name for i, p in zip(bundles, profiles, strict=True)},
    )
    material = SigningMaterial(
        p12_b64=p12(dist, password),
        password=password,
        profiles=profiles,
        installer_p12_b64=p12(installer, password) if installer else "",
    )
    return params, material  # fmt: skip
