from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from email.message import EmailMessage
import smtplib

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Customer, PromotionCampaign, PromotionDispatch, utcnow

settings = get_settings()


@dataclass
class SendResult:
    sent: int
    failed: int
    skipped: int


def eligible_customers_query() -> Select[tuple[Customer]]:
    return select(Customer).where(
        Customer.marketing_opt_in.is_(True),
        Customer.unsubscribed_at.is_(None),
    ).order_by(Customer.created_at.desc())


def export_marketing_csv_rows(customers: list[Customer]) -> list[list[str]]:
    rows = [["first_name", "last_name", "email", "company", "phone", "marketing_source", "marketing_opt_in_at"]]
    for customer in customers:
        rows.append(
            [
                customer.first_name,
                customer.last_name or "",
                customer.email,
                customer.company or "",
                customer.phone or "",
                customer.marketing_source or "",
                customer.marketing_opt_in_at.isoformat() if customer.marketing_opt_in_at else "",
            ]
        )
    return rows


def _build_email(customer: Customer, campaign: PromotionCampaign) -> EmailMessage:
    unsubscribe_url = f"{settings.base_url.rstrip('/')}/unsubscribe/{customer.unsubscribe_token}"
    msg = EmailMessage()
    msg["Subject"] = campaign.subject
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    msg["To"] = customer.email
    msg.set_content(
        f"{campaign.preview_text or ''}\n\n{campaign.body_text}\n\nUnsubscribe: {unsubscribe_url}".strip()
    )
    return msg


def _send_via_smtp(message: EmailMessage) -> str:
    if not settings.smtp_host:
        raise RuntimeError("SMTP_HOST is not configured.")

    if settings.smtp_use_tls:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
        server.starttls()
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)

    try:
        if settings.smtp_username and settings.smtp_password:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)
    finally:
        server.quit()

    return message["Message-ID"] or "smtp-send"


def create_and_optionally_send_campaign(
    db: Session,
    name: str,
    subject: str,
    preview_text: str | None,
    body_text: str,
    send_now: bool,
) -> tuple[PromotionCampaign, SendResult]:
    campaign = PromotionCampaign(
        name=name,
        subject=subject,
        preview_text=preview_text,
        body_text=body_text,
        status="draft",
    )
    db.add(campaign)
    db.flush()

    customers = list(db.scalars(eligible_customers_query()).all())
    sent = failed = skipped = 0

    for customer in customers:
        dispatch = PromotionDispatch(
            campaign_id=campaign.id,
            customer_id=customer.id,
            email=customer.email,
            status="pending",
        )
        db.add(dispatch)
        db.flush()

        if not send_now:
            skipped += 1
            dispatch.status = "queued"
            continue

        if not settings.send_promotions_enabled:
            skipped += 1
            dispatch.status = "skipped"
            dispatch.error_message = "SEND_PROMOTIONS_ENABLED is false."
            continue

        try:
            email = _build_email(customer, campaign)
            message_id = _send_via_smtp(email)
            dispatch.status = "sent"
            dispatch.provider_message_id = message_id
            dispatch.sent_at = utcnow()
            sent += 1
        except Exception as exc:  # pragma: no cover - depends on SMTP runtime
            dispatch.status = "failed"
            dispatch.error_message = str(exc)
            failed += 1

    campaign.status = "sent" if send_now and sent > 0 and failed == 0 else "draft"
    campaign.sent_at = utcnow() if send_now and sent > 0 else None
    db.add(campaign)
    db.flush()
    return campaign, SendResult(sent=sent, failed=failed, skipped=skipped)
