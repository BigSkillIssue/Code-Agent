"""Apple in-app purchases on the server (W36): App Store Server Notifications v2.

Apple tells a hosted app's server about its purchases at `/api/apple/notifications/<app>` (the
URL the maker enters in App Store Connect). Each notification is a JWS whose header carries the
certificate chain that signed it; it counts only when that chain ends in Apple's root (Apple Root
CA - G3, kept here), each certificate signed the next and is valid now, the leaf and the
intermediate carry Apple's marks, and the signature fits. The purchase inside is signed the same
way. A notification is handled once, by its UUID. Purchases and renewals give the app's customer
the product until it expires; a refund or a revocation takes it away. The app asks for its
customers' products with its payments relay token (`GET /api/relay/payments/apple`); sandbox
purchases go to staging, real ones to production.
"""

import base64
import json
import time
from datetime import UTC, datetime
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from forge_web.billing.relay_tokens import payments_of
from forge_web.db.billing_models import AppleEntitlement, AppleNotification, AppPayments
from forge_web.db.hosting_models import HostedApp
from forge_web.db.models import AppleRelease
from forge_web.services import Services, services_of

APPLE_ROOT_G3 = b"""-----BEGIN CERTIFICATE-----
MIICQzCCAcmgAwIBAgIILcX8iNLFS5UwCgYIKoZIzj0EAwMwZzEbMBkGA1UEAwwS
QXBwbGUgUm9vdCBDQSAtIEczMSYwJAYDVQQLDB1BcHBsZSBDZXJ0aWZpY2F0aW9u
IEF1dGhvcml0eTETMBEGA1UECgwKQXBwbGUgSW5jLjELMAkGA1UEBhMCVVMwHhcN
MTQwNDMwMTgxOTA2WhcNMzkwNDMwMTgxOTA2WjBnMRswGQYDVQQDDBJBcHBsZSBS
b290IENBIC0gRzMxJjAkBgNVBAsMHUFwcGxlIENlcnRpZmljYXRpb24gQXV0aG9y
aXR5MRMwEQYDVQQKDApBcHBsZSBJbmMuMQswCQYDVQQGEwJVUzB2MBAGByqGSM49
AgEGBSuBBAAiA2IABJjpLz1AcqTtkyJygRMc3RCV8cWjTnHcFBbZDuWmBSp3ZHtf
TjjTuxxEtX/1H7YyYl3J6YRbTzBPEVoA/VhYDKX1DyxNB0cTddqXl5dvMVztK517
IDvYuVTZXpmkOlEKMaNCMEAwHQYDVR0OBBYEFLuw3qFYM4iapIqZ3r6966/ayySr
MA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgEGMAoGCCqGSM49BAMDA2gA
MGUCMQCD6cHEFl4aXTQY2e3v9GwOAEZLuN+yRhHFD/3meoyhpmvOwgPUnPWTxnS4
at+qIxUCMG1mihDK1A3UT82NQz60imOlM27jbdoXt2QfyFMm+YhidDkLF1vLUagM
6BgD56KyKA==
-----END CERTIFICATE-----
"""  # SHA-256 63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179, from apple.com
TRUSTED_ROOTS = [x509.load_pem_x509_certificate(APPLE_ROOT_G3)]  # the tests trust their own
LEAF_MARK = x509.ObjectIdentifier("1.2.840.113635.100.6.11.1")  # Apple's receipt signing
INTERMEDIATE_MARK = x509.ObjectIdentifier("1.2.840.113635.100.6.2.1")  # Apple WWDR
MAX_BODY = 256 * 1024
# Notifications that carry a purchase's current dates; the ones that take a purchase away.
UPDATES = {"SUBSCRIBED", "DID_RENEW", "OFFER_REDEEMED", "ONE_TIME_CHARGE", "REFUND_REVERSED",
           "DID_CHANGE_RENEWAL_PREF", "DID_CHANGE_RENEWAL_STATUS", "EXPIRED",
           "GRACE_PERIOD_EXPIRED", "PRICE_INCREASE", "RENEWAL_EXTENDED"}  # fmt: skip
TAKES = {"REFUND", "REVOKE"}
ENVIRONMENTS = {"Sandbox": "staging", "Production": "production"}


class SignedByNobody(ValueError):
    """A JWS that Apple did not sign (or that is not one)."""


def b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def verified(jws: str, now: datetime) -> dict[str, Any]:
    """The payload of a JWS signed by Apple's chain; SignedByNobody otherwise."""
    try:
        head, body, signature = jws.split(".")
        header = json.loads(b64(head))
        chain = [x509.load_der_x509_certificate(base64.b64decode(c)) for c in header["x5c"]]
        raw = b64(signature)
    except (ValueError, KeyError, TypeError) as error:
        raise SignedByNobody(f"not a signed notification: {error}") from None
    if header.get("alg") != "ES256" or len(chain) != 3 or len(raw) != 64:
        raise SignedByNobody("not Apple's kind of signature")
    leaf, intermediate, root = chain
    check_chain(leaf, intermediate, root, now)
    key = leaf.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise SignedByNobody("the signing key is not Apple's kind")
    der = encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:]))
    try:
        key.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise SignedByNobody("the signature does not fit") from None
    payload = json.loads(b64(body))
    if not isinstance(payload, dict):
        raise SignedByNobody("not a notification")
    return payload


def check_chain(
    leaf: x509.Certificate, intermediate: x509.Certificate, root: x509.Certificate, now: datetime
) -> None:
    """The leaf signed by the intermediate, signed by a trusted root; all valid now, and the
    leaf and the intermediate with Apple's marks."""
    if not any(root == trusted for trusted in TRUSTED_ROOTS):
        raise SignedByNobody("the chain does not end in Apple's root")
    try:
        intermediate.verify_directly_issued_by(root)
        leaf.verify_directly_issued_by(intermediate)
    except (ValueError, TypeError, InvalidSignature):
        raise SignedByNobody("a certificate of the chain was not signed by the next") from None
    for cert in (leaf, intermediate, root):
        if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
            raise SignedByNobody("a certificate of the chain is not valid now")
    for cert, mark in ((leaf, LEAF_MARK), (intermediate, INTERMEDIATE_MARK)):
        try:
            cert.extensions.get_extension_for_oid(mark)
        except x509.ExtensionNotFound:
            raise SignedByNobody("a certificate of the chain is not Apple's") from None


async def bundle_of(services: Services, project_id: str) -> str:
    """The bundle ID of the project's Apple app, from its newest release."""
    async with services.db.session() as session:
        newest = AppleRelease.created_at.desc()
        rows = await session.scalars(select(AppleRelease.data).where(
            AppleRelease.project_id == project_id).order_by(newest))  # fmt: skip
        for data in rows:
            bundle = str(json.loads(data or "{}").get("bundle_id", ""))
            if bundle:
                return bundle
    return ""


async def take_notification(services: Services, app: str, signed: str) -> bool:
    """Check and handle one notification; False when it came before (it counts once)."""
    now = datetime.now(UTC)
    try:
        notice = verified(signed, now)
        data = notice.get("data") or {}
        purchase = verified(str(data.get("signedTransactionInfo", "")), now)
    except SignedByNobody as error:
        raise HTTPException(400, str(error)) from None
    async with services.db.session() as session:
        hosted = await session.scalar(select(HostedApp).where(HostedApp.app == app))
    if hosted is None:
        raise HTTPException(404, "no such app")
    bundle = await bundle_of(services, hosted.project_id)
    if not bundle or data.get("bundleId") != bundle or purchase.get("bundleId") != bundle:
        raise HTTPException(403, "this notification is about another app")
    uuid = str(notice.get("notificationUUID", ""))
    if not uuid:
        raise HTTPException(400, "a notification without its UUID")
    async with services.db.session() as session, session.begin():
        if await session.get(AppleNotification, uuid) is not None:
            return False
        session.add(AppleNotification(uuid=uuid, project_id=hosted.project_id,
                                      kind=str(notice.get("notificationType", "")),
                                      received_at=time.time()))  # fmt: skip
        environment = ENVIRONMENTS.get(str(data.get("environment")), "staging")
        await apply(session, hosted.project_id, str(notice.get("notificationType", "")),
                    purchase, environment)  # fmt: skip
    return True


async def apply(session: Any, project_id: str, kind: str, purchase: dict[str, Any],
                environment: str) -> None:  # fmt: skip
    """What the notification does to the customer's product."""
    original = str(purchase.get("originalTransactionId") or purchase.get("transactionId") or "")
    if not original or (kind not in UPDATES and kind not in TAKES):
        return
    row = await session.get(AppleEntitlement, (project_id, original))
    if row is None:
        row = AppleEntitlement(project_id=project_id, original_transaction_id=original,
                               environment=environment, product_id="", account_token="",
                               expires_at=0.0, revoked_at=0.0, updated_at=0.0)  # fmt: skip
        session.add(row)
    row.product_id = str(purchase.get("productId", row.product_id))
    row.account_token = str(purchase.get("appAccountToken") or row.account_token)
    expires = float(purchase.get("expiresDate") or 0) / 1000  # 0: a purchase that never ends
    row.expires_at = max(row.expires_at, expires) if row.expires_at and expires else expires
    if kind in TAKES:
        row.revoked_at = float(purchase.get("revocationDate") or time.time() * 1000) / 1000
    else:  # a renewal, a new purchase, a reversed refund: the transaction says if it is revoked
        row.revoked_at = float(purchase.get("revocationDate") or 0) / 1000
    row.updated_at = time.time()


def entitlement_view(row: AppleEntitlement) -> dict[str, Any]:
    """A customer's product, for the app."""
    return {"original_transaction_id": row.original_transaction_id,
            "product_id": row.product_id, "account_token": row.account_token,
            "expires_at": row.expires_at}  # fmt: skip


def iap_router() -> APIRouter:
    """Apple's notifications (signed, no session) and the apps' question about them."""
    router = APIRouter()

    @router.post("/api/apple/notifications/{app}")
    async def notification(app: str, request: Request) -> dict[str, bool]:
        services = services_of(request)
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "too large for a notification")
        try:
            signed = str(json.loads(body)["signedPayload"])
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "not a notification") from None
        first = await take_notification(services, app, signed)
        return {"ok": True, "repeated": not first}

    @router.get("/api/relay/payments/apple")
    async def products(request: Request, account_token: str = "") -> list[dict[str, Any]]:
        services = services_of(request)
        relay = await app_of(services, request.headers.get("authorization", ""))
        now = time.time()
        async with services.db.session() as session:
            query = select(AppleEntitlement).where(
                AppleEntitlement.project_id == relay.project_id,
                AppleEntitlement.environment == relay.environment,
                AppleEntitlement.revoked_at == 0)  # fmt: skip
            if account_token:
                query = query.where(AppleEntitlement.account_token == account_token)
            rows = list(await session.scalars(query))
        return [entitlement_view(r) for r in rows if not r.expires_at or r.expires_at > now]

    return router


async def app_of(services: Services, authorization: str) -> AppPayments:
    """The app of a payments relay token that may still sell (401, 403)."""
    found = await payments_of(services.db, services.vault, authorization)
    if found is None:
        raise HTTPException(401, "not a valid payments relay token")
    async with services.db.session() as session:
        hosted = await session.get(HostedApp, found.project_id)
    if hosted is None or hosted.suspended or hosted.deleting_at:
        raise HTTPException(403, "this app may not take payments")
    return found
