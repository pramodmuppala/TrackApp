from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Shipment, TrackingEvent, utcnow

settings = get_settings()


class FedExConfigurationError(RuntimeError):
    pass


class FedExRequestError(RuntimeError):
    pass


@dataclass
class OAuthToken:
    access_token: str
    expires_at: datetime


_token_cache: OAuthToken | None = None


def official_tracking_url(tracking_number: str) -> str:
    return f"https://www.fedex.com/fedextrack/?trknbr={tracking_number}"


def _ensure_credentials() -> None:
    if not settings.fedex_client_id or not settings.fedex_client_secret:
        raise FedExConfigurationError(
            "FedEx credentials are not configured. Add FEDEX_CLIENT_ID and FEDEX_CLIENT_SECRET to .env."
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for candidate in [value.replace("Z", "+00:00"), value]:
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _first(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _get_token() -> str:
    global _token_cache
    _ensure_credentials()
    if _token_cache and _token_cache.expires_at > utcnow() + timedelta(seconds=60):
        return _token_cache.access_token

    url = f"{settings.fedex_base_url.rstrip('/')}{settings.fedex_oauth_path}"
    payload = {
        "grant_type": "client_credentials",
        "client_id": settings.fedex_client_id,
        "client_secret": settings.fedex_client_secret,
    }
    with httpx.Client(timeout=20.0) as client:
        response = client.post(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if response.status_code >= 400:
        raise FedExRequestError(f"FedEx OAuth failed: {response.status_code} {response.text[:300]}")

    data = response.json()
    expires_in = int(data.get("expires_in", 3600))
    _token_cache = OAuthToken(data["access_token"], utcnow() + timedelta(seconds=expires_in))
    return _token_cache.access_token


def fetch_tracking_detail(tracking_number: str) -> dict[str, Any]:
    token = _get_token()
    url = f"{settings.fedex_base_url.rstrip('/')}{settings.fedex_tracking_path}"
    payload = {
        "includeDetailedScans": True,
        "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking_number}}],
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=25.0) as client:
        response = client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        raise FedExRequestError(f"FedEx tracking failed: {response.status_code} {response.text[:400]}")
    data = response.json()
    if not isinstance(data, dict):
        raise FedExRequestError("Unexpected FedEx tracking response shape.")
    return data


def _extract_result(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("output", {}).get("completeTrackResults") or []
    if not results:
        raise FedExRequestError("FedEx returned no tracking results for this number.")
    track_results = results[0].get("trackResults") or []
    if not track_results:
        raise FedExRequestError("FedEx returned no detailed track result.")
    return track_results[0]


def sync_shipment_tracking(db: Session, shipment: Shipment) -> Shipment:
    payload = fetch_tracking_detail(shipment.tracking_number)
    result = _extract_result(payload)

    shipment.official_tracking_url = official_tracking_url(shipment.tracking_number)
    shipment.latest_payload = payload
    shipment.last_synced_at = utcnow()

    destination = result.get("destinationLocation") or {}
    origin = result.get("originLocation") or {}

    shipment.destination_city = _first(destination.get("city"), destination.get("locationContactAndAddress", {}).get("address", {}).get("city"))
    shipment.destination_state = _first(destination.get("stateOrProvinceCode"), destination.get("locationContactAndAddress", {}).get("address", {}).get("stateOrProvinceCode"))
    shipment.destination_zip = _first(destination.get("postalCode"), destination.get("locationContactAndAddress", {}).get("address", {}).get("postalCode"))
    shipment.destination_country = _first(destination.get("countryCode"), destination.get("locationContactAndAddress", {}).get("address", {}).get("countryCode"))
    shipment.origin_city = _first(origin.get("city"), origin.get("locationContactAndAddress", {}).get("address", {}).get("city"))
    shipment.origin_state = _first(origin.get("stateOrProvinceCode"), origin.get("locationContactAndAddress", {}).get("address", {}).get("stateOrProvinceCode"))
    shipment.origin_zip = _first(origin.get("postalCode"), origin.get("locationContactAndAddress", {}).get("address", {}).get("postalCode"))
    shipment.origin_country = _first(origin.get("countryCode"), origin.get("locationContactAndAddress", {}).get("address", {}).get("countryCode"))

    latest_status = result.get("latestStatusDetail") or {}
    shipment.status = _first(latest_status.get("statusByLocale"), latest_status.get("description"), result.get("latestStatusDetail", {}).get("statusByLocale"))
    shipment.status_category = _first(latest_status.get("code"), result.get("deliveryOptionEligibilityDetails", [{}])[0].get("option"))
    shipment.status_summary = _first(latest_status.get("ancillaryDetails", [{}])[0].get("reason"), latest_status.get("description"), shipment.status)

    latest_event_dt: datetime | None = None
    for item in result.get("scanEvents") or []:
        event_dt = _parse_dt(item.get("date") or item.get("dateTime"))
        if event_dt and (latest_event_dt is None or event_dt > latest_event_dt):
            latest_event_dt = event_dt
        location = item.get("scanLocation") or {}
        exists = db.scalar(
            select(TrackingEvent).where(
                TrackingEvent.shipment_id == shipment.id,
                TrackingEvent.event_timestamp == event_dt,
                TrackingEvent.event_code == item.get("eventType"),
                TrackingEvent.event_type == item.get("derivedStatus"),
            )
        )
        if exists:
            continue
        db.add(
            TrackingEvent(
                shipment_id=shipment.id,
                event_type=item.get("derivedStatus") or item.get("eventDescription"),
                event_code=item.get("eventType"),
                event_timestamp=event_dt,
                event_city=_first(location.get("city")),
                event_state=_first(location.get("stateOrProvinceCode")),
                event_zip=_first(location.get("postalCode")),
                event_country=_first(location.get("countryCode")),
                description=_first(item.get("eventDescription"), item.get("derivedStatus"), shipment.status),
                raw_payload=item,
            )
        )

    shipment.last_event_at = latest_event_dt or _parse_dt(result.get("estimatedDeliveryTimeWindow", {}).get("window", {}).get("ends"))
    db.add(shipment)
    db.flush()
    db.refresh(shipment)
    return shipment
