# Project Vienna — GG Signal Sourcing & Scoring Dashboard

A Streamlit dashboard for screening companies: it sources public signals
(official statistics, registries, news, job postings, review sites, etc.),
scores them into indicators, and surfaces pain points and valuations.

## Repository map

| Path | Purpose |
|---|---|
| `app.py` | Streamlit entry point and navigation |
| `config.py` | Environment/configuration (with `.env.example` as template) |
| `models.py`, `database.py` | SQLAlchemy models and session management |
| `company_service.py` | Core orchestration: crawl planning, company CRUD, CSV/data import |
| `indicators.py`, `painpoints.py`, `scoring.py`, `calibration.py` | Indicator, pain-point and scoring logic |
| `valuation.py`, `valuation_data.py`, `valuation_models.py`, `valuation_service.py` | Valuation pipeline |
| `adapters/` | Data-source adapters (Eurostat, Destatis, EPO, EU funding, Google News RSS, ...) |
| `scrapers/` | Website crawlers/scrapers (registry, news, jobs, reviews, product catalog, ...) |
| `views/` | Streamlit pages |
| `worker_hub.py`, `worker_installer.py`, `worker/`, `worker_shim/`, `worker_dist/` | Distributed Node.js crawler worker: hub, installer, launchers, and the committed build bundle |
| `crawl_jobs.py`, `resource_governor.py` | Crawl job queue and rate limiting |
| `aida_import.py`, `data_repairs.py`, `seed.py` | Data import, repair and demo seeding |
| `scripts/` | Developer utilities (bundle builder, catalog generator, benchmark, repairs) |
| `tests/` | Pytest suite (network-free) |
| `docs/` | Reference docs, incl. `LinkedIn_Extraction_Gem_Prompt.md` (manual LinkedIn CSV-import pipeline) |

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in credentials/secrets
streamlit run app.py
```

## Testing

```bash
python -m pytest tests/
```

## Worker bundle

`worker_dist/vienna-crawler-bundle.zip` is committed on purpose (the
Crawler Setup page hands it out to helper machines). After changing a
crawler or `worker/worker.mjs`, rebuild and commit it:

```bash
python scripts/build_worker_bundle.py
```
