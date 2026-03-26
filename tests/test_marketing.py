from app.models import Customer
from app.services.marketing import export_marketing_csv_rows


def test_export_marketing_csv_rows():
    customer = Customer(
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        company="Acme",
        phone="123",
        marketing_source="web",
        marketing_opt_in=True,
    )
    rows = export_marketing_csv_rows([customer])
    assert rows[0][0] == "first_name"
    assert rows[1][2] == "jane@example.com"
