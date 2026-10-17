"""Who may use an admin's powers now (the security review after W39, W42): an admin, with
two-factor sign-in on where the server asks for it (`auth.admin_two_factor`). The admin routes
check it through `require_admin`; the places where being an admin skips a step check it here."""

from forge_web.db.models import User


def acts_as_admin(user: User, two_factor_required: bool) -> bool:
    """Whether the user may use an admin's powers now."""
    return user.role == "admin" and (user.totp_enabled or not two_factor_required)
