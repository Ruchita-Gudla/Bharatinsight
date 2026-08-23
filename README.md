# BharatInsight

> A complete analyst workflow — raw relational data to a boardroom-ready
> dashboard — built on a single real Indian food-delivery dataset, free of
> cost, start to finish.

**Stack:** PostgreSQL 16 · Python 3.11 · SQLAlchemy · Power BI · Streamlit · Docker
**Dataset:** [Zomato Database (Kaggle)](https://www.kaggle.com/datasets/anas123siddiqui/zomato-database) — 1 dataset, 5 real relational tables

### Dataset disclosure (read first)

> This project uses the public Zomato Database dataset (Kaggle). Personal
> identifiers (name, email, password) are anonymized by the dataset source;
> all transactional, restaurant, menu, and order data is real.

The dataset has **no free-text review column**, so sentiment analysis is
intentionally omitted in favor of Market Basket Analysis on real order
co-occurrence (Phase 3). Single dataset, no synthetic rows, no invented
order history — every number in the final deliverables traces back to a
real query result.

---

## Phase 0 — Environment & Data Foundation (this phase)

Phase 0 has no analysis yet — it's the skeleton every later phase depends on:
verify the real CSV columns, stand up PostgreSQL, load all 5 tables, and
confirm every foreign key actually joins.

| File | Purpose |
|---|---|
| `scripts/verify_schema.py` | Profiles every CSV in `data/raw/` (shape, dtypes, head, nulls) and writes `schema_report.md` — run **first** |
| `sql/01_schema.sql` | `CREATE TABLE` for users, restaurant, menu, food, orders + PK/FK constraints + indexes, idempotent (`IF NOT EXISTS`) |
| `scripts/load_data.py` | Loads CSVs → Postgres via SQLAlchemy in FK-safe order, validates row counts (fails loudly), runs join-integrity checks |
| `docker-compose.yml` | PostgreSQL 16 service, named volume, port 5432, schema auto-applied on first start |
| `.env.example` | `DATABASE_URL` + Postgres config template — no real credentials |

### Project structure

```
bharatinsight/
├── .vscode/                  # VS Code settings + recommended extensions
├── data/
│   ├── raw/                  # original Kaggle CSVs — never edited in place
│   └── processed/            # cleaned/exported data (later phases)
├── scripts/
│   ├── verify_schema.py      # Phase 0, step 1: profile CSVs → schema_report.md
│   └── load_data.py          # Phase 0, step 2: CSV → PostgreSQL with validation
├── sql/
│   └── 01_schema.sql         # tables + FKs + indexes (re-runnable)
├── excel/                    # Phase 1
├── python/                   # Phase 3 notebooks + outputs
├── powerbi/                  # Phase 4
├── models/                   # Phase 5 (saved churn model)
├── docker-compose.yml
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Quick start (Windows / PowerShell)

### 1. Prerequisites

- Python 3.11 (`python --version`)
- **Either** Docker Desktop **or** a local PostgreSQL 16 install
- The Kaggle dataset extracted into `data/raw/` (see `data/raw/README.md`)

### 2. Environment

```powershell
cd bharatinsight
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env     # then edit .env: set your real password in BOTH
                           # DATABASE_URL and POSTGRES_PASSWORD
```

### 3. Verify the real schema — always first

```powershell
python scripts/verify_schema.py
```

Open the generated **`schema_report.md`** and confirm the actual column
names. The blueprint's column names are a best-effort reconstruction from
public documentation — if yours differ, add the real header to the matching
alias list in `TABLE_SPECS` at the top of `scripts/load_data.py` (the loader
normalizes CSV headers to the canonical schema; you should not need to touch
`sql/01_schema.sql`).

### 4. Start PostgreSQL — pick ONE path

**Path A — Docker (recommended, portable):**

```powershell
docker-compose up -d postgres
# schema auto-applied on first start; wait for the healthcheck:
docker-compose ps
```

**Path B — local PostgreSQL 16 install:**

```powershell
psql -U postgres -c "CREATE DATABASE bharatinsight_db;"
psql -U postgres -d bharatinsight_db -f sql/01_schema.sql
```

### 5. Load the data

```powershell
python scripts/load_data.py
# re-runnable refresh (truncate + reload):
python scripts/load_data.py --reset
```

The loader prints, per table: CSV rows → cleaned → inserted → DB count
(raises + exits non-zero on any mismatch), then four FK orphan checks
(orders→users, orders→restaurant, menu→restaurant, menu→food).
Duplicates/NULL keys and FK-orphan rows are **quarantined** to
`data/processed/quarantine/` and logged — those findings are legitimate
Phase 1 data-quality material, keep them.

### 6. Verify

```powershell
psql -U postgres -d bharatinsight_db -c "\dt" -c "SELECT COUNT(*) FROM orders;"
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `psql: command not found` | PostgreSQL `bin` folder not in PATH — re-run installer or add `C:\Program Files\PostgreSQL\16\bin` |
| `verify_schema.py` reports missing files | CSVs not in `data/raw/` or misnamed — expected names are in `data/raw/README.md` |
| `load_data.py`: *"Could not map these canonical columns"* | Real CSV header differs from docs — add it to the alias list in `TABLE_SPECS` in `scripts/load_data.py` |
| `load_data.py`: *"Target tables are not empty"* | Re-run with `--reset` (loader refuses to double-append on purpose) |
| Row-count mismatch | Likely a header/encoding quirk — open the CSV with the rainbow-csv VS Code extension and check for stray commas/quotes |
| Can't connect from scripts | `DATABASE_URL` must use host `localhost` on your machine; `postgres` is only the host *inside* the Docker network |
| Container starts but tables missing | The init script runs only on first boot with an empty volume — `docker-compose down -v && docker-compose up -d postgres` |

---

## Roadmap

| Phase | Focus | Status |
|---|---|---|
| **0** | Environment & data foundation | **this phase** |
| 1 | Excel foundations — profiling, pivots, 1-page dashboard | next |
| 2 | SQL mastery — 50+ business queries | pending |
| 3 | Python — EDA, market basket, RFM+KMeans, churn (XGBoost), forecast (Prophet) | pending |
| 4 | Power BI — 5-page dashboard with DAX measures | pending |
| 5 | Deployment — Streamlit app, Docker, GitHub, portfolio | pending |

## License

MIT — dataset remains under its original Kaggle terms; see the disclosure
note above.
