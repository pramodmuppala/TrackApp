from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from app.models import Shipment

SUPPORTED_CARRIERS = ("USPS", "FedEx", "UPS")


class CarrierConfigurationError(RuntimeError):
    pass


class CarrierRequestError(RuntimeError):
    pass


class CarrierAccessRestrictedError(CarrierRequestError):
    pass


def normalize_carrier(value: str | None) -> str:
    raw = (value or "USPS").strip().lower()
    if raw in {"usps", "postal", "postal service"}:
        return "USPS"
    if raw in {"fedex", "federal express"}:
        return "FedEx"
    if raw in {"ups", "united parcel service"}:
        return "UPS"
    raise ValueError("Unsupported carrier")


def official_tracking_url(carrier: str, tracking_number: str) -> str:
    normalized = normalize_carrier(carrier)
    if normalized == "USPS":
        return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}"
    if normalized == "FedEx":
        return f"https://www.fedex.com/fedextrack/?trknbr={tracking_number}"
    return f"https://www.ups.com/track?tracknum={tracking_number}"


def sync_shipment_tracking(db: Session, shipment: Shipment) -> Shipment:
    normalized = normalize_carrier(shipment.carrier)
    shipment.carrier = normalized
    if normalized == "USPS":
        from . import usps
        try:
            return usps.sync_shipment_tracking(db, shipment)
        except usps.USPSConfigurationError as exc:
            raise CarrierConfigurationError(str(exc)) from exc
        except usps.USPSAccessRestrictedError as exc:
            raise CarrierAccessRestrictedError(str(exc)) from exc
        except usps.USPSRequestError as exc:
            raise CarrierRequestError(str(exc)) from exc
    if normalized == "FedEx":
        from . import fedex
        try:
            return fedex.sync_shipment_tracking(db, shipment)
        except fedex.FedExConfigurationError as exc:
            raise CarrierConfigurationError(str(exc)) from exc
        except fedex.FedExRequestError as exc:
            raise CarrierRequestError(str(exc)) from exc
    from . import ups
    try:
        return ups.sync_shipment_tracking(db, shipment)
    except ups.UPSConfigurationError as exc:
        raise CarrierConfigurationError(str(exc)) from exc
    except ups.UPSRequestError as exc:
        raise CarrierRequestError(str(exc)) from exc
