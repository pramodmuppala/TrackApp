from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class RecipientProfile(TimestampMixin, Base):
    __tablename__ = "recipient_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    company: Mapped[str | None] = mapped_column(String(160), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="tracking", nullable=False)
    raw_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    shipments: Mapped[list[Shipment]] = relationship("Shipment", back_populates="recipient")


class Shipment(TimestampMixin, Base):
    __tablename__ = "shipments"
    __table_args__ = (UniqueConstraint("carrier", "tracking_number", name="uq_shipment_carrier_tracking"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipient_id: Mapped[int | None] = mapped_column(
        ForeignKey("recipient_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tracking_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    carrier: Mapped[str] = mapped_column(String(30), default="USPS", nullable=False, index=True)
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    destination_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    destination_state: Mapped[str | None] = mapped_column(String(10), nullable=True)
    destination_zip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    destination_country: Mapped[str | None] = mapped_column(String(120), nullable=True)
    origin_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    origin_state: Mapped[str | None] = mapped_column(String(10), nullable=True)
    origin_zip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    origin_country: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    official_tracking_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    recipient: Mapped[RecipientProfile | None] = relationship("RecipientProfile", back_populates="shipments")
    events: Mapped[list[TrackingEvent]] = relationship(
        "TrackingEvent",
        back_populates="shipment",
        cascade="all, delete-orphan",
        order_by="desc(TrackingEvent.event_timestamp)",
    )


class TrackingEvent(TimestampMixin, Base):
    __tablename__ = "tracking_events"
    __table_args__ = (
        UniqueConstraint("shipment_id", "event_timestamp", "event_code", "event_type", name="uq_tracking_event_dedupe"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    event_state: Mapped[str | None] = mapped_column(String(10), nullable=True)
    event_zip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    event_country: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    shipment: Mapped[Shipment] = relationship("Shipment", back_populates="events")
