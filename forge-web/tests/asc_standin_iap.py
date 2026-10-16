"""Apple's side of in-app purchases for the tests (W36): a certificate chain like Apple's (a root,
an intermediate with Apple's WWDR mark, a leaf with Apple's receipt-signing mark), made here,
and App Store Server Notifications v2 signed with it, as Apple sends them."""

import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

LEAF_MARK = x509.ObjectIdentifier("1.2.840.113635.100.6.11.1")
INTERMEDIATE_MARK = x509.ObjectIdentifier("1.2.840.113635.100.6.2.1")
DAY = 86400.0


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def certificate(
    name: str, key: ec.EllipticCurvePrivateKey, issuer: x509.Certificate | None,
    signer: ec.EllipticCurvePrivateKey, mark: x509.ObjectIdentifier | None, ca: bool,
) -> x509.Certificate:  # fmt: skip
    """A certificate for `key`, signed by `signer` (itself when there is no issuer)."""
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    builder = (x509.CertificateBuilder().subject_name(subject)
               .issuer_name(issuer.subject if issuer else subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), True))  # fmt: skip
    if mark is not None:
        builder = builder.add_extension(x509.UnrecognizedExtension(mark, b"\x05\x00"), False)
    return builder.sign(signer, hashes.SHA256())


class AppleSigner:
    """Signs like Apple: a chain of three, and ES256 with the leaf's key."""

    def __init__(self, marks: bool = True) -> None:
        self.root_key = ec.generate_private_key(ec.SECP384R1())
        self.root = certificate("Stand-in Root CA", self.root_key, None, self.root_key, None, True)
        middle_key = ec.generate_private_key(ec.SECP384R1())
        self.intermediate = certificate("Stand-in WWDR", middle_key, self.root, self.root_key,
                                        INTERMEDIATE_MARK if marks else None, True)  # fmt: skip
        self.leaf_key = ec.generate_private_key(ec.SECP256R1())
        self.leaf = certificate("Stand-in Store Signing", self.leaf_key, self.intermediate,
                                middle_key, LEAF_MARK if marks else None, False)  # fmt: skip

    def sign(self, payload: dict[str, Any], chain: list[x509.Certificate] | None = None) -> str:
        """A JWS of the payload with the chain in its header."""
        certs = chain or [self.leaf, self.intermediate, self.root]
        header = {"alg": "ES256", "x5c": [base64.b64encode(c.public_bytes(Encoding.DER)).decode()
                                          for c in certs]}  # fmt: skip
        signing = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}"
        r, s = decode_dss_signature(self.leaf_key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        return f"{signing}.{b64url(r.to_bytes(32) + s.to_bytes(32))}"

    def notification(
        self, kind: str, bundle: str, purchase: dict[str, Any], notice_id: str = "",
        environment: str = "Sandbox",
    ) -> dict[str, str]:  # fmt: skip
        """The body Apple posts: the signed notification with the signed purchase inside."""
        transaction = self.sign({"bundleId": bundle, "environment": environment, **purchase})
        notice = {"notificationType": kind, "notificationUUID": notice_id or str(uuid.uuid4()),
                  "version": "2.0", "signedDate": int(time.time() * 1000),
                  "data": {"bundleId": bundle, "environment": environment,
                           "signedTransactionInfo": transaction}}  # fmt: skip
        return {"signedPayload": self.sign(notice)}


def subscription(
    original: str, product: str, expires_in_days: float, **more: Any
) -> dict[str, Any]:
    """A subscription's transaction as Apple describes it (times in milliseconds)."""
    now = time.time()
    return {"transactionId": f"{original}-{int(now * 1000)}", "originalTransactionId": original,
            "productId": product, "purchaseDate": int(now * 1000),
            "expiresDate": int((now + expires_in_days * DAY) * 1000),
            "type": "Auto-Renewable Subscription", **more}  # fmt: skip
