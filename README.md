# 📦 TrackApp

A self-hosted **USPS shipment tracking dashboard** built with FastAPI. Add custom names to tracking numbers, bulk-refresh statuses, and auto-archive delivered packages — all running locally via Docker.

---

## ✨ Features

| Feature | Description |
|---|---|
| **Manual naming** | Assign a human-readable label to any USPS tracking number |
| **Edit & delete** | Update or remove shipment rows directly from the dashboard |
| **Bulk refresh** | Select multiple rows and refresh all their statuses in one click |
| **Archive tab** | Delivered shipments older than 10 days are moved automatically |
| **Official links** | Each row links directly to the USPS tracking page |
| **Email notifications** | Background worker sends status-change emails via Mailpit (dev) |

---

## 🏗️ Architecture

### System Overview

> All components, their responsibilities, and how they communicate.

![System Architecture](./docs/arch-system-overview.svg)

### Request Flow — Bulk Refresh

> End-to-end sequence: user triggers a refresh → worker polls USPS → database updated → dashboard re-renders.

![Bulk Refresh Flow](./docs/arch-refresh-flow.svg)

### Docker Compose Services

> Service dependency order, exposed ports, and volume mounts.

![Docker Services](./docs/arch-docker-services.svg)

> **Diagram files** are located in [`/docs`](./docs). They are standard SVG and can be opened in any browser or vector editor.

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| **Web framework** | [FastAPI](https://fastapi.tiangolo.com/) 0.115 |
| **Server** | Uvicorn (ASGI) |
| **ORM** | SQLAlchemy 2.0 |
| **Database** | PostgreSQL 16 |
| **DB Driver** | psycopg 3 (binary) |
| **Templates** | Jinja2 |
| **HTTP client** | HTTPX |
| **Scheduler** | APScheduler 3 |
| **Config** | pydantic-settings |
| **Dev email** | Mailpit |
| **Containerization** | Docker Compose |
| **Testing** | pytest |

---

## 🚀 Getting Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- USPS API credentials (Client ID + Client Secret)

### 1. Clone the repo

```bash
git clone https://github.com/pramodmuppala/TrackApp.git
cd TrackApp
```

### 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in your USPS credentials:

```env
USPS_CLIENT_ID=your_client_id_here
USPS_CLIENT_SECRET=your_client_secret_here
```

### 3. Start the app

```bash
# First run or switching from an older multi-carrier build:
docker compose down -v

docker compose up --build
```

Open **http://localhost:8000** in your browser.

### 4. View test emails (optional)

The Mailpit web UI is available at **http://localhost:8025** — all outbound emails from the app land here during development.

---

## ⚙️ Configuration

| Variable | Description |
|---|---|
| `USPS_CLIENT_ID` | Your USPS Web Tools / OAuth2 Client ID |
| `USPS_CLIENT_SECRET` | Your USPS OAuth2 Client Secret |

Additional settings (database URL, SMTP host, etc.) can be found in `.env.example`.

---

## 📁 Project Structure

```
TrackApp/
├── docs/                 # Architecture diagrams (SVG)
│   ├── arch-system-overview.svg
│   ├── arch-refresh-flow.svg
│   └── arch-docker-services.svg
├── app/                  # Application source code
│   ├── main.py           # FastAPI app entry point & routes
│   ├── models.py         # SQLAlchemy ORM models
│   ├── schemas.py        # Pydantic request/response schemas
│   ├── usps.py           # USPS API client (OAuth2 + tracking)
│   ├── worker.py         # APScheduler background job
│   └── templates/        # Jinja2 HTML templates
├── tests/                # pytest test suite
├── .env.example          # Environment variable template
├── docker-compose.yml    # Multi-service Docker setup
├── Dockerfile            # App container definition
├── requirements.txt      # Python dependencies
└── pytest.ini            # pytest configuration
```

---

## 🧪 Running Tests

```bash
# Inside the container
docker compose exec web pytest

# Or locally with a virtual environment
pip install -r requirements.txt
pytest
```

---

## 📝 Notes

- This dashboard is **USPS-only**. Existing FedEx / UPS rows from older multi-carrier builds are ignored.
- If you are migrating from an older build, run `docker compose down -v` once to reset the database volume before starting.
- The background worker polls USPS automatically on a schedule — no manual refresh is required, though bulk refresh is available on demand.

---

## 📄 License

This project does not currently specify a license. Contact the author for usage terms.
