"""How an account looks to the web UI, and which accounts may sign in."""

from typing import Any

from fastapi import HTTPException

from forge_web.db.models import User
from forge_web.services import Services


def user_view(user: User, services: Services | None = None) -> dict[str, Any]:
    """The signed-in user as the web UI sees them."""
    required = services is not None and services.settings.auth.admin_two_factor
    return {
        "id": user.id, "email": user.email, "name": user.name, "role": user.role,
        "avatar_url": user.avatar_url, "default_model": user.default_model,
        "has_password": user.password_hash is not None, "totp_enabled": user.totp_enabled,
        "two_factor_required": required and user.role == "admin" and not user.totp_enabled,
    }  # fmt: skip


def refuse_inactive(user: User) -> None:
    """403 with the reason for accounts that may not sign in."""
    reasons = {
        "pending": "your account is waiting for an admin to approve it",
        "unverified": "please confirm your email address first (see the mail we sent)",
        "disabled": "this account is disabled",
    }
    if user.status in reasons:
        raise HTTPException(403, reasons[user.status])
