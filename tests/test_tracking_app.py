from app.services.carriers import normalize_carrier, official_tracking_url


def test_official_tracking_url() -> None:
    assert official_tracking_url("USPS", "9400111202555425055555").startswith(
        "https://tools.usps.com/go/TrackConfirmAction?tLabels="
    )


def test_normalize_carrier() -> None:
    assert normalize_carrier("usps") == "USPS"
