import pytest

from app.models import Shipment
from app.services.carriers import normalize_carrier, official_tracking_url
from app.services.usps import USPSAccessRestrictedError, fetch_tracking_detail, sync_shipment_tracking


def test_official_tracking_url() -> None:
    assert official_tracking_url("USPS", "9400111202555425055555").startswith(
        "https://tools.usps.com/go/TrackConfirmAction?tLabels="
    )


def test_normalize_carrier() -> None:
    assert normalize_carrier("usps") == "USPS"


def test_usps_fetch_tracking_detail_raises_access_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 403
        text = '{"error":{"code":"403"}}'

        def json(self) -> dict:
            return {
                "apiVersion": "/tracking/v3",
                "error": {
                    "code": "403",
                    "message": (
                        "The requested MID is not authorized to access /tracking/9400. "
                        "USPS implemented Tracking API Access Controls 4/1/2026. "
                        "If you still require access submit an IP Agreement inquiry via Email Us."
                    ),
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("app.services.usps._get_token", lambda: "token")
    monkeypatch.setattr("app.services.usps.httpx.Client", FakeClient)

    with pytest.raises(USPSAccessRestrictedError):
        fetch_tracking_detail("9400111202555425055555")


def test_usps_sync_marks_shipment_access_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    shipment = Shipment(tracking_number="9400111202555425055555", carrier="USPS")

    def raise_access_restricted(tracking_number: str) -> dict:
        raise USPSAccessRestrictedError(
            "USPS access restricted for this tracking number. Submit the USPS IP Agreement inquiry to re-enable API tracking.",
            payload={"error": {"code": "403"}},
        )

    class FakeSession:
        def add(self, obj) -> None:
            pass

        def flush(self) -> None:
            pass

    monkeypatch.setattr("app.services.usps.fetch_tracking_detail", raise_access_restricted)

    with pytest.raises(USPSAccessRestrictedError):
        sync_shipment_tracking(FakeSession(), shipment)

    assert shipment.status == "USPS access restricted"
    assert shipment.status_category == "Authorization"
    assert "IP Agreement inquiry" in (shipment.status_summary or "")
    assert shipment.last_synced_at is not None
    assert shipment.official_tracking_url
