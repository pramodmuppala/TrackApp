from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Shipment, TrackingEvent, utcnow

settings = get_settings()


class UPSConfigurationError(RuntimeError):
    pass


class UPSRequestError(RuntimeError):
    pass


@dataclass
class OAuthToken:
    access_token: str
    expires_at: datetime


_token_cache: OAuthToken | None = None


def official_tracking_url(tracking_number: str) -> str:
    return f"https://www.ups.com/track?tracknum={tracking_number}"


def _ensure_credentials() -> None:
    if not settings.ups_client_id or not settings.ups_client_secret:
        raise UPSConfigurationError(
            "UPS credentials are not configured. Add UPS_CLIENT_ID and UPS_CLIENT_SECRET to .env."
        )


def _parse_dt(*values: str | None) -> datetime | None:
    for value in values:
        if not value:
            continue
        candidate = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
        if len(value) == 8 and value.isdigit():
            try:
                return datetime.strptime(value, "%Y%m%d")
            except ValueError:
                continue
        if len(value) == 14 and value.isdigit():
            try:
                return datetime.strptime(value, "%Y%m%d%H%M%S")
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

    url = f"{settings.ups_base_url.rstrip('/')}{settings.ups_oauth_path}"
    basic = b64encode(f"{settings.ups_client_id}:{settings.ups_client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=20.0) as client:
        response = client.post(url, data={"grant_type": "client_credentials"}, headers=headers)
    if response.status_code >= 400:
        raise UPSRequestError(f"UPS OAuth failed: {response.status_code} {response.text[:300]}")
    data = response.json()
    expires_in = int(data.get("expires_in", 3600))
    _token_cache = OAuthToken(data["access_token"], utcnow() + timedelta(seconds=expires_in))
    return _token_cache.access_token


def fetch_tracking_detail(tracking_number: str) -> dict[str, Any]:
    token = _get_token()
    path = settings.ups_tracking_path_template.format(tracking_number=tracking_number)
    url = f"{settings.ups_base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with httpx.Client(timeout=25.0) as client:
        response = client.get(url, headers=headers, params={"locale": "en_US"})
    if response.status_code >= 400:
        raise UPSRequestError(f"UPS tracking failed: {response.status_code} {response.text[:400]}")
    data = response.json()
    if not isinstance(data, dict):
        raise UPSRequestError("Unexpected UPS tracking response shape.")
    return data


def _extract_package(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    track_response = payload.get("trackResponse") or payload
    shipments = track_response.get("shipment") or []
    if not shipments:
        raise UPSRequestError("UPS returned no shipment results for this number.")
    shipment = shipments[0]
    packages = shipment.get("package") or []
    if not packages:
        raise UPSRequestError("UPS returned no package details.")
    return shipment, packages[0]


def sync_shipment_tracking(db: Session, shipment: Shipment) -> Shipment:
    payload = fetch_tracking_detail(shipment.tracking_number)
    shipment_node, package = _extract_package(payload)

    shipment.official_tracking_url = official_tracking_url(shipment.tracking_number)
    shipment.latest_payload = payload
    shipment.last_synced_at = utcnow()

    delivery = package.get("deliveryDate") or []
    if isinstance(delivery, list):
        delivery_value = delivery[0] if delivery else None
    else:
        delivery_value = delivery

    current_status = package.get("currentStatus") or {}
    latest_activity = (package.get("activity") or [{}])[0]
    location = latest_activity.get("location", {}).get("address", {}) if isinstance(latest_activity, dict) else {}

    shipment.destination_city = _first(location.get("city"))
    shipment.destination_state = _first(location.get("stateProvince"), location.get("stateProvinceCode"))
    shipment.destination_zip = _first(location.get("postalCode"))
    shipment.destination_country = _first(location.get("countryCode"))
    shipment.origin_city = _first(shipment_node.get("originAddress", {}).get("city"))
    shipment.origin_state = _first(shipment_node.get("originAddress", {}).get("stateProvinceCode"))
    shipment.origin_zip = _first(shipment_node.get("originAddress", {}).get("postalCode"))
    shipment.origin_country = _first(shipment_node.get("originAddress", {}).get("countryCode"))
    shipment.status = _first(current_status.get("description"), current_status.get("type"), package.get("message", {}).get("description"))
    shipment.status_category = _first(current_status.get("code"), current_status.get("type"))
    shipment.status_summary = _first(latest_activity.get("status", {}).get("description"), shipment.status)

    latest_event_dt: datetime | None = _parse_dt(delivery_value)
    for item in package.get("activity") or []:
        status = item.get("status") or {}
        address = item.get("location", {}).get("address", {}) if isinstance(item, dict) else {}
        event_dt = _parse_dt(item.get("dateTime"), f"{item.get('date','')}{item.get('time','')}" if item.get('date') and item.get('time') else item.get('date'))
        if event_dt and (latest_event_dt is None or event_dt > latest_event_dt):
            latest_event_dt = event_dt
        exists = db.scalar(
            select(TrackingEvent).where(
                TrackingEvent.shipment_id == shipment.id,
                TrackingEvent.event_timestamp == event_dt,
                TrackingEvent.event_code == status.get("code"),
                TrackingEvent.event_type == status.get("type"),
            )
        )
        if exists:
            continue
        db.add(
            TrackingEvent(
                shipment_id=shipment.id,
                event_type=status.get("type") or status.get("description"),
                event_code=status.get("code"),
                event_timestamp=event_dt,
                event_city=_first(address.get("city")),
                event_state=_first(address.get("stateProvince"), address.get("stateProvinceCode")),
                event_zip=_first(address.get("postalCode")),
                event_country=_first(address.get("countryCode")),
                description=_first(status.get("description"), shipment.status),
                raw_payload=item,
            )
        )

    shipment.last_event_at = latest_event_dt
    db.add(shipment)
    db.flush()
    db.refresh(shipment)
    return shipment
