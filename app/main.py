from __future__ import annotations

from datetime import timedelta
import re
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import Base, engine, get_db
from app.models import RecipientProfile, Shipment, TrackingEvent, utcnow
from app.services.carriers import (
    CarrierAccessRestrictedError,
    CarrierConfigurationError,
    CarrierRequestError,
    official_tracking_url,
    sync_shipment_tracking,
)

settings = get_settings()
app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
TRACKING_RE = re.compile(r"^[A-Za-z0-9-]{8,34}$")
ARCHIVE_AFTER_DAYS = 10
PURGE_AFTER_DAYS = 30


def shipment_is_archived(shipment: Shipment) -> bool:
    status = (shipment.status or "").lower()
    if "deliver" not in status:
        return False
    cutoff = utcnow() - timedelta(days=ARCHIVE_AFTER_DAYS)
    reference_time = shipment.last_event_at or shipment.updated_at or shipment.created_at
    return bool(reference_time and reference_time <= cutoff)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


def dashboard_redirect(message: str, tab: str = "active", edit_id: int | None = None) -> RedirectResponse:
    params: dict[str, str] = {"message": message, "tab": tab if tab in ("active", "archive", "delivered") else "active"}
    if edit_id is not None:
        params["edit_id"] = str(edit_id)
    return RedirectResponse(url=f"/?{urlencode(params)}", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    message: str | None = None,
    tab: str = "active",
    edit_id: int | None = None,
):
    all_shipments = list(
        db.scalars(
            select(Shipment)
            .options(joinedload(Shipment.recipient), joinedload(Shipment.events))
            .where(Shipment.carrier == "USPS")
            .order_by(Shipment.updated_at.desc())
            .limit(250)
        ).unique()
    )

    archived_shipments = [shipment for shipment in all_shipments if shipment_is_archived(shipment)]
    delivered_shipments = [
        shipment
        for shipment in all_shipments
        if (shipment.status or "") and "deliver" in (shipment.status or "").lower() and not shipment_is_archived(shipment)
    ]
    active_shipments = [
        shipment
        for shipment in all_shipments
        if shipment not in archived_shipments and shipment not in delivered_shipments
    ]
    active_tab = tab if tab in ("active", "archive", "delivered") else "active"
    if active_tab == "archive":
        visible_shipments = archived_shipments
    elif active_tab == "delivered":
        visible_shipments = delivered_shipments
    else:
        visible_shipments = active_shipments

    editing_shipment = db.get(Shipment, edit_id) if edit_id else None

    metrics = {
        "shipment_count": db.scalar(select(func.count(Shipment.id))) or 0,
        "recipient_count": db.scalar(select(func.count(RecipientProfile.id))) or 0,
        "event_count": db.scalar(select(func.count(TrackingEvent.id))) or 0,
        "stale_shipments": db.scalar(
            select(func.count(Shipment.id)).where(
                Shipment.last_synced_at.is_(None)
                | (Shipment.last_synced_at < utcnow() - timedelta(minutes=settings.tracking_refresh_minutes))
            )
        )
        or 0,
        "archived_count": len(archived_shipments),
        "active_count": len(active_shipments),
        "delivered_count": len(delivered_shipments),
    }
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "message": message,
            "shipments": visible_shipments,
            "metrics": metrics,
            "settings": settings,
            "active_tab": active_tab,
            "archive_after_days": ARCHIVE_AFTER_DAYS,
            "editing_shipment": editing_shipment,
        },
    )


@app.post("/shipments")
def create_shipment(
    tracking_number: str = Form(...),
    name: str = Form(""),
    sync_now: bool = Form(False),
    db: Session = Depends(get_db),
):
    normalized_tracking = tracking_number.strip().replace(" ", "")
    if not TRACKING_RE.match(normalized_tracking):
        return dashboard_redirect("Tracking number format looks invalid")

    normalized_carrier = "USPS"

    existing = db.scalar(
        select(Shipment).where(
            Shipment.tracking_number == normalized_tracking,
            Shipment.carrier == normalized_carrier,
        )
    )
    if existing:
        return dashboard_redirect("Tracking number already exists")

    shipment = Shipment(
        tracking_number=normalized_tracking,
        carrier=normalized_carrier,
        reference=name.strip() or None,
        official_tracking_url=official_tracking_url(normalized_carrier, normalized_tracking),
    )
    db.add(shipment)
    db.commit()
    db.refresh(shipment)

    message = "Shipment created"
    if sync_now:
        try:
            sync_shipment_tracking(db, shipment)
            db.commit()
            message = "Shipment created and synced"
        except CarrierConfigurationError as exc:
            db.commit()
            message = f"Shipment saved. {str(exc)[:120]}"
        except CarrierAccessRestrictedError as exc:
            db.commit()
            message = f"Shipment saved. {str(exc)[:120]}"
        except CarrierRequestError as exc:
            db.commit()
            message = f"Shipment saved. USPS sync failed: {str(exc)[:120]}"

    return dashboard_redirect(message)


@app.post("/shipments/{shipment_id}/edit")
def update_shipment(
    shipment_id: int,
    name: str = Form(""),
    tracking_number: str = Form(...),
    tab: str = Form("active"),
    db: Session = Depends(get_db),
):
    shipment = db.get(Shipment, shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    normalized_tracking = tracking_number.strip().replace(" ", "")
    if not TRACKING_RE.match(normalized_tracking):
        return dashboard_redirect("Tracking number format looks invalid", tab=tab, edit_id=shipment_id)

    normalized_carrier = "USPS"

    duplicate = db.scalar(
        select(Shipment).where(
            Shipment.tracking_number == normalized_tracking,
            Shipment.carrier == normalized_carrier,
            Shipment.id != shipment_id,
        )
    )
    if duplicate:
        return dashboard_redirect("Tracking number already exists", tab=tab, edit_id=shipment_id)

    shipment.reference = name.strip() or None
    shipment.tracking_number = normalized_tracking
    shipment.carrier = normalized_carrier
    shipment.official_tracking_url = official_tracking_url(normalized_carrier, normalized_tracking)
    db.add(shipment)
    db.commit()
    return dashboard_redirect("Shipment updated", tab=tab)


@app.post("/shipments/{shipment_id}/delete")
def delete_shipment(
    shipment_id: int,
    tab: str = Form("active"),
    db: Session = Depends(get_db),
):
    shipment = db.get(Shipment, shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")
    db.delete(shipment)
    db.commit()
    return dashboard_redirect("Shipment deleted", tab=tab)


@app.post("/shipments/{shipment_id}/archive")
def archive_shipment(
    shipment_id: int,
    tab: str = Form("active"),
    db: Session = Depends(get_db),
):
    """Mark a delivered shipment as archived (manual confirmation).

    This sets the shipment's last_event_at to a time older than the archive cutoff
    so `shipment_is_archived` will return True without adding a new DB column.
    """
    shipment = db.get(Shipment, shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    status = (shipment.status or "").lower()
    if "deliver" not in status and not shipment_is_archived(shipment):
        return dashboard_redirect("Only delivered shipments can be archived manually", tab=tab)

    # Set last_event_at to older than the archive cutoff so it is considered archived
    shipment.last_event_at = utcnow() - timedelta(days=ARCHIVE_AFTER_DAYS + 1)
    db.add(shipment)
    db.commit()
    return dashboard_redirect("Shipment moved to archive", tab="archive")


@app.post("/shipments/purge-archive")
def purge_archive(
    days: int = Form(PURGE_AFTER_DAYS),
    db: Session = Depends(get_db),
):
    """Delete USPS shipments that are delivered and older than `days` days.

    This is a manual purge action triggered from the Archive view.
    """
    cutoff = utcnow() - timedelta(days=days)
    # select shipments that are USPS, status contains 'deliver', and whose
    # last_event_at/updated_at/created_at is older than cutoff
    candidates = list(
        db.scalars(
            select(Shipment).where(
                Shipment.carrier == "USPS",
                func.lower(Shipment.status).like("%deliver%"),
                func.coalesce(Shipment.last_event_at, Shipment.updated_at, Shipment.created_at) <= cutoff,
            )
        ).all()
    )
    count = 0
    for s in candidates:
        db.delete(s)
        count += 1
    if count:
        db.commit()
    return dashboard_redirect(f"Purged {count} USPS archived shipment(s) older than {days} days", tab="archive")


@app.post("/shipments/{shipment_id}/refresh")
def refresh_shipment(
    shipment_id: int,
    tab: str = Form("active"),
    db: Session = Depends(get_db),
):
    shipment = db.get(Shipment, shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")
    # Do not poll or refresh shipments that are delivered or archived
    status = (shipment.status or "").lower()
    if "deliver" in status or shipment_is_archived(shipment):
        return dashboard_redirect("Delivered or archived shipments are not refreshed", tab=tab)

    try:
        sync_shipment_tracking(db, shipment)
        db.commit()
        message = "Shipment refreshed"
    except CarrierConfigurationError as exc:
        db.rollback()
        message = str(exc)[:120]
    except CarrierAccessRestrictedError as exc:
        db.commit()
        message = str(exc)[:120]
    except CarrierRequestError as exc:
        db.rollback()
        message = f"USPS refresh failed: {str(exc)[:120]}"
    return dashboard_redirect(message, tab=tab)


@app.post("/shipments/bulk-delete")
def bulk_delete_shipments(
    selected_ids: list[int] = Form(default=[]),
    tab: str = Form("archive"),
    db: Session = Depends(get_db),
):
    unique_ids = list(dict.fromkeys(selected_ids))
    if not unique_ids:
        return dashboard_redirect("Select at least one shipment to delete", tab=tab)

    shipments = list(db.scalars(select(Shipment).where(Shipment.id.in_(unique_ids))).all())
    if not shipments:
        return dashboard_redirect("No matching shipments found", tab=tab)

    deleted = 0
    for shipment in shipments:
        db.delete(shipment)
        deleted += 1
    db.commit()
    return dashboard_redirect(f"Deleted {deleted} shipment(s)", tab=tab)


@app.post("/shipments/bulk-archive")
def bulk_archive_shipments(
    selected_ids: list[int] = Form(default=[]),
    tab: str = Form("delivered"),
    db: Session = Depends(get_db),
):
    unique_ids = list(dict.fromkeys(selected_ids))
    if not unique_ids:
        return dashboard_redirect("Select at least one shipment to archive", tab=tab)

    shipments = list(db.scalars(select(Shipment).where(Shipment.id.in_(unique_ids))).all())
    if not shipments:
        return dashboard_redirect("No matching shipments found", tab=tab)

    archived = 0
    for shipment in shipments:
        status = (shipment.status or "").lower()
        # Only archive delivered shipments (or those already considered archived)
        if "deliver" not in status and not shipment_is_archived(shipment):
            continue
        shipment.last_event_at = utcnow() - timedelta(days=ARCHIVE_AFTER_DAYS + 1)
        db.add(shipment)
        archived += 1

    if archived:
        db.commit()
    return dashboard_redirect(f"Archived {archived} shipment(s)", tab="archive")


@app.post("/shipments/bulk-edit")
def bulk_edit_shipments(
    selected_ids: list[int] = Form(default=[]),
    tab: str = Form("active"),
    db: Session = Depends(get_db),
):
    """Redirect to edit the first selected shipment. This lets users pick a row via checkboxes
    and hit "Edit selected"; we open the edit form for the first selected id.
    """
    unique_ids = list(dict.fromkeys(selected_ids))
    if not unique_ids:
        return dashboard_redirect("Select at least one shipment to edit", tab=tab)

    first_id = unique_ids[0]
    # ensure exists
    shipment = db.get(Shipment, first_id)
    if not shipment:
        return dashboard_redirect("Selected shipment not found", tab=tab)

    return dashboard_redirect("Editing selected shipment", tab=tab, edit_id=first_id)


@app.post("/shipments/bulk-refresh")
def bulk_refresh_shipments(
    selected_ids: list[int] = Form(default=[]),
    tab: str = Form("active"),
    db: Session = Depends(get_db),
):
    unique_ids = list(dict.fromkeys(selected_ids))
    if not unique_ids:
        return dashboard_redirect("Select at least one shipment to refresh", tab=tab)

    shipments = list(db.scalars(select(Shipment).where(Shipment.id.in_(unique_ids))))
    if not shipments:
        return dashboard_redirect("No matching shipments found", tab=tab)

    refreshed = 0
    failed = 0
    skipped = 0
    blocked = 0
    configuration_errors: list[str] = []
    for shipment in shipments:
        # skip delivered or archived shipments
        status = (shipment.status or "").lower()
        if "deliver" in status or shipment_is_archived(shipment):
            skipped += 1
            continue
        try:
            sync_shipment_tracking(db, shipment)
            db.commit()
            refreshed += 1
        except CarrierConfigurationError as exc:
            db.rollback()
            configuration_errors.append(str(exc))
            failed += 1
        except CarrierAccessRestrictedError:
            db.commit()
            blocked += 1
        except CarrierRequestError:
            db.rollback()
            failed += 1

    if configuration_errors and not refreshed:
        return dashboard_redirect(configuration_errors[0][:120], tab=tab)
    if refreshed and (failed or blocked):
        message = f"Refreshed {refreshed} shipment(s). {blocked} blocked. {failed} failed. {skipped} skipped."
    elif refreshed:
        message = f"Refreshed {refreshed} shipment(s). {skipped} skipped."
    elif blocked and not failed:
        message = f"No shipments refreshed. {blocked} blocked by USPS access controls. {skipped} skipped."
    else:
        message = f"No shipments refreshed. {failed} failed. {skipped} skipped."
    return dashboard_redirect(message, tab=tab)


@app.get("/healthz", response_class=PlainTextResponse)
def healthcheck() -> str:
    return "ok"
