"""What Sign in with Apple and Facebook Login need beyond plain OpenID Connect.

Apple wants a client secret that is itself a short-lived JWT, signed (ES256) with the developer's
Sign in with Apple key, and posts its answer back (`response_mode=form_post`) when it is asked for
the name and email. Facebook has no ID token: who signed in comes from its Graph API.
"""

import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_AUTHORIZE = f"{APPLE_ISSUER}/auth/authorize"
APPLE_TOKEN = f"{APPLE_ISSUER}/auth/token"
APPLE_SECRET_SECONDS = 300  # Apple accepts up to six months; one sign-in needs minutes

FACEBOOK_VERSION = "v23.0"
FACEBOOK_AUTHORIZE = f"https://www.facebook.com/{FACEBOOK_VERSION}/dialog/oauth"
FACEBOOK_GRAPH = f"https://graph.facebook.com/{FACEBOOK_VERSION}"
FACEBOOK_TOKEN = f"{FACEBOOK_GRAPH}/oauth/access_token"
FACEBOOK_FIELDS = "id,name,email,picture.type(large)"


def b64url(data: bytes) -> str:
    """Base64url without padding, as JWTs use it."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def load_apple_key(pem: str) -> ec.EllipticCurvePrivateKey:
    """The P-256 key from a .p8 file's text; ValueError if it is not one."""
    key = serialization.load_pem_private_key(pem.strip().encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError("a Sign in with Apple key is a P-256 key")
    return key


def apple_client_secret(
    key: ec.EllipticCurvePrivateKey,
    team_id: str,
    key_id: str,
    client_id: str,
    now: float | None = None,
) -> str:
    """The client secret Apple's token endpoint wants: an ES256 JWT about this app."""
    issued = int(now if now is not None else time.time())
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    claims = {"iss": team_id, "iat": issued, "exp": issued + APPLE_SECRET_SECONDS,
              "aud": APPLE_ISSUER, "sub": client_id}  # fmt: skip
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(claims).encode())}"
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    return f"{signing_input}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def apple_name(user: str) -> str:
    """The name in the `user` field Apple posts on the first sign-in only ("" otherwise)."""
    try:
        data = json.loads(user) if user else {}
    except ValueError:
        return ""
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, dict):
        return ""
    parts = (name.get("firstName"), name.get("lastName"))
    return " ".join(str(p).strip() for p in parts if isinstance(p, str) and p.strip())[:200]
