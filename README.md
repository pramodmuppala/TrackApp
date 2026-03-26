# TrackApp

USPS tracking dashboard for manually named shipments.

## What changed

- USPS-only dashboard
- Manual name entry stored in your database
- Edit and delete for each row
- Bulk refresh for selected USPS tracking numbers
- Archive tab for delivered shipments older than 10 days
- Official USPS tracking links per row

## Start with Docker

```bash
cp .env.example .env
docker compose down -v
docker compose up --build
```

Open `http://localhost:8000`

## Credentials

Fill in your USPS credentials in `.env`:

- `USPS_CLIENT_ID`
- `USPS_CLIENT_SECRET`

## Notes

- Existing FedEx and UPS rows from older builds are ignored by this USPS-only dashboard.
- If you are switching from an older multi-carrier build, run `docker compose down -v` once before starting.
