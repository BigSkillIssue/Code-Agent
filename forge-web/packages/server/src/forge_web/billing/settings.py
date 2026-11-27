"""`[billing]` in forge-web.toml (Phase E, trial since W72), apart so settings.py stays short."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BillingSettings(BaseModel):
    """Customers paying the admin (Phase E), through the admin's own Stripe account."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allow_live: bool = False  # test keys only until the legal texts are there (W34)
    # A catalog plan for customers without a subscription ("": the server's own limits).
    free_plan: str = ""
    # Plans set AI budgets in EUR; the gateway counts USD.
    usd_per_eur: float = Field(default=1.08, gt=0)
    # Dunning (W33b2): unpaid -> grace -> hosted apps suspended -> notice and export -> deleted.
    grace_days: float = Field(default=14, gt=0)
    notice_after_days: float = Field(default=14, gt=0)  # after the suspension
    delete_after_days: float = Field(default=30, gt=0)  # after the notice, once exported
    # Legal texts (W34): which the customer accepts before paying, and where VAT IDs are checked.
    required_texts: list[Literal["terms", "privacy", "dpa", "aup"]] = [
        "terms",
        "privacy",
        "dpa",
        "aup",
    ]
    vies_url: str = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
    # Payments in the apps (W35): the admin's share of every charge, in percent.
    connect_fee_percent: float = Field(default=0, ge=0, le=50)
    # A new account's trial (W72): days (0: none) and credit on the server's AI keys.
    trial_days: float = Field(default=14, ge=0, le=365)
    trial_credit_usd: float = Field(default=5, ge=0, le=10_000)
