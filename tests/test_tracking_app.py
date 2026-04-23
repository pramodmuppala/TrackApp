import pytest

from app.models import Shipment
from app.services.carriers import normalize_carrier, official_tracking_url
from app.services.usps import (
    SessionNotCreatedException,
    USPSAccessRestrictedError,
    USPSConfigurationError,
    _build_driver,
    fetch_tracking_detail,
    sync_shipment_tracking,
)


def test_official_tracking_url() -> None:
    assert official_tracking_url("USPS", "9400111202555425055555").startswith(
        "https://tools.usps.com/go/TrackConfirmAction?tLabels="
    )


def test_normalize_carrier() -> None:
    assert normalize_carrier("usps") == "USPS"


def test_build_driver_requires_remote_automation(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSafari:
        def __init__(self, *args, **kwargs):
            raise SessionNotCreatedException("remote automation disabled")

    monkeypatch.setattr("app.services.usps.webdriver.Safari", FakeSafari)

    with pytest.raises(USPSConfigurationError):
        _build_driver()


def test_usps_fetch_tracking_detail_uses_page_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeElement:
        def __init__(self, text: str = ""):
            self.text = text

        def clear(self) -> None:
            pass

        def send_keys(self, value: str) -> None:
            self.text = value

        def click(self) -> None:
            pass

    class FakeDriver:
        page_source = "<html><body>No embedded JSON</body></html>"

        def __init__(self, *args, **kwargs):
            self.body = FakeElement(
                "\n".join(
                    [
                        "USPS Tracking",
                        "Tracking Number",
                        "9400111202555425055555",
                        "Latest Update",
                        "In Transit to Next Facility, Arriving Late",
                        "Your package will arrive later than expected, but is still on its way.",
                        "Tracking History",
                        "April 23, 2026, 8:14 pm",
                        "Arrived at USPS Regional Facility",
                        "JERSEY CITY NJ NETWORK DISTRIBUTION CENTER",
                    ]
                )
            )
            self.input = FakeElement()
            self.button = FakeElement()

        def get(self, url: str) -> None:
            self.url = url

        def find_element(self, by, value):
            if value == "body":
                return self.body
            if "submit" in value or "track" in value:
                return self.button
            return self.input

        def quit(self) -> None:
            pass

    class FakeWait:
        def __init__(self, driver, timeout):
            self.driver = driver
            self.timeout = timeout

        def until(self, condition):
            assert condition(self.driver)
            return True

    monkeypatch.setattr("app.services.usps.webdriver.Safari", FakeDriver)
    monkeypatch.setattr("app.services.usps.WebDriverWait", FakeWait)

    payload = fetch_tracking_detail("9400111202555425055555")

    assert payload["status"] == "In Transit to Next Facility, Arriving Late"
    assert payload["statusCategory"] == "In Transit"
    assert payload["trackingEvents"]
    assert payload["trackingEvents"][0]["eventType"] == "Arrived at USPS Regional Facility"


def test_usps_sync_marks_shipment_access_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    shipment = Shipment(tracking_number="9400111202555425055555", carrier="USPS")

    def raise_access_restricted(tracking_number: str) -> dict:
        raise USPSAccessRestrictedError(
            "USPS blocked the tracking page request in the browser session.",
            payload={"error": {"code": "blocked"}},
        )

    class FakeSession:
        def add(self, obj) -> None:
            pass

        def flush(self) -> None:
            pass

    monkeypatch.setattr("app.services.usps.fetch_tracking_detail", raise_access_restricted)

    with pytest.raises(USPSAccessRestrictedError):
        sync_shipment_tracking(FakeSession(), shipment)

    assert shipment.status == "USPS page blocked"
    assert shipment.status_category == "Authorization"
    assert "browser session" in (shipment.status_summary or "")
    assert shipment.last_synced_at is not None
    assert shipment.official_tracking_url
