#!/usr/bin/env python3
"""
BharatInsight — Phase 0, Step 1: Schema Verification
=====================================================

Reads every CSV in ``data/raw/`` and prints a structural profile for each
table — filename, shape, dtypes, ``head(3)`` and per-column null counts —
then writes a consolidated ``schema_report.md`` to the project root.

WHY THIS RUNS FIRST
-------------------
Every column name in the project blueprint was reconstructed from the
dataset's public documentation, not from the files on disk. Run this script
BEFORE writing/running ``sql/01_schema.sql`` or ``scripts/load_data.py`` and
treat the generated ``schema_report.md`` as the single source of truth.

Usage
-----
    python scripts/verify_schema.py

Exit codes
----------
    0 — all 5 expected CSVs found and profiled
    1 — data/raw missing, or one or more expected CSVs missing/unreadable
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
REPORT_PATH = PROJECT_ROOT / "schema_report.md"

# The five tables this project is built on (matched case-insensitively).
EXPECTED_FILES: tuple[str, ...] = (
    "users.csv",
    "restaurant.csv",
    "menu.csv",
    "food.csv",
    "orders.csv",
)

# Encodings tried in order — Kaggle CSVs are usually utf-8, occasionally
# utf-8-sig (BOM) or latin-1.
ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "latin-1")

HEAD_ROWS = 3
MAX_SAMPLE_VALUES = 5
RULE = "=" * 78


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def read_csv_robust(path: Path) -> pd.DataFrame:
    """Read a CSV, falling back through common encodings before giving up."""
    last_error: Exception | None = None
    for encoding in ENCODINGS:
        try:
            return pd.read_csv(path, encoding=encoding, low_memory=False)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise RuntimeError(
        f"Could not decode {path.name!r} with any of: {', '.join(ENCODINGS)}"
    ) from last_error


def profile_dataframe(path: Path, df: pd.DataFrame) -> dict:
    """Build a JSON-serialisable profile of one table."""
    return {
        "filename": path.name,
        "encoding_tested": ", ".join(ENCODINGS),
        "rows": df.shape[0],
        "cols": df.shape[1],
        "columns": list(df.columns),
        "dtypes": df.dtypes,
        "head": df.head(HEAD_ROWS),
        "nulls": df.isnull().sum(),
        "nunique": df.nunique(),
        "duplicate_rows": int(df.duplicated().sum()),
        "df": df,  # kept for sample-value extraction in the report
    }


def print_profile(profile: dict) -> None:
    """Console output: filename, shape, dtypes, head(3), null counts."""
    print(RULE)
    print(f"FILE: {profile['filename']}")
    print(RULE)
    print(f"Shape          : {profile['rows']:,} rows x {profile['cols']} columns")
    print(f"Duplicate rows : {profile['duplicate_rows']:,}")
    print("\n-- dtypes " + "-" * 68)
    print(profile["dtypes"].to_string())
    print(f"\n-- head({HEAD_ROWS}) " + "-" * 57)
    print(profile["head"].to_string(index=False))
    print("\n-- null counts " + "-" * 62)
    print(profile["nulls"].to_string())
    print("\n")


def _sample_values(df: pd.DataFrame, column: str) -> str:
    """Short, report-safe preview of a column's values."""
    samples = (
        df[column].dropna().astype(str).unique()[:MAX_SAMPLE_VALUES].tolist()
    )
    rendered = ", ".join(s if len(s) <= 25 else s[:22] + "..." for s in samples)
    return rendered if rendered else "—"


def write_report(profiles: list[dict], path: Path) -> None:
    """Write schema_report.md — per-table column profiles + join-key matrix."""
    lines: list[str] = []
    lines.append("# BharatInsight — Schema Verification Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} by `scripts/verify_schema.py`")
    lines.append("")
    lines.append("> **This report is the single source of truth for the actual column names")
    lines.append("> in your downloaded CSVs.** Compare it against `sql/01_schema.sql` and the")
    lines.append("> alias mapping in `scripts/load_data.py` before loading any data.")
    lines.append("")

    # --- Overview table -----------------------------------------------------
    lines.append("## 1. File Overview")
    lines.append("")
    lines.append("| File | Rows | Columns | Duplicate Rows | Total Nulls |")
    lines.append("|---|---:|---:|---:|---:|")
    for p in profiles:
        total_nulls = int(p["nulls"].sum())
        lines.append(
            f"| `{p['filename']}` | {p['rows']:,} | {p['cols']} "
            f"| {p['duplicate_rows']:,} | {total_nulls:,} |"
        )
    lines.append("")

    # --- Per-table detail ----------------------------------------------------
    lines.append("## 2. Per-Table Column Profiles")
    lines.append("")
    for p in profiles:
        lines.append(f"### {p['filename']} — {p['rows']:,} rows")
        lines.append("")
        lines.append("| # | Column | Dtype | Non-Null | Null | Null % | Unique | Sample Values |")
        lines.append("|---:|---|---|---:|---:|---:|---:|---|")
        for i, col in enumerate(p["columns"], start=1):
            null_count = int(p["nulls"][col])
            null_pct = (null_count / p["rows"] * 100) if p["rows"] else 0.0
            lines.append(
                f"| {i} | `{col}` | {p['dtypes'][col]} | {p['rows'] - null_count:,} "
                f"| {null_count:,} | {null_pct:.1f}% | {int(p['nunique'][col]):,} "
                f"| {_sample_values(p['df'], col)} |"
            )
        lines.append("")

    # --- Cross-table join-key matrix ----------------------------------------
    lines.append("## 3. Shared Columns Across Tables (potential join keys)")
    lines.append("")
    column_to_files: dict[str, list[str]] = {}
    for p in profiles:
        for col in p["columns"]:
            column_to_files.setdefault(str(col).strip().lower(), []).append(p["filename"])
    shared = {c: sorted(f) for c, f in column_to_files.items() if len(f) > 1}
    if shared:
        lines.append("| Column (normalised) | Appears in |")
        lines.append("|---|---|")
        for col, files in sorted(shared.items()):
            lines.append(f"| `{col}` | {', '.join(files)} |")
    else:
        lines.append("_No column name appears in more than one table — foreign-key columns")
        lines.append("use different names per table. Note them here before writing the schema._")
    lines.append("")

    # --- Expected foreign keys checklist -------------------------------------
    lines.append("## 4. Foreign-Key Checklist (verify before running sql/01_schema.sql)")
    lines.append("")
    lines.append("| FK | Child column must exist in | Parent column must exist in |")
    lines.append("|---|---|---|")
    lines.append("| orders → users | `orders.csv` user-id column | `users.csv` user-id column |")
    lines.append("| orders → restaurant | `orders.csv` restaurant-id column | `restaurant.csv` id column |")
    lines.append("| menu → restaurant | `menu.csv` restaurant-id column | `restaurant.csv` id column |")
    lines.append("| menu → food | `menu.csv` food-id column | `food.csv` food-id column |")
    lines.append("")
    lines.append("_If any column name differs from the canonical names in `sql/01_schema.sql`,")
    lines.append("adjust the alias lists at the top of `scripts/load_data.py` (preferred) or")
    lines.append("edit the schema itself._")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    print(RULE)
    print("BharatInsight — Phase 0 · Schema Verification")
    print(RULE)

    if not RAW_DATA_DIR.is_dir():
        print(f"\n[ERROR] Expected directory not found: {RAW_DATA_DIR}")
        print("        Create it and place the 5 Kaggle CSVs inside.")
        return 1

    available = {p.name.lower(): p for p in RAW_DATA_DIR.glob("*.csv")}

    missing = [name for name in EXPECTED_FILES if name not in available]
    extras = sorted(set(available) - set(EXPECTED_FILES))

    if missing:
        print("\n[ERROR] Missing expected CSV files in data/raw/:")
        for name in missing:
            print(f"        - {name}")
        print(
            "\n        Download the dataset from "
            "https://www.kaggle.com/datasets/anas123siddiqui/zomato-database"
        )
        print("        and rename the files to match the expected names above.")
        return 1

    if extras:
        print("\n[WARN] Extra CSVs found (will be profiled after the expected 5):")
        for name in extras:
            print(f"        - {name}")
        print()

    ordered_paths = [available[name] for name in EXPECTED_FILES] + [
        available[name] for name in extras
    ]

    profiles: list[dict] = []
    for path in ordered_paths:
        try:
            df = read_csv_robust(path)
        except (RuntimeError, pd.errors.ParserError) as exc:
            print(f"\n[ERROR] Failed to read {path.name}: {exc}")
            return 1
        profile = profile_dataframe(path, df)
        print_profile(profile)
        profiles.append(profile)

    write_report(profiles, REPORT_PATH)
    print(RULE)
    print(f"[OK] Schema report written to: {REPORT_PATH.relative_to(PROJECT_ROOT)}")
    print("     NEXT STEP: review the report, confirm column names, then run")
    print("     sql/01_schema.sql followed by scripts/load_data.py")
    print(RULE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
