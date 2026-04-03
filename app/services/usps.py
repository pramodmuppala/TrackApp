from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import RecipientProfile, Shipment, TrackingEvent, utcnow

settings = get_settings()


class USPSConfigurationError(RuntimeError):
    pass


class USPSRequestError(RuntimeError):
    pass


class USPSAccessRestrictedError(USPSRequestError):
    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


@dataclass
class OAuthToken:
    access_token: str
    expires_at: datetime


_token_cache: OAuthToken | None = None


def official_tracking_url(tracking_number: str) -> str:
    return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _ensure_credentials() -> None:
    if not settings.usps_client_id or not settings.usps_client_secret:
        raise USPSConfigurationError(
            "USPS credentials are not configured. Add USPS_CLIENT_ID and USPS_CLIENT_SECRET to .env."
        )


def _extract_error_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_error_message(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None


def _get_token() -> str:
    global _token_cache
    _ensure_credentials()

    if _token_cache and _token_cache.expires_at > utcnow() + timedelta(seconds=60):
        return _token_cache.access_token

    url = f"{settings.usps_base_url.rstrip('/')}{settings.usps_oauth_path}"
    payload = {
        "client_id": settings.usps_client_id,
        "client_secret": settings.usps_client_secret,
        "grant_type": "client_credentials",
    }
    with httpx.Client(timeout=20.0) as client:
        response = client.post(url, json=payload, headers={"Content-Type": "application/json"})
    if response.status_code >= 400:
        raise USPSRequestError(f"USPS OAuth failed: {response.status_code} {response.text[:300]}")

    data = response.json()
    expires_in = int(data.get("expires_in", "300"))
    _token_cache = OAuthToken(
        access_token=data["access_token"],
        expires_at=utcnow() + timedelta(seconds=expires_in),
    )
    return _token_cache.access_token


def fetch_tracking_detail(tracking_number: str) -> dict[str, Any]:
    token = _get_token()
    path = settings.usps_tracking_path_template.format(tracking_number=tracking_number)
    url = f"{settings.usps_base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    params = {"expand": "DETAIL"}
    with httpx.Client(timeout=25.0) as client:
        response = client.get(url, headers=headers, params=params)
    if response.status_code >= 400:
        error_payload = _extract_error_payload(response)
        error_message = _extract_error_message(error_payload)
        if response.status_code == 403 and error_message:
            lowered = error_message.lower()
            if "not authorized to access" in lowered or "access controls" in lowered or "ip agreement" in lowered:
                raise USPSAccessRestrictedError(
                    "USPS access restricted for this tracking number. Submit the USPS IP Agreement inquiry to re-enable API tracking.",
                    payload=error_payload,
                )
        detail = error_message or response.text[:400]
        raise USPSRequestError(f"USPS tracking failed: {response.status_code} {detail[:400]}")
    data = response.json()
    if not isinstance(data, dict):
        raise USPSRequestError("Unexpected USPS tracking response shape.")
    return data


def _mark_access_restricted(shipment: Shipment, exc: USPSAccessRestrictedError) -> None:
    shipment.official_tracking_url = official_tracking_url(shipment.tracking_number)
    shipment.last_synced_at = utcnow()
    shipment.status = "USPS access restricted"
    shipment.status_category = "Authorization"
    shipment.status_summary = str(exc)
    shipment.latest_payload = exc.payload or {
        "error": {
            "code": "403",
            "message": str(exc),
        }
    }


def _derive_recipient_data(payload: dict[str, Any]) -> dict[str, Any]:
    city = _first_non_empty(payload.get("destinationCity"))
    state = _first_non_empty(payload.get("destinationState"))
    postal_code = _first_non_empty(payload.get("destinationZIP"), payload.get("destinationZip"))
    country = _first_non_empty(payload.get("destinationCountry"))
    display_name = _first_non_empty(
        payload.get("recipientName"),
        payload.get("destinationName"),
        payload.get("toName"),
        payload.get("deliveryAddressName"),
    )
    company = _first_non_empty(
        payload.get("recipientCompany"),
        payload.get("destinationCompany"),
        payload.get("toCompany"),
    )

    return {
        "display_name": display_name,
        "company": company,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "country": country,
        "source": "usps_tracking",
        "raw_profile": {
            "display_name": display_name,
            "company": company,
            "city": city,
            "state": state,
            "postal_code": postal_code,
            "country": country,
        },
    }


def _sync_recipient_profile(db: Session, shipment: Shipment, payload: dict[str, Any]) -> None:
    profile_data = _derive_recipient_data(payload)
    if not any(value for key, value in profile_data.items() if key != "raw_profile"):
        return

    profile = shipment.recipient or RecipientProfile()
    for key, value in profile_data.items():
        if value:
            setattr(profile, key, value)

    db.add(profile)
    db.flush()
    shipment.recipient = profile


def sync_shipment_tracking(db: Session, shipment: Shipment) -> Shipment:
    try:
        payload = fetch_tracking_detail(shipment.tracking_number)
    except USPSAccessRestrictedError as exc:
        _mark_access_restricted(shipment, exc)
        db.add(shipment)
        db.flush()
        raise
    shipment.official_tracking_url = official_tracking_url(shipment.tracking_number)
    shipment.latest_payload = payload
    shipment.last_synced_at = utcnow()

    shipment.destination_city = payload.get("destinationCity")
    shipment.destination_state = payload.get("destinationState")
    shipment.destination_zip = payload.get("destinationZIP")
    shipment.destination_country = payload.get("destinationCountry")
    shipment.origin_city = payload.get("originCity")
    shipment.origin_state = payload.get("originState")
    shipment.origin_zip = payload.get("originZIP")
    shipment.origin_country = payload.get("originCountry")
    shipment.status = payload.get("status")
    shipment.status_category = payload.get("statusCategory")
    shipment.status_summary = payload.get("statusSummary")

    _sync_recipient_profile(db, shipment, payload)

    latest_event_dt: datetime | None = None
    tracking_events = payload.get("trackingEvents") or []
    for item in tracking_events:
        event_dt = _parse_dt(item.get("eventTimestamp"))
        latest_event_dt = max([dt for dt in [latest_event_dt, event_dt] if dt is not None], default=latest_event_dt)

        exists = db.scalar(
            select(TrackingEvent).where(
                TrackingEvent.shipment_id == shipment.id,
                TrackingEvent.event_timestamp == event_dt,
                TrackingEvent.event_code == item.get("eventCode"),
                TrackingEvent.event_type == item.get("eventType"),
            )
        )
        if exists:
            continue

        db.add(
            TrackingEvent(
                shipment_id=shipment.id,
                event_type=item.get("eventType"),
                event_code=item.get("eventCode"),
                event_timestamp=event_dt,
                event_city=item.get("eventCity"),
                event_state=item.get("eventState"),
                event_zip=item.get("eventZIP"),
                event_country=item.get("eventCountry"),
                description=item.get("eventDescription") or item.get("eventType") or payload.get("statusSummary"),
                raw_payload=item,
            )
        )

    shipment.last_event_at = latest_event_dt
    db.add(shipment)
    db.flush()
    db.refresh(shipment)
    return shipment
