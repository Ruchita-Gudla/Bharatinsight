#!/usr/bin/env python3
"""
BharatInsight — Phase 0, Step 2: Database Loader
=================================================

Loads the 5 Kaggle CSVs from ``data/raw/`` into PostgreSQL in FK-safe order
(users → restaurant → food → menu → orders) using SQLAlchemy + pandas.to_sql.

Verified against the ACTUAL downloaded files (Phase 0 findings)
---------------------------------------------------------------
* IDs are prefixed string codes: food.f_id = "fd123", menu.menu_id = "mn45" →
  parsed to integers by a 3-pass parser (plain → prefixed code → "50+ /
  1K+ ratings" band strings), every recovery logged.
* menu.csv has NO row-level key: menu_id is a per-restaurant menu code, not
  unique per row → the loader dedupes on the composite (restaurant_id,
  food_id), which is the menu table's real primary key.
* orders.csv has NO order_id column → the table uses a surrogate
  BIGINT IDENTITY primary key assigned by PostgreSQL; the loader simply does
  not insert that column. This is documented in sql/01_schema.sql.
* orders.r_id is float-formatted ("567335.0"); negative sales_amount rows
  exist (refunds/adjustments) and are kept + counted for Phase 1.

What it does, in order
----------------------
1. Reads ``DATABASE_URL`` from ``.env`` (via python-dotenv) or ``--database-url``.
2. Normalises each CSV's headers and maps them to the canonical schema
   columns using the alias lists in ``TABLE_SPECS`` below.
3. Cleans types (multi-pass integer parsing, junk→NULL coercion), strips
   whitespace, de-duplicates primary keys (loudly), quarantines FK orphans
   to ``data/processed/quarantine/``.
4. Refuses to append into non-empty tables unless ``--reset`` is passed
   (TRUNCATE ... RESTART IDENTITY CASCADE) — no accidental double loads.
5. Post-load: per-table row-count validation (fails loudly on mismatch) and
   four FK join-integrity checks (all should print 0 orphans).

Prerequisites
-------------
    psql -U postgres -d bharatinsight_db -f sql/01_schema.sql

Usage
-----
    python scripts/load_data.py             # load into empty tables
    python scripts/load_data.py --reset     # truncate + reload (re-runnable)
    python scripts/load_data.py --strict    # also fail if any cleaning happened
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
QUARANTINE_DIR = PROJECT_ROOT / "data" / "processed" / "quarantine"

CSV_ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "latin-1")
INSERT_CHUNKSIZE = 1000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("bharatinsight.load")


# ---------------------------------------------------------------------------
# Table specifications
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TableSpec:
    """One table's load contract.

    columns: canonical DB column -> accepted CSV header aliases (all compared
    after lower-casing + stripping). FIRST matching alias in the CSV wins.
    pk_columns: source-backed column(s) used for de-duplication. An empty
    tuple means the table's PK is surrogate (DB-assigned IDENTITY), so no
    de-duplication applies.
    """

    csv_name: str
    pk_columns: tuple[str, ...]
    columns: dict[str, tuple[str, ...]]
    integer_columns: tuple[str, ...] = ()
    numeric_columns: tuple[str, ...] = ()
    datetime_columns: tuple[str, ...] = ()
    # (column, parent_table, parent_column) used for orphan quarantine
    foreign_keys: tuple[tuple[str, str, str], ...] = ()


TABLE_SPECS: dict[str, TableSpec] = {
    "users": TableSpec(
        csv_name="users.csv",
        pk_columns=("user_id",),
        columns={
            "user_id": ("user_id", "id"),
            "name": ("name", "user_name", "full_name"),
            "email": ("email", "email_id"),
            "password": ("password", "pass"),
            "age": ("age",),
            "gender": ("gender", "sex"),
            "marital_status": ("marital_status", "marital"),
            "occupation": ("occupation",),
            "monthly_income": ("monthly_income", "income", "monthly_income_rs"),
            "educational_qualifications": (
                "educational_qualifications",
                "educational_qualification",
                "education",
                "education_qualifications",
            ),
            "family_size": ("family_size", "family_size_members"),
        },
        integer_columns=("user_id", "age", "family_size"),
    ),
    "restaurant": TableSpec(
        csv_name="restaurant.csv",
        pk_columns=("restaurant_id",),
        columns={
            "restaurant_id": ("restaurant_id", "id", "r_id", "res_id"),
            "name": ("name", "restaurant_name"),
            "location": ("location", "city", "area"),
            "rating": ("rating", "rate", "aggregate_rating"),
            "num_ratings": ("num_ratings", "rating_count", "num_rating", "votes"),
            "cost_for_two": (
                "cost_for_two",
                "cost",
                "approx_cost_for_two",
                "approx_cost_for_two_people",
                "cost_for_two_people",
            ),
            "cuisine": ("cuisine", "cuisines"),
            "license_number": ("license_number", "lic_no", "license_no", "licence_number", "license"),
            "website_link": ("website_link", "link", "website", "url"),
            "address": ("address",),
        },
        integer_columns=("restaurant_id", "num_ratings"),
        numeric_columns=("rating", "cost_for_two"),
    ),
    "food": TableSpec(
        csv_name="food.csv",
        pk_columns=("food_id",),
        columns={
            # real IDs look like "fd123" — parsed to int by the 3-pass parser
            "food_id": ("food_id", "f_id", "id"),
            "name": ("name", "item", "food_item", "food_name"),
            "veg_or_non_veg": ("veg_or_non_veg", "veg_non_veg", "veg_or_nonveg", "type"),
        },
        integer_columns=("food_id",),
    ),
    "menu": TableSpec(
        csv_name="menu.csv",
        # menu_id is a per-restaurant menu code (NOT unique per row — e.g.
        # "mn0" repeats for every item of one restaurant) → the honest row
        # key is the composite (restaurant_id, food_id), matching the schema.
        pk_columns=("restaurant_id", "food_id"),
        columns={
            "menu_id": ("menu_id", "mn_id"),
            "restaurant_id": ("restaurant_id", "r_id", "res_id"),
            "food_id": ("food_id", "f_id"),
            "cuisine": ("cuisine", "cuisines"),
            "price": ("price",),
        },
        integer_columns=("menu_id", "restaurant_id", "food_id"),
        numeric_columns=("price",),
        foreign_keys=(
            ("restaurant_id", "restaurant", "restaurant_id"),
            ("food_id", "food", "food_id"),
        ),
    ),
    "orders": TableSpec(
        csv_name="orders.csv",
        # orders.csv has NO order id column → surrogate IDENTITY PK assigned
        # by PostgreSQL (see sql/01_schema.sql); no de-duplication applies.
        pk_columns=(),
        columns={
            "order_datetime": ("order_datetime", "order_date", "datetime", "date"),
            "quantity": ("quantity", "sales_qty", "qty", "order_qty"),
            "sales_amount": ("sales_amount", "amount", "total_amount"),
            "currency": ("currency",),
            "user_id": ("user_id",),
            "restaurant_id": ("restaurant_id", "r_id", "res_id"),
        },
        integer_columns=("quantity", "user_id", "restaurant_id"),
        numeric_columns=("sales_amount",),
        datetime_columns=("order_datetime",),
        foreign_keys=(
            ("user_id", "users", "user_id"),
            ("restaurant_id", "restaurant", "restaurant_id"),
        ),
    ),
}

# FK-safe load order: parents before children.
LOAD_ORDER: tuple[str, ...] = ("users", "restaurant", "food", "menu", "orders")


# ---------------------------------------------------------------------------
# CSV reading + column resolution
# ---------------------------------------------------------------------------
def read_csv_robust(path: Path) -> pd.DataFrame:
    """Read a CSV with byte-level pre-cleaning + encoding fallback.

    The source export pollutes cells with raw carriage-return bytes (seen as
    'INR\\r' in currencies). Left in place they survive into the database and
    can even be mistaken for line breaks, shattering rows. So:

      CRLF (\\r\\n) -> \\n       (real Windows line endings, preserved)
      bare \\r      -> deleted   (in-cell pollution, removed before parsing)
    """
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"")
    last_error: Exception | None = None
    for encoding in CSV_ENCODINGS:
        try:
            return pd.read_csv(io.StringIO(raw.decode(encoding)), low_memory=False)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise RuntimeError(
        f"Could not decode {path.name!r} with any of: {', '.join(CSV_ENCODINGS)}"
    ) from last_error


def normalise_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case, trim, and collapse non-alphanumerics in headers."""
    df.columns = (
        df.columns.astype(str)
        .str.strip()
        .str.lower()
        .str.replace(r"[^0-9a-z]+", "_", regex=True)
        .str.strip("_")
    )
    return df


def resolve_columns(df: pd.DataFrame, table: str, spec: TableSpec) -> pd.DataFrame:
    """Rename CSV columns to canonical names using the alias lists.

    Raises a precise, actionable error if any canonical column cannot be
    matched — fail-fast keeps silent schema drift from poisoning the load.
    """
    rename_map: dict[str, str] = {}
    unresolved: list[str] = []
    for canonical, aliases in spec.columns.items():
        match = next((a for a in aliases if a in df.columns), None)
        if match is None:
            unresolved.append(canonical)
        else:
            rename_map[match] = canonical

    if unresolved:
        raise ValueError(
            f"[{table}] Could not map these canonical columns to any CSV header: "
            f"{unresolved}. CSV headers found: {sorted(df.columns)}. "
            f"Fix: add the real header to the alias list for '{table}' in "
            f"TABLE_SPECS at the top of scripts/load_data.py."
        )

    extra = [c for c in df.columns if c not in rename_map]
    if extra:
        log.warning("[%s] Ignoring unmapped CSV columns: %s", table, extra)

    return df.rename(columns=rename_map)[list(spec.columns)]


# ---------------------------------------------------------------------------
# Type parsing — multi-pass, fully logged
# ---------------------------------------------------------------------------
def _to_int_robust(series: pd.Series) -> tuple[pd.Series, dict[str, int]]:
    """Parse a column to nullable Int64 through three passes, in order:

    1. Plain numeric ("567335", "567335.0")
    2. Alpha-prefixed numeric codes ("fd123", "mn45")     -> 123, 45
    3. Count-band strings ("50+ ratings", "1K+ ratings")  -> 50, 1000
       ("K" multiplies by 1000; "Too Few Ratings" stays NULL)

    Anything still unparsed becomes NULL and is reported as `lost`.
    """
    raw_notnull = series.notna()
    s = series.astype(str).str.strip().str.replace(",", "", regex=False)

    parsed = pd.to_numeric(s, errors="coerce")
    stats = {"prefix": 0, "band": 0, "nonint": 0, "lost": 0}

    def _pending() -> pd.Series:
        return parsed.isna() & raw_notnull & (s.str.lower() != "nan")

    # Pass 2 — prefixed codes: letters/underscore/dash THEN digits, whole string
    pref = s.str.extract(r"(?i)^[a-z_\-]*(\d+(?:\.\d+)?)$", expand=False)
    hit = _pending() & pref.notna()
    if hit.any():
        parsed.loc[hit] = pd.to_numeric(pref.loc[hit], errors="coerce")
        stats["prefix"] = int(hit.sum())

    # Pass 3 — band strings: "50+ ratings", "1K+ ratings", "500+"
    band = s.str.extract(r"(?i)^(\d+(?:\.\d+)?)\s*(k)?\+", expand=True)
    band_vals = pd.to_numeric(band[0], errors="coerce") * band[1].fillna("").str.lower().eq("k").map(
        {True: 1000.0, False: 1.0}
    )
    hit = _pending() & band_vals.notna()
    if hit.any():
        parsed.loc[hit] = band_vals.loc[hit]
        stats["band"] = int(hit.sum())

    # Non-integral floats ("567335.5") are junk for an integer column
    nonint = parsed.notna() & (parsed % 1 != 0)
    if nonint.any():
        stats["nonint"] = int(nonint.sum())
        parsed = parsed.mask(nonint)

    stats["lost"] = int((parsed.isna() & raw_notnull).sum())
    return parsed.astype("Int64"), stats


def _parse_datetime(series: pd.Series) -> tuple[pd.Series, int]:
    """Parse datetimes, preferring the dayfirst interpretation that parses
    more values. Returns (parsed_series, coerced_to_null_count)."""
    candidates: list[tuple[int, pd.Series]] = []
    for dayfirst in (False, True):
        for fmt in (None, "mixed"):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    parsed = pd.to_datetime(
                        series, errors="coerce", dayfirst=dayfirst, format=fmt
                    )
            except (TypeError, ValueError):
                continue
            candidates.append((int(parsed.notna().sum()), parsed))
    if not candidates:
        return pd.Series(pd.NaT, index=series.index), int(series.notna().sum())
    candidates.sort(key=lambda t: t[0], reverse=True)
    best = candidates[0][1]
    coerced = int(series.notna().sum() - best.notna().sum())
    return best, max(coerced, 0)


def clean_types(
    df: pd.DataFrame, table: str, spec: TableSpec, stats: dict[str, int]
) -> pd.DataFrame:
    df = df.copy()

    # Strip whitespace on string columns; empty strings -> NULL
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace({"": None, "nan": None, "None": None})

    for col in spec.integer_columns:
        before = int(df[col].notna().sum())
        df[col], st = _to_int_robust(df[col])
        if st["prefix"]:
            log.info(
                "[%s] %s: %d alpha-prefixed codes parsed to integers (e.g. 'fd123' -> 123)",
                table, col, st["prefix"],
            )
        if st["band"]:
            log.info(
                "[%s] %s: %d band strings parsed (e.g. '50+ ratings' -> 50, '1K+' -> 1000)",
                table, col, st["band"],
            )
        lost = before - int(df[col].notna().sum())
        if lost:
            log.warning(
                "[%s] %s: %d values could not be parsed as integers -> NULL",
                table, col, lost,
            )
            stats["coerced"] += lost

    for col in spec.numeric_columns:
        before = int(df[col].notna().sum())
        cleaned = (
            df[col].astype(str).str.replace(r"[^\d.\-]", "", regex=True).replace("", None)
        )
        df[col] = pd.to_numeric(cleaned, errors="coerce")
        lost = before - int(df[col].notna().sum())
        if lost:
            log.warning(
                "[%s] %s: %d junk values (e.g. '--', 'NEW') coerced to NULL",
                table, col, lost,
            )
            stats["coerced"] += lost
        negatives = int((df[col] < 0).sum())
        if negatives:
            log.info(
                "[%s] %s: %d negative values kept as-is (likely refunds/adjustments — Phase 1 finding)",
                table, col, negatives,
            )

    for col in spec.datetime_columns:
        df[col], coerced = _parse_datetime(df[col])
        if coerced:
            log.warning("[%s] %s: %d unparseable datetimes coerced to NULL", table, col, coerced)
            stats["coerced"] += coerced

    return df


def dedupe_primary_key(
    df: pd.DataFrame, table: str, spec: TableSpec, stats: dict[str, int]
) -> pd.DataFrame:
    """De-duplicate on the source-backed key (single or composite).
    Skipped entirely for tables with a surrogate (DB-assigned) key."""
    if not spec.pk_columns:
        return df

    subset = list(spec.pk_columns)
    dupes = int(df.duplicated(subset=subset, keep="first").sum())
    if dupes:
        log.warning(
            "[%s] %d rows share a duplicate key %s — keeping first occurrence. "
            "Document this as a Phase 1 data-quality finding.",
            table, dupes, subset,
        )
        stats["deduplicated"] += dupes
        df = df.drop_duplicates(subset=subset, keep="first")

    null_pk = int(df[subset].isna().any(axis=1).sum())
    if null_pk:
        log.warning("[%s] Dropping %d rows with NULL key %s", table, null_pk, subset)
        stats["deduplicated"] += null_pk
        df = df[~df[subset].isna().any(axis=1)]
    return df


def quarantine_orphans(
    df: pd.DataFrame,
    table: str,
    spec: TableSpec,
    parent_keys: dict[tuple[str, str], set],
    stats: dict[str, int],
) -> pd.DataFrame:
    """Move rows whose FK has no parent into data/processed/quarantine/.

    The schema enforces real FK constraints, so inserting orphans would abort
    the load. Quarantining keeps the pipeline deterministic *and* preserves
    the offending rows for the Phase 1 data-quality write-up.
    """
    for column, parent_table, parent_column in spec.foreign_keys:
        null_fk = int(df[column].isna().sum())
        if null_fk:
            log.info(
                "[%s] %s: %d rows have NULL %s — kept as-is (valid aggregate-level "
                "facts; excluded from %s-level rollups). Document for Phase 1.",
                table, column, null_fk, column, parent_table,
            )
        valid = parent_keys[(parent_table, parent_column)]
        mask = df[column].notna() & ~df[column].isin(valid)
        orphans = df[mask]
        if len(orphans):
            QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
            out = QUARANTINE_DIR / f"{table}_orphans_{column}.csv"
            orphans.to_csv(out, index=False)
            log.warning(
                "[%s] %d rows have no matching parent in %s.%s — quarantined to %s",
                table, len(orphans), parent_table, parent_column,
                out.relative_to(PROJECT_ROOT),
            )
            stats["quarantined"] += len(orphans)
            df = df[~mask]
    return df


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        raise ConnectionError(
            "Could not connect to PostgreSQL. Is it running?\n"
            "  • Docker:      docker-compose up -d postgres\n"
            "  • Local:       check the PostgreSQL Windows service is started\n"
            "  • DATABASE_URL in .env must point at host 'localhost' when running\n"
            "    this script from your machine (use 'postgres' only inside Docker).\n"
            f"Underlying error: {exc}"
        ) from exc
    return engine


def ensure_tables_exist(engine: Engine) -> None:
    missing = [t for t in LOAD_ORDER if not inspect(engine).has_table(t)]
    if missing:
        raise RuntimeError(
            f"Tables missing in the database: {missing}. Create the schema first:\n"
            "  psql -U postgres -d bharatinsight_db -f sql/01_schema.sql\n"
            "(docker-compose applies it automatically on first container start)."
        )


def ensure_empty_or_reset(engine: Engine, reset: bool) -> None:
    with engine.begin() as conn:
        if reset:
            log.warning("--reset passed: truncating %s", ", ".join(LOAD_ORDER))
            conn.execute(
                text(f"TRUNCATE TABLE {', '.join(LOAD_ORDER)} RESTART IDENTITY CASCADE")
            )
            return
        non_empty = []
        for table in LOAD_ORDER:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            if count:
                non_empty.append(f"{table} ({count:,} rows)")
        if non_empty:
            raise RuntimeError(
                "Target tables are not empty: "
                + ", ".join(non_empty)
                + ". Append would duplicate rows. Re-run with --reset to "
                "truncate and reload."
            )


def insert_dataframe(engine: Engine, table: str, df: pd.DataFrame) -> None:
    with engine.begin() as conn:
        df.to_sql(
            table,
            conn,
            if_exists="append",
            index=False,
            method="multi",
            chunksize=INSERT_CHUNKSIZE,
        )


def post_load_count_check(engine: Engine, expected: dict[str, int]) -> None:
    log.info("Row-count validation (DB vs loaded):")
    failures: list[str] = []
    with engine.connect() as conn:
        for table in LOAD_ORDER:
            db_count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            status = "OK" if db_count == expected[table] else "MISMATCH"
            log.info(
                f"  {table:<12} CSV->loaded: {expected[table]:>8,} "
                f"| DB: {db_count:>8,} | {status}"
            )
            if status == "MISMATCH":
                failures.append(table)
    if failures:
        raise AssertionError(
            f"Row-count mismatch in {failures}. The database state does not match "
            "what was loaded — investigate before proceeding to Phase 1."
        )


def join_integrity_check(engine: Engine) -> None:
    checks = [
        ("orders.user_id   -> users.user_id          ",
         "SELECT COUNT(*) FROM orders o LEFT JOIN users u "
         "ON u.user_id = o.user_id WHERE o.user_id IS NOT NULL AND u.user_id IS NULL"),
        ("orders.restaurant_id -> restaurant.restaurant_id",
         "SELECT COUNT(*) FROM orders o LEFT JOIN restaurant r "
         "ON r.restaurant_id = o.restaurant_id WHERE o.restaurant_id IS NOT NULL AND r.restaurant_id IS NULL"),
        ("menu.restaurant_id   -> restaurant.restaurant_id ",
         "SELECT COUNT(*) FROM menu m LEFT JOIN restaurant r "
         "ON r.restaurant_id = m.restaurant_id WHERE m.restaurant_id IS NOT NULL AND r.restaurant_id IS NULL"),
        ("menu.food_id     -> food.food_id           ",
         "SELECT COUNT(*) FROM menu m LEFT JOIN food f "
         "ON f.food_id = m.food_id WHERE m.food_id IS NOT NULL AND f.food_id IS NULL"),
    ]
    log.info("Join-integrity checks (orphan rows — expected 0):")
    with engine.connect() as conn:
        for label, query in checks:
            orphans = conn.execute(text(query)).scalar_one()
            log.info("  %s : %d orphans", label, orphans)
        # documented Phase 0 finding: orders rows with missing dimension keys
        # (kept with NULL FK — included in revenue/RFM aggregates, excluded
        #  from restaurant-level rollups)
        for column, label in (("user_id", "orders.user_id"), ("restaurant_id", "orders.restaurant_id")):
            nulls = conn.execute(
                text(f"SELECT COUNT(*) FROM orders WHERE {column} IS NULL")
            ).scalar_one()
            if nulls:
                log.info("  %s : %d rows with NULL key (kept — see README finding #9)", label, nulls)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BharatInsight Phase 0 loader — CSVs in data/raw/ -> PostgreSQL"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="TRUNCATE all 5 tables (CASCADE) before loading — makes the load re-runnable.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any rows were deduplicated, quarantined, or value-coerced.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL from .env",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL not set. Copy .env.example to .env and fill it in.")
        return 1

    log.info("=" * 70)
    log.info("BharatInsight — Phase 0 · Database Loader")
    log.info("=" * 70)

    stats = {"coerced": 0, "deduplicated": 0, "quarantined": 0}

    try:
        engine = get_engine(database_url)
        ensure_tables_exist(engine)
        ensure_empty_or_reset(engine, reset=args.reset)

        parent_keys: dict[tuple[str, str], set] = {}
        loaded_counts: dict[str, int] = {}

        for table in LOAD_ORDER:
            spec = TABLE_SPECS[table]
            csv_path = RAW_DATA_DIR / spec.csv_name
            if not csv_path.is_file():
                case_fix = next(
                    (p for p in RAW_DATA_DIR.glob("*.csv") if p.name.lower() == spec.csv_name),
                    None,
                )
                if case_fix:
                    csv_path = case_fix
                else:
                    raise FileNotFoundError(
                        f"Expected CSV not found: {csv_path}. Run scripts/verify_schema.py "
                        "first and confirm all 5 files are in data/raw/."
                    )

            log.info("[%s] Loading %s...", table, csv_path.name)

            df = read_csv_robust(csv_path)
            log.info("[%s] CSV shape: %d rows x %d cols", table, df.shape[0], df.shape[1])
            df = normalise_headers(df)
            df = resolve_columns(df, table, spec)
            df = clean_types(df, table, spec, stats)
            df = dedupe_primary_key(df, table, spec, stats)
            df = quarantine_orphans(df, table, spec, parent_keys, stats)

            insert_dataframe(engine, table, df)
            loaded_counts[table] = len(df)
            # parent-key registry: only single-column, source-backed keys can
            # be FK targets for later tables (users, restaurant, food)
            if len(spec.pk_columns) == 1:
                parent_keys[(table, spec.pk_columns[0])] = set(
                    df[spec.pk_columns[0]].dropna().tolist()
                )
            log.info("[%s] Inserted %d rows.", table, len(df))

        post_load_count_check(engine, loaded_counts)
        join_integrity_check(engine)

    except (ConnectionError, RuntimeError, ValueError, FileNotFoundError, AssertionError) as exc:
        log.error("%s", exc)
        return 1

    log.info("-" * 70)
    if any(stats.values()):
        log.warning(
            "Cleaning summary: %d value(s) coerced to NULL | %d duplicate/NULL-PK row(s) dropped "
            "| %d orphan row(s) quarantined.",
            stats["coerced"], stats["deduplicated"], stats["quarantined"],
        )
        if args.strict:
            log.error("--strict mode: failing because cleaning actions were required.")
            return 1

    log.info("PHASE 0 LOAD: SUCCESS — database is ready for Phase 1 (Excel) and Phase 2 (SQL).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
