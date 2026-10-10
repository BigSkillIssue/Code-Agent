"""The release half of the App Store Connect stand-in (W22b): identifiers, certificates and
profiles (the Provisioning API), build uploads, build processing and TestFlight groups.

Apple's rules that matter here are kept: certificates come from a CSR signed with a 2048-bit RSA
key and are limited per type, profile names are unique, build numbers only go up, upload parts
must arrive whole and match their checksum, and upload URLs get no credentials.
"""

import base64
import datetime
import hashlib
import plistlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request, Response

TEAM = "TEAM123456"
SUBJECTS = {"DISTRIBUTION": "Apple Distribution",
            "MAC_INSTALLER_DISTRIBUTION": "3rd Party Mac Developer Installer"}  # fmt: skip
MAX_CERTIFICATES = 2  # of each type, as for a real team
UTI = {"IOS": "com.apple.ipa", "MAC_OS": "com.apple.pkg"}


@dataclass
class ReleaseState:
    """What the release endpoints know."""

    base_url: str = ""
    certificates: dict[str, dict[str, Any]] = field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    uploads: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    builds: dict[str, dict[str, Any]] = field(default_factory=dict)
    beta_groups: list[dict[str, Any]] = field(default_factory=list)
    part_size: int = 5 * 1024 * 1024
    polls: int = 2  # answers "still processing" this often before it is done
    invalid_builds: bool = False  # processing finds a problem
    credentials_at_upload: int = 0  # requests to an upload URL that carried a token
    ca: tuple[rsa.RSAPrivateKey, x509.Certificate] | None = None


def resource(kind: str, rid: str, attributes: dict[str, Any], **relations: Any) -> dict[str, Any]:
    """One item the way the API returns it."""
    item: dict[str, Any] = {"type": kind, "id": rid, "attributes": attributes}
    if relations:
        item["relationships"] = {k: {"data": v} for k, v in relations.items()}
    return {"data": item}


def new_id() -> str:
    return uuid.uuid4().hex[:10].upper()


def issuer(state: ReleaseState) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """The stand-in's certificate authority (made once)."""
    if state.ca is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Stand-in WWDR")])
        now = datetime.datetime.now(datetime.UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(1).not_valid_before(now)
                .not_valid_after(now + datetime.timedelta(days=3650))
                .sign(key, hashes.SHA256()))  # fmt: skip
        state.ca = (key, cert)
    return state.ca


def certificate_from(state: ReleaseState, kind: str, csr_pem: str) -> x509.Certificate | str:
    """A certificate for a CSR, or why there is none."""
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
    except ValueError:
        return "The certificate signing request is not valid."
    public = csr.public_key()
    if not csr.is_signature_valid or not isinstance(public, rsa.RSAPublicKey):
        return "The CSR must be signed with an RSA key."
    if public.key_size != 2048:
        return "The key in the CSR must be a 2048-bit RSA key."
    ca_key, ca_cert = issuer(state)
    subject = x509.Name([
        x509.NameAttribute(NameOID.USER_ID, TEAM),
        x509.NameAttribute(NameOID.COMMON_NAME, f"{SUBJECTS[kind]}: Forge Test ({TEAM})"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, TEAM),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Forge Test"),
    ])  # fmt: skip
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_cert.subject)
            .public_key(public).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=365))
            .sign(ca_key, hashes.SHA256()))  # fmt: skip
    return cert


def provisioning_routes(app: FastAPI, state: ReleaseState, apps: Any, error: Any) -> None:
    """Certificates and profiles."""

    @app.get("/v1/certificates/{cid}")
    async def certificate(cid: str) -> Any:
        found = state.certificates.get(cid)
        if found is None or found["revoked"]:
            return error(404, "There is no resource of type 'certificates' with id " + cid)
        return resource("certificates", cid, found["attributes"])

    @app.post("/v1/certificates")
    async def new_certificate(request: Request) -> Any:
        attributes = (await request.json())["data"]["attributes"]
        kind = attributes.get("certificateType")
        if kind not in SUBJECTS:
            return error(409, f"certificateType {kind!r} is not supported here")
        current = [c for c in state.certificates.values()
                   if c["attributes"]["certificateType"] == kind and not c["revoked"]]  # fmt: skip
        if len(current) >= MAX_CERTIFICATES:
            return error(409, "You already have a current Distribution certificate or a "
                              "pending certificate request.")  # fmt: skip
        cert = certificate_from(state, kind, attributes.get("csrContent", ""))
        if isinstance(cert, str):
            return error(409, cert)
        cid = new_id()
        der = cert.public_bytes(serialization.Encoding.DER)
        state.certificates[cid] = {"revoked": False, "attributes": {
            "certificateType": kind, "name": SUBJECTS[kind], "displayName": "Forge Test",
            "certificateContent": base64.b64encode(der).decode(),
            "expirationDate": cert.not_valid_after_utc.isoformat(),
            "serialNumber": format(cert.serial_number, "X")}}  # fmt: skip
        return resource("certificates", cid, state.certificates[cid]["attributes"])

    @app.get("/v1/profiles")
    async def profiles(request: Request) -> Any:
        name = request.query_params.get("filter[name]")
        found = [
            resource("profiles", pid, p["attributes"])["data"]
            for pid, p in state.profiles.items()
            if name in (None, p["attributes"]["name"])
        ]
        return {"data": found, "links": {}}  # fmt: skip

    @app.delete("/v1/profiles/{pid}")
    async def delete_profile(pid: str) -> Any:
        if state.profiles.pop(pid, None) is None:
            return error(404, "There is no resource of type 'profiles' with id " + pid)
        return Response(status_code=204)

    @app.post("/v1/profiles")
    async def new_profile(request: Request) -> Any:
        data = (await request.json())["data"]
        name, kind = data["attributes"]["name"], data["attributes"]["profileType"]
        bundle_id = data["relationships"]["bundleId"]["data"]["id"]
        certs = [c["id"] for c in data["relationships"]["certificates"]["data"]]
        bundle = next((b for b in apps.bundle_ids if b["id"] == bundle_id), None)
        if bundle is None or not all(c in state.certificates for c in certs):
            return error(409, "The bundle ID or a certificate does not exist.")
        if kind not in ("IOS_APP_STORE", "MAC_APP_STORE"):
            return error(409, f"profileType {kind!r} is not supported here")
        if any(p["attributes"]["name"] == name for p in state.profiles.values()):
            return error(409, "Multiple profiles found with the name '" + name + "'.")
        identifier, profile_uuid = bundle["attributes"]["identifier"], str(uuid.uuid4()).upper()
        content = plistlib.dumps({"Name": name, "UUID": profile_uuid, "TeamIdentifier": [TEAM],
                                  "Entitlements": {"application-identifier":
                                                   f"{TEAM}.{identifier}"}})  # fmt: skip
        pid = new_id()
        state.profiles[pid] = {"certificates": certs, "bundle": identifier, "attributes": {
            "name": name, "profileType": kind, "profileState": "ACTIVE", "uuid": profile_uuid,
            "profileContent": base64.b64encode(content).decode()}}  # fmt: skip
        return resource("profiles", pid, state.profiles[pid]["attributes"])


def upload_routes(app: FastAPI, state: ReleaseState, apps: Any, error: Any) -> None:
    """Build uploads and the processing that follows them."""

    @app.post("/v1/buildUploads")
    async def new_upload(request: Request) -> Any:
        data = (await request.json())["data"]
        attributes, app_id = data["attributes"], data["relationships"]["app"]["data"]["id"]
        if not any(a["id"] == app_id for a in apps.apps):
            return error(404, "There is no resource of type 'apps' with id " + app_id)
        platform, number = attributes["platform"], int(attributes["cfBundleVersion"])
        earlier = [int(u["attributes"]["cfBundleVersion"]) for u in state.uploads.values()
                   if u["app"] == app_id and u["attributes"]["platform"] == platform]  # fmt: skip
        if earlier and number <= max(earlier):
            return error(409, "The bundle version must be higher than the previously uploaded "
                              f"version: '{max(earlier)}'.")  # fmt: skip
        uid = new_id()
        state.uploads[uid] = {"app": app_id, "seen": 0, "build": None, "attributes": {
            **attributes, "state": {"state": "AWAITING_UPLOAD", "errors": []}}}  # fmt: skip
        return resource("buildUploads", uid, state.uploads[uid]["attributes"])

    @app.post("/v1/buildUploadFiles")
    async def new_file(request: Request) -> Any:
        data = (await request.json())["data"]
        attributes = data["attributes"]
        upload = state.uploads.get(data["relationships"]["buildUpload"]["data"]["id"])
        if upload is None:
            return error(404, "There is no such build upload.")
        if attributes.get("uti") != UTI[upload["attributes"]["platform"]]:
            return error(409, f"A {upload['attributes']['platform']} build cannot be a "
                              f"{attributes.get('uti')}.")  # fmt: skip
        fid, size = new_id(), int(attributes["fileSize"])
        operations = [{"method": "PUT", "url": f"{state.base_url}/upload/{fid}/{n}",
                       "offset": offset, "length": min(state.part_size, size - offset),
                       "partNumber": n + 1, "requestHeaders": [
                           {"name": "Content-Type", "value": "application/octet-stream"}]}
                      for n, offset in enumerate(range(0, size, state.part_size))]  # fmt: skip
        state.files[fid] = {"upload": data["relationships"]["buildUpload"]["data"]["id"],
                            "size": size, "parts": {}, "operations": operations}  # fmt: skip
        return resource("buildUploadFiles", fid, {**attributes, "uploadOperations": operations})

    @app.put("/upload/{fid}/{part}")
    async def put_part(fid: str, part: int, request: Request) -> Any:
        if "authorization" in request.headers:
            state.credentials_at_upload += 1
            return error(400, "upload URLs take no credentials")
        found = state.files.get(fid)
        body = await request.body()
        if found is None or len(body) != found["operations"][part]["length"]:
            return error(400, "this part does not belong here, or is not whole")
        found["parts"][part] = body
        return Response(status_code=200)

    @app.patch("/v1/buildUploadFiles/{fid}")
    async def uploaded(fid: str, request: Request) -> Any:
        attributes = (await request.json())["data"]["attributes"]
        found = state.files.get(fid)
        if found is None or not attributes.get("uploaded"):
            return error(409, "nothing was uploaded")
        whole = b"".join(found["parts"][n] for n in sorted(found["parts"]))
        checksum = attributes.get("sourceFileChecksums", {}).get("file", {})
        if len(whole) != found["size"] or checksum.get("hash") != hashlib.md5(whole).hexdigest():
            return error(409, "The uploaded file does not match its size or checksum.")
        upload = state.uploads[found["upload"]]
        upload["attributes"]["state"] = {"state": "PROCESSING", "errors": []}
        upload["data"] = whole
        return resource("buildUploadFiles", fid, {"uploaded": True})

    @app.get("/v1/buildUploads/{uid}")
    async def upload_state(uid: str) -> Any:
        upload = state.uploads.get(uid)
        if upload is None:
            return error(404, "There is no such build upload.")
        if upload["attributes"]["state"]["state"] == "PROCESSING":
            upload["seen"] += 1
            if upload["seen"] >= state.polls:
                upload["attributes"]["state"] = {"state": "COMPLETE", "errors": []}
                upload["build"] = bid = new_id()
                state.builds[bid] = {"seen": 0, "app": upload["app"], "attributes": {
                    "version": upload["attributes"]["cfBundleVersion"],
                    "processingState": "PROCESSING"}}  # fmt: skip
        build = {"type": "builds", "id": upload["build"]} if upload["build"] else None
        return resource("buildUploads", uid, upload["attributes"], build=build)

    @app.get("/v1/builds/{bid}")
    async def build(bid: str) -> Any:
        found = state.builds.get(bid)
        if found is None:
            return error(404, "There is no resource of type 'builds' with id " + bid)
        found["seen"] += 1
        if found["attributes"]["processingState"] == "PROCESSING" and found["seen"] >= state.polls:
            found["attributes"]["processingState"] = "INVALID" if state.invalid_builds else "VALID"
        return resource("builds", bid, found["attributes"])


def testflight_routes(app: FastAPI, state: ReleaseState, error: Any) -> None:
    """Beta groups."""

    @app.get("/v1/apps/{app_id}/betaGroups")
    async def groups(app_id: str) -> Any:
        found = [{"type": "betaGroups", "id": g["id"], "attributes": g["attributes"]}
                 for g in state.beta_groups if g["app"] == app_id]  # fmt: skip
        return {"data": found, "links": {}}

    @app.post("/v1/betaGroups")
    async def new_group(request: Request) -> Any:
        data = (await request.json())["data"]
        app_id = data["relationships"]["app"]["data"]["id"]
        if any(g["app"] == app_id and g["attributes"]["name"] == data["attributes"]["name"]
               for g in state.beta_groups):  # fmt: skip
            return error(409, "A group with this name already exists.")
        group = {"id": new_id(), "app": app_id, "attributes": data["attributes"]}
        state.beta_groups.append(group)
        return resource("betaGroups", group["id"], group["attributes"])
