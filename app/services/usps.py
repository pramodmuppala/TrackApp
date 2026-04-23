from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import RecipientProfile, Shipment, TrackingEvent, utcnow

try:
    from selenium import webdriver
    from selenium.common.exceptions import (
        NoSuchElementException,
        SessionNotCreatedException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.remote.webdriver import WebDriver
    from selenium.webdriver.safari.options import Options as SafariOptions
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:  # pragma: no cover - exercised via configuration tests
    webdriver = None
    WebDriver = Any
    By = None
    Keys = None
    SafariOptions = None
    WebDriverWait = None
    EC = None

    class NoSuchElementException(Exception):
        pass

    class SessionNotCreatedException(Exception):
        pass

    class TimeoutException(Exception):
        pass

    class WebDriverException(Exception):
        pass


TRACKING_INPUT_URL = "https://tools.usps.com/go/TrackConfirmAction_input"
STATUS_KEYWORDS = (
    "delivered",
    "delivery attempted",
    "out for delivery",
    "in transit",
    "arriving late",
    "moving through network",
    "arrived",
    "departed",
    "accepted",
    "label created",
    "pre-shipment",
    "available for pickup",
    "picked up",
    "forwarded",
    "return to sender",
    "shipping partner",
    "processed",
)
DATE_PATTERNS = (
    "%B %d, %Y, %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%b %d, %Y, %I:%M %p",
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y",
    "%b %d, %Y",
)
settings = get_settings()


class USPSConfigurationError(RuntimeError):
    pass


class USPSRequestError(RuntimeError):
    pass


class USPSAccessRestrictedError(USPSRequestError):
    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


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


def _require_selenium() -> None:
    if webdriver is None or SafariOptions is None:
        raise USPSConfigurationError(
            "Selenium is not installed. Run `pip install -r requirements.txt` to enable USPS web tracking."
        )


def _build_driver() -> WebDriver:
    _require_selenium()
    try:
        return webdriver.Safari(options=SafariOptions())
    except SessionNotCreatedException as exc:
        raise USPSConfigurationError(
            "Safari remote automation is disabled. Enable Safari > Settings > Advanced > Show Develop menu, "
            "then Develop > Allow Remote Automation."
        ) from exc
    except WebDriverException as exc:
        raise USPSRequestError(f"Unable to start Safari WebDriver: {exc}") from exc


def _find_first(driver: WebDriver, selectors: list[str]):
    last_error: Exception | None = None
    for selector in selectors:
        try:
            return driver.find_element(By.CSS_SELECTOR, selector)
        except NoSuchElementException as exc:
            last_error = exc
            continue
    raise USPSRequestError(f"USPS page layout changed; could not find any of: {', '.join(selectors)}") from last_error


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _wait_for_results(driver: WebDriver, tracking_number: str) -> None:
    def _results_ready(inner_driver: WebDriver) -> bool:
        body_text = inner_driver.find_element(By.TAG_NAME, "body").text
        lowered = body_text.lower()
        if "allow remote automation" in lowered:
            return True
        if tracking_number in body_text and ("tracking" in lowered or "latest update" in lowered):
            return True
        if "tracking history" in lowered or "latest update" in lowered or "status not available" in lowered:
            return True
        return False

    try:
        WebDriverWait(driver, settings.usps_browser_timeout_seconds).until(_results_ready)
    except TimeoutException as exc:
        raise USPSRequestError("USPS tracking page did not finish loading in time.") from exc


def _submit_tracking_number(driver: WebDriver, tracking_number: str) -> None:
    driver.get(TRACKING_INPUT_URL)
    tracking_input = _find_first(
        driver,
        [
            "input[name='tLabels']",
            "input[id*='tracking' i]",
            "input[aria-label*='tracking' i]",
            "input[placeholder*='tracking' i]",
            "input[type='text']",
        ],
    )
    tracking_input.clear()
    tracking_input.send_keys(tracking_number)

    try:
        button = _find_first(
            driver,
            [
                "button[type='submit']",
                "input[type='submit']",
                "button[aria-label*='track' i]",
                "button[id*='track' i]",
            ],
        )
        button.click()
    except USPSRequestError:
        tracking_input.send_keys(Keys.ENTER)


def _extract_json_candidates(page_source: str, tracking_number: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    script_blocks = re.findall(r"<script[^>]*>(.*?)</script>", page_source, flags=re.IGNORECASE | re.DOTALL)
    for block in script_blocks:
        if tracking_number not in block:
            continue
        for match in re.finditer(r"\{.*?\}", block, flags=re.DOTALL):
            snippet = match.group(0)
            if tracking_number not in snippet or '"tracking' not in snippet.lower():
                continue
            try:
                data = json.loads(snippet)
            except ValueError:
                continue
            if isinstance(data, dict):
                candidates.append(data)
    return candidates


def _walk_for_tracking_payload(obj: Any, tracking_number: str) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        serialized = json.dumps(obj, default=str).lower()
        if tracking_number.lower() in serialized and any(
            key in serialized for key in ("trackingevents", "statussummary", "trackingnumber", "statuscategory")
        ):
            return obj
        for value in obj.values():
            found = _walk_for_tracking_payload(value, tracking_number)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _walk_for_tracking_payload(value, tracking_number)
            if found:
                return found
    return None


def _status_category(status: str | None) -> str | None:
    lowered = (status or "").lower()
    if not lowered:
        return None
    if "deliver" in lowered:
        return "Delivered"
    if "out for delivery" in lowered:
        return "Out for Delivery"
    if any(term in lowered for term in ("in transit", "arriving late", "moving through network", "departed")):
        return "In Transit"
    if any(term in lowered for term in ("accepted", "arrived", "processed", "shipping partner")):
        return "Accepted"
    if any(term in lowered for term in ("label created", "pre-shipment")):
        return "Pre-Shipment"
    if "pickup" in lowered:
        return "Pickup"
    if "return" in lowered:
        return "Return"
    return "Tracking"


def _extract_status(lines: list[str]) -> str | None:
    for index, line in enumerate(lines):
        if line.lower() == "latest update" and index + 1 < len(lines):
            return lines[index + 1]
    for line in lines:
        lowered = line.lower()
        if any(keyword in lowered for keyword in STATUS_KEYWORDS):
            return line
    return None


def _extract_summary(lines: list[str], status: str | None) -> str | None:
    if not status:
        return None
    for index, line in enumerate(lines):
        if line == status and index + 1 < len(lines):
            next_line = lines[index + 1]
            if next_line != status and tracking_number_like(next_line) is False:
                return next_line
    return status


def tracking_number_like(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    return compact.isalnum() and 8 <= len(compact) <= 34


def _parse_event_datetime(value: str) -> datetime | None:
    cleaned = re.sub(r"\s+at\s+", " ", value, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    for pattern in DATE_PATTERNS:
        try:
            return datetime.strptime(cleaned, pattern)
        except ValueError:
            continue
    return None


def _extract_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        event_dt = _parse_event_datetime(line)
        if not event_dt:
            continue
        description_parts: list[str] = []
        for follower in lines[index + 1 : index + 4]:
            if _parse_event_datetime(follower):
                break
            if follower.lower() in {"tracking history", "latest update", "product information"}:
                break
            description_parts.append(follower)
        description = " ".join(description_parts).strip() or "Tracking update"
        event_type = description_parts[0] if description_parts else "Tracking update"
        events.append(
            {
                "eventTimestamp": event_dt.isoformat(),
                "eventType": event_type,
                "eventCode": event_type.lower().replace(" ", "_")[:40],
                "eventDescription": description,
                "rawText": [line, *description_parts],
            }
        )
    return events


def _payload_from_text(body_text: str, tracking_number: str) -> dict[str, Any]:
    lines = _clean_lines(body_text)
    status = _extract_status(lines)
    summary = _extract_summary(lines, status)
    events = _extract_events(lines)
    if not events and status:
        events = [
            {
                "eventTimestamp": utcnow().isoformat(),
                "eventType": status,
                "eventCode": status.lower().replace(" ", "_")[:40],
                "eventDescription": summary or status,
                "rawText": [status, summary] if summary else [status],
            }
        ]

    return {
        "trackingNumber": tracking_number,
        "status": status or "Tracking details available on USPS website",
        "statusCategory": _status_category(status),
        "statusSummary": summary or status or "USPS tracking details loaded from website",
        "trackingEvents": events,
        "rawText": lines,
        "source": "usps_web_tracking",
    }


def _normalize_payload(payload: dict[str, Any], tracking_number: str, body_text: str) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault("trackingNumber", tracking_number)
    normalized.setdefault("status", _extract_status(_clean_lines(body_text)))
    normalized.setdefault("statusCategory", _status_category(normalized.get("status")))
    normalized.setdefault("statusSummary", normalized.get("status"))
    normalized.setdefault("trackingEvents", [])
    normalized["source"] = "usps_web_tracking"
    normalized["rawText"] = _clean_lines(body_text)
    return normalized


def fetch_tracking_detail(tracking_number: str) -> dict[str, Any]:
    driver: WebDriver | None = None
    try:
        driver = _build_driver()
        _submit_tracking_number(driver, tracking_number)
        _wait_for_results(driver, tracking_number)
        body_text = driver.find_element(By.TAG_NAME, "body").text
        lowered = body_text.lower()
        if "access denied" in lowered or "forbidden" in lowered:
            raise USPSAccessRestrictedError(
                "USPS blocked the tracking page request in the browser session.",
                payload={"bodyText": body_text},
            )

        for candidate in _extract_json_candidates(driver.page_source, tracking_number):
            payload = _walk_for_tracking_payload(candidate, tracking_number)
            if payload:
                return _normalize_payload(payload, tracking_number, body_text)

        return _payload_from_text(body_text, tracking_number)
    except USPSConfigurationError:
        raise
    except USPSRequestError:
        raise
    except USPSAccessRestrictedError:
        raise
    except Exception as exc:
        raise USPSRequestError(f"USPS web tracking failed: {exc}") from exc
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def _mark_access_restricted(shipment: Shipment, exc: USPSAccessRestrictedError) -> None:
    shipment.official_tracking_url = official_tracking_url(shipment.tracking_number)
    shipment.last_synced_at = utcnow()
    shipment.status = "USPS page blocked"
    shipment.status_category = "Authorization"
    shipment.status_summary = str(exc)
    shipment.latest_payload = exc.payload or {"error": {"message": str(exc)}}


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
        "source": "usps_web_tracking",
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
