from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import Base, engine, session_scope
from app.models import Shipment, utcnow
from app.services.carriers import CarrierConfigurationError, CarrierRequestError, sync_shipment_tracking

settings = get_settings()


def run_worker() -> None:
    Base.metadata.create_all(bind=engine)
    print("Worker started. Polling for stale shipments...")

    while True:
        with session_scope() as db:
            stale_before = utcnow() - timedelta(minutes=settings.tracking_refresh_minutes)
            shipments = list(
                db.scalars(
                    select(Shipment).where(
                        Shipment.carrier == "USPS",
                        Shipment.last_synced_at.is_(None) | (Shipment.last_synced_at < stale_before)
                    )
                ).all()
            )
            for shipment in shipments:
                try:
                    sync_shipment_tracking(db, shipment)
                    print(f"Refreshed USPS {shipment.tracking_number}")
                except CarrierConfigurationError as exc:
                    print(f"Skipped USPS {shipment.tracking_number}: {exc}")
                except CarrierRequestError as exc:
                    print(f"Refresh failed for USPS {shipment.tracking_number}: {exc}")

        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    run_worker()
