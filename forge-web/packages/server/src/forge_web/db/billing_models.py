"""Tables of billing (Phase E): the admin's Stripe keys and the events Stripe sent (W31), the
catalog and its coupons (W32)."""

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from forge_web.db.models import Base


class BillingKeys(Base):
    """The admin's Stripe keys, encrypted with the vault (one row)."""

    __tablename__ = "billing_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    secret_key: Mapped[str] = mapped_column(Text)
    webhook_secret: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(8))  # "test" or "live"
    hint: Mapped[str] = mapped_column(String(8))  # the key's last characters, to tell keys apart
    updated_at: Mapped[float] = mapped_column(Float)
    updated_by: Mapped[str] = mapped_column(String(32), default="")


class BillingEvent(Base):
    """A Stripe event as it arrived: stored once, handled once, in the order Stripe made them."""

    __tablename__ = "billing_events"
    __table_args__ = (
        Index("billing_events_waiting", "handled_at", "created"),
        Index("billing_events_by_object", "object_id", "created"),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Stripe's evt_...
    type: Mapped[str] = mapped_column(String(100))
    object_id: Mapped[str] = mapped_column(String(255), default="")  # what it is about
    created: Mapped[float] = mapped_column(Float)  # when Stripe made it
    livemode: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[str] = mapped_column(Text)  # the whole event
    received_at: Mapped[float] = mapped_column(Float)
    handled_at: Mapped[float] = mapped_column(Float, default=0.0)  # 0: waiting
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str] = mapped_column(
        Text, default=""
    )  # handled, ignored, superseded, failed: …


class CatalogItem(Base):
    """Something customers can buy (W32): a plan, an app's one-time price, hosting or an add-on."""

    __tablename__ = "billing_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # the admin's name for it
    kind: Mapped[str] = mapped_column(String(16))  # plan, app_once, hosting, addon
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    currency: Mapped[str] = mapped_column(String(3), default="eur")
    amounts: Mapped[str] = mapped_column(Text, default="{}")  # interval -> cents
    trial_days: Mapped[int] = mapped_column(Integer, default=0)
    limits: Mapped[str] = mapped_column(Text, default="{}")  # what a plan allows (W33a)
    version: Mapped[int] = mapped_column(Integer, default=1)  # counts the admin's changes
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    archived_at: Mapped[float] = mapped_column(Float, default=0.0)  # removed by the admin
    stripe_product: Mapped[str] = mapped_column(String(255), default="")
    stripe_prices: Mapped[str] = mapped_column(Text, default="{}")  # interval -> id, amount
    product_synced: Mapped[str] = mapped_column(String(32), default="")  # what Stripe has
    synced_at: Mapped[float] = mapped_column(Float, default=0.0)


class Coupon(Base):
    """A discount with a code (W32); its terms stay once Stripe has it."""

    __tablename__ = "billing_coupons"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    percent_off: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_off: Mapped[int | None] = mapped_column(Integer, nullable=True)  # cents
    currency: Mapped[str] = mapped_column(String(3), default="eur")
    duration: Mapped[str] = mapped_column(String(16), default="once")
    duration_in_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    archived_at: Mapped[float] = mapped_column(Float, default=0.0)
    stripe_coupon: Mapped[str] = mapped_column(String(255), default="")
    stripe_promotion: Mapped[str] = mapped_column(String(255), default="")
    synced_at: Mapped[float] = mapped_column(Float, default=0.0)  # 0: a change waits for the sync


class Subscription(Base):
    """One item a customer pays for, as Stripe reports it (W33a): a plan, an add-on or hosting."""

    __tablename__ = "billing_subscriptions"
    __table_args__ = (Index("billing_subscriptions_by_user", "user_id", "status"),)

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Stripe's subscription item
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    item_id: Mapped[str] = mapped_column(String(40))  # in the catalog
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32))  # Stripe's: active, trialing, past_due, …
    interval: Mapped[str] = mapped_column(String(8), default="month")
    stripe_subscription: Mapped[str] = mapped_column(String(255), default="")
    current_period_end: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class BillingCustomer(Base):
    """A user as Stripe knows them (W33a2)."""

    __tablename__ = "billing_customers"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    stripe_customer: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[float] = mapped_column(Float)


class Checkout(Base):
    """One order through Stripe Checkout (W33a2), with the moment the customer waived the right of
    withdrawal; a completed one-time order is the record of that purchase."""

    __tablename__ = "billing_checkouts"
    __table_args__ = (Index("billing_checkouts_by_user", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Stripe's cs_...
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    item_id: Mapped[str] = mapped_column(String(40))
    interval: Mapped[str] = mapped_column(String(8))
    waiver_at: Mapped[float] = mapped_column(Float)  # when the box was ticked
    status: Mapped[str] = mapped_column(String(16), default="open")  # open, complete, expired
    created_at: Mapped[float] = mapped_column(Float)
    completed_at: Mapped[float] = mapped_column(Float, default=0.0)


class Cancellation(Base):
    """A cancellation as it came in through the cancellation button (§312k BGB, W33b1)."""

    __tablename__ = "billing_cancellations"
    __table_args__ = (Index("billing_cancellations_by_sender", "sender", "received_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    received_at: Mapped[float] = mapped_column(Float)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    contract: Mapped[str] = mapped_column(String(200), default="")  # "": every contract
    extraordinary: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[str] = mapped_column(String(32), default="")  # "" when no account fits
    status: Mapped[str] = mapped_column(String(16))  # done, or manual (an admin looks at it)
    effective_at: Mapped[float] = mapped_column(Float, default=0.0)  # 0: an admin confirms it
    sender: Mapped[str] = mapped_column(String(64), default="")  # a hash of the address, for limits


class DunningCase(Base):
    """A customer's unpaid bill and how far dunning has gone (W33b2)."""

    __tablename__ = "billing_dunning"
    __table_args__ = (Index("billing_dunning_open", "user_id", "closed_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    opened_at: Mapped[float] = mapped_column(Float)
    stage: Mapped[str] = mapped_column(String(16))  # grace, suspended, noticed, deleted
    stage_at: Mapped[float] = mapped_column(Float)  # when it reached its stage
    closed_at: Mapped[float] = mapped_column(Float, default=0.0)  # paid, or done


class LegalText(Base):
    """One version of a legal text the admin published (W34): terms, privacy, DPA or AUP."""

    __tablename__ = "legal_texts"

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)  # as the admin wrote it; shown as plain text
    published_at: Mapped[float] = mapped_column(Float)
    published_by: Mapped[str] = mapped_column(String(32), default="")


class LegalAcceptance(Base):
    """A user's acceptance of one version of a legal text (W34)."""

    __tablename__ = "legal_acceptances"
    __table_args__ = (Index("legal_acceptances_by_user", "user_id", "kind", "version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer)
    accepted_at: Mapped[float] = mapped_column(Float)
    ip: Mapped[str] = mapped_column(String(64), default="")


class VatCheck(Base):
    """A customer's EU VAT ID and what VIES said about it (W34)."""

    __tablename__ = "legal_vat_ids"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    vat_id: Mapped[str] = mapped_column(String(20))
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(Text, default="")  # as VIES registered it
    address: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[float] = mapped_column(Float)


class ConnectAccount(Base):
    """A maker's own Stripe Express account (W35), as Stripe last described it."""

    __tablename__ = "connect_accounts"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    stripe_account: Mapped[str] = mapped_column(String(64), unique=True)
    charges_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    payouts_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    details_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float)
    checked_at: Mapped[float] = mapped_column(Float)


class AppPayments(Base):
    """The payments relay of a hosted app in one environment: its token (W35)."""

    __tablename__ = "app_payments"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    environment: Mapped[str] = mapped_column(String(16), primary_key=True)
    token: Mapped[str] = mapped_column(Text)  # encrypted (it is sealed into every release)
    created_at: Mapped[float] = mapped_column(Float)


class RelayCheckout(Base):
    """A checkout an app opened through the relay, for one connected account (W35)."""

    __tablename__ = "relay_checkouts"
    __table_args__ = (Index("relay_checkouts_by_app", "project_id", "environment", "subscription"),)

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Stripe's cs_...
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    environment: Mapped[str] = mapped_column(String(16))
    stripe_account: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))  # payment or subscription
    amount: Mapped[int] = mapped_column(Integer)  # cents, all pieces
    currency: Mapped[str] = mapped_column(String(3))
    fee: Mapped[int] = mapped_column(Integer, default=0)  # cents; a subscription's is a percent
    reference: Mapped[str] = mapped_column(String(200), default="")
    subscription: Mapped[str] = mapped_column(String(255), default="")  # once Stripe made one
    created_at: Mapped[float] = mapped_column(Float)


class AppleNotification(Base):
    """An App Store Server Notification handled (W36): each counts once, by its UUID."""

    __tablename__ = "apple_notifications"

    uuid: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))  # Apple's notificationType
    received_at: Mapped[float] = mapped_column(Float)


class AppleEntitlement(Base):
    """What an app's customer bought in the app with Apple's in-app purchase (W36)."""

    __tablename__ = "apple_entitlements"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    original_transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    environment: Mapped[str] = mapped_column(String(16))  # staging (sandbox) or production
    product_id: Mapped[str] = mapped_column(String(200))
    account_token: Mapped[str] = mapped_column(String(64), default="")  # the app's own user id
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)  # 0: never
    revoked_at: Mapped[float] = mapped_column(Float, default=0.0)  # refunded or revoked
    updated_at: Mapped[float] = mapped_column(Float)
