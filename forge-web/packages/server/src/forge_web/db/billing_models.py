"""Tables of billing (Phase E): the admin's Stripe keys and the events Stripe sent (W31)."""

from sqlalchemy import Boolean, Float, Index, Integer, String, Text
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
