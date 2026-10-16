"""Signing an Android bundle for Google Play (W37b) with the app's upload key.

Google Play takes only bundles signed with the app's upload key and signs the app itself for its
users (Play App Signing). Forge Web makes one upload key per app (RSA, a self-signed certificate
valid for 30 years, as Google asks for at least 25), keeps it in the vault, and signs every
bundle on this server, so the key never reaches a host. A bundle is signed as a JAR is
(`jarsigner`): a manifest with each entry's SHA-256, a signature file over the manifest, and a
detached PKCS #7 signature of that file.
"""

import base64
import datetime
import hashlib
import zipfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

KEY_BITS = 3072
VALID_YEARS = 30
SIGNER = "FORGE"  # META-INF/FORGE.SF and META-INF/FORGE.RSA
CREATED_BY = "Forge Web"
LINE = 72  # a manifest line's bytes before it continues on the next line
OWN = ("META-INF/MANIFEST.MF",)  # what a signature replaces
SIGNATURE_SUFFIXES = (".SF", ".RSA", ".DSA", ".EC")


def new_upload_key(app_name: str) -> tuple[str, str]:
    """A new upload key and its certificate, both PEM."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{app_name} upload key")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365 * VALID_YEARS))
        .sign(key, hashes.SHA256())
    )
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())  # fmt: skip
    return private.decode(), certificate.public_bytes(serialization.Encoding.PEM).decode()


def fingerprint(certificate_pem: str) -> str:
    """The certificate's SHA-256, as Play Console shows it (AB:CD:...)."""
    certificate = x509.load_pem_x509_certificate(certificate_pem.encode())
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def header(name: str, value: str) -> bytes:
    """One manifest header, wrapped at 72 bytes as the JAR format wants."""
    line = f"{name}: {value}".encode()
    parts = [line[:LINE]] + [b" " + line[at : at + LINE - 1]
                             for at in range(LINE, len(line), LINE - 1)]  # fmt: skip
    return b"".join(part + b"\r\n" for part in parts)


def is_signature(name: str) -> bool:
    """Whether an entry belongs to a signature (the bundle's own, or an earlier one)."""
    return name.startswith("META-INF/") and name.endswith(SIGNATURE_SUFFIXES)


def signed_entries(bundle: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The entries a signature covers: all files but the manifest and earlier signatures."""
    return [info for info in bundle.infolist() if not info.is_dir()
            and info.filename not in OWN and not is_signature(info.filename)]  # fmt: skip


def signature_files(
    bundle: zipfile.ZipFile, key_pem: str, certificate_pem: str
) -> dict[str, bytes]:
    """META-INF/MANIFEST.MF, FORGE.SF and FORGE.RSA for this bundle."""
    main = header("Manifest-Version", "1.0") + header("Created-By", CREATED_BY) + b"\r\n"
    sections: list[tuple[str, bytes]] = []
    for info in signed_entries(bundle):
        section = header("Name", info.filename)
        section += header("SHA-256-Digest", b64_sha256(bundle.read(info))) + b"\r\n"
        sections.append((info.filename, section))
    manifest = main + b"".join(section for _, section in sections)
    signature = (header("Signature-Version", "1.0") + header("Created-By", CREATED_BY)
                 + header("SHA-256-Digest-Manifest", b64_sha256(manifest))
                 + header("SHA-256-Digest-Manifest-Main-Attributes", b64_sha256(main))
                 + b"\r\n")  # fmt: skip
    for name, section in sections:
        signature += header("Name", name) + header("SHA-256-Digest", b64_sha256(section)) + b"\r\n"
    key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("an upload key is an RSA key")
    certificate = x509.load_pem_x509_certificate(certificate_pem.encode())
    block = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(signature)
        .add_signer(certificate, key, hashes.SHA256())
        .sign(serialization.Encoding.DER,
              [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.NoAttributes])
    )  # fmt: skip
    return {"META-INF/MANIFEST.MF": manifest, f"META-INF/{SIGNER}.SF": signature,
            f"META-INF/{SIGNER}.RSA": block}  # fmt: skip


def sign_bundle(unsigned: Path, signed: Path, key_pem: str, certificate_pem: str) -> None:
    """Write `signed`: the bundle with its signature first, every entry as it was."""
    partial = signed.with_suffix(".part")
    with zipfile.ZipFile(unsigned) as source, zipfile.ZipFile(partial, "w") as target:
        for name, data in signature_files(source, key_pem, certificate_pem).items():
            target.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), data,
                            compress_type=zipfile.ZIP_DEFLATED)  # fmt: skip
        for info in source.infolist():
            if info.filename not in OWN and not is_signature(info.filename):
                target.writestr(info, source.read(info), compress_type=info.compress_type)
    partial.replace(signed)
