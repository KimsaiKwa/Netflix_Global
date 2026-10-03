#!/usr/bin/env python3
"""Detect structural changes in Netflix Tudum Top 10 TSV datasets.

This checker is intentionally separate from the mirror generators. It compares the
official TSV files against schema_baseline.json and fails on structural or semantic
schema drift before the weekly mirror updater depends on the changed format.

It checks:
- exact column names and order
- row column counts
- expected category values
- required non-empty fields
- basic field types/formats used by the generators

The checker does not validate rankings or freshness; those belong to health_check.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINE = ROOT / "schema_baseline.json"

ISO2_RE = re.compile(r"^[A-Z]{2}$")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def load_baseline(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Schema baseline does not exist: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Unable to parse schema baseline {path}: {type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise RuntimeError("schema_baseline.json must contain a sources object")

    return data


def parse_int(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None

    try:
        return int(text)
    except ValueError:
        return None


def parse_number(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None

    try:
        return float(text)
    except ValueError:
        return None


def valid_iso_date(value: str) -> bool:
    text = value.strip()
    if not text:
        return False

    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


def format_columns(columns: Iterable[str]) -> str:
    return " | ".join(columns)


def validate_header(
    source_name: str,
    actual: list[str],
    expected: list[str],
    report: Report,
) -> None:
    if actual == expected:
        return

    actual_set = set(actual)
    expected_set = set(expected)

    missing = [column for column in expected if column not in actual_set]
    added = [column for column in actual if column not in expected_set]

    if missing:
        report.error(
            f"{source_name}: columns removed/missing: {', '.join(missing)}"
        )

    if added:
        report.error(
            f"{source_name}: new/unexpected columns: {', '.join(added)}"
        )

    if not missing and not added:
        report.error(
            f"{source_name}: column order changed. "
            f"expected [{format_columns(expected)}], "
            f"actual [{format_columns(actual)}]"
        )


def validate_common_row(
    source_name: str,
    row_number: int,
    row: dict[str, str | None],
    expected_columns: list[str],
    required_nonempty: set[str],
    allowed_categories: set[str],
    report: Report,
) -> None:
    label = f"{source_name} row {row_number}"

    # DictReader stores surplus fields under the None key.
    if None in row:
        report.error(
            f"{label}: row contains more tab-separated fields than the header"
        )

    for column in expected_columns:
        if column not in row:
            report.error(f"{label}: column {column!r} is missing from parsed row")
            continue

        value = row.get(column)

        if column in required_nonempty and (
            value is None or not str(value).strip()
        ):
            report.error(f"{label}: required field {column!r} is empty")

    week = str(row.get("week") or "")
    if week and not valid_iso_date(week):
        report.error(f"{label}: week is not YYYY-MM-DD: {week!r}")

    category = str(row.get("category") or "").strip()
    if category and category not in allowed_categories:
        report.error(
            f"{label}: unexpected category {category!r}; "
            f"allowed={sorted(allowed_categories)!r}"
        )

    weekly_rank = parse_int(str(row.get("weekly_rank") or ""))
    if weekly_rank is None or not (1 <= weekly_rank <= 10):
        report.error(
            f"{label}: weekly_rank must be an integer from 1 to 10; "
            f"found {row.get('weekly_rank')!r}"
        )

    cumulative = parse_int(
        str(row.get("cumulative_weeks_in_top_10") or "")
    )
    if cumulative is None or cumulative < 0:
        report.error(
            f"{label}: cumulative_weeks_in_top_10 must be a "
            f"non-negative integer; found "
            f"{row.get('cumulative_weeks_in_top_10')!r}"
        )


def validate_global_row(
    row_number: int,
    row: dict[str, str | None],
    report: Report,
) -> None:
    label = f"global row {row_number}"

    hours = parse_number(str(row.get("weekly_hours_viewed") or ""))
    if hours is None or hours < 0:
        report.error(
            f"{label}: weekly_hours_viewed must be a non-negative number; "
            f"found {row.get('weekly_hours_viewed')!r}"
        )

    # Netflix historical rows may have blanks for runtime / weekly_views.
    runtime_text = str(row.get("runtime") or "").strip()
    if runtime_text:
        runtime = parse_number(runtime_text)
        if runtime is None or runtime < 0:
            report.error(
                f"{label}: runtime must be blank or a non-negative number; "
                f"found {row.get('runtime')!r}"
            )

    views_text = str(row.get("weekly_views") or "").strip()
    if views_text:
        views = parse_number(views_text)
        if views is None or views < 0:
            report.error(
                f"{label}: weekly_views must be blank or a non-negative number; "
                f"found {row.get('weekly_views')!r}"
            )


def validate_country_row(
    row_number: int,
    row: dict[str, str | None],
    report: Report,
) -> None:
    label = f"countries row {row_number}"

    iso2 = str(row.get("country_iso2") or "").strip()
    if iso2 and not ISO2_RE.fullmatch(iso2):
        report.error(
            f"{label}: country_iso2 must be two uppercase ASCII letters; "
            f"found {iso2!r}"
        )


def validate_source(
    source_name: str,
    file_path: Path,
    baseline: dict[str, Any],
    report: Report,
) -> dict[str, Any]:
    if not file_path.exists():
        report.error(f"{source_name}: source file not found: {file_path}")
        return {
            "rows": 0,
            "categories": [],
            "header": [],
        }

    expected_columns = list(baseline.get("columns") or [])
    required_nonempty = set(baseline.get("required_nonempty") or [])
    allowed_categories = set(baseline.get("allowed_categories") or [])

    if not expected_columns:
        report.error(f"{source_name}: baseline has no columns")
        return {
            "rows": 0,
            "categories": [],
            "header": [],
        }

    if not allowed_categories:
        report.error(f"{source_name}: baseline has no allowed_categories")

    categories_seen: set[str] = set()
    rows_seen = 0

    try:
        handle = file_path.open(
            "r",
            encoding="utf-8-sig",
            errors="strict",
            newline="",
        )
    except Exception as exc:
        report.error(
            f"{source_name}: unable to open {file_path}: "
            f"{type(exc).__name__}: {exc}"
        )
        return {
            "rows": 0,
            "categories": [],
            "header": [],
        }

    with handle:
        reader = csv.DictReader(handle, delimiter="\t")
        actual_header = list(reader.fieldnames or [])

        validate_header(
            source_name,
            actual_header,
            expected_columns,
            report,
        )

        for row_number, row in enumerate(reader, start=2):
            rows_seen += 1

            validate_common_row(
                source_name,
                row_number,
                row,
                expected_columns,
                required_nonempty,
                allowed_categories,
                report,
            )

            category = str(row.get("category") or "").strip()
            if category:
                categories_seen.add(category)

            if source_name == "global":
                validate_global_row(row_number, row, report)
            elif source_name == "countries":
                validate_country_row(row_number, row, report)

            # Avoid producing millions of duplicate messages if Netflix changes a type.
            if len(report.errors) >= 100:
                report.error(
                    "Too many schema errors; validation stopped after 100+ errors"
                )
                break

    if rows_seen == 0:
        report.error(f"{source_name}: TSV contains no data rows")

    missing_categories = sorted(allowed_categories - categories_seen)
    unexpected_categories = sorted(categories_seen - allowed_categories)

    if missing_categories:
        report.error(
            f"{source_name}: expected categories not observed: "
            + ", ".join(missing_categories)
        )

    if unexpected_categories:
        report.error(
            f"{source_name}: unexpected categories observed: "
            + ", ".join(unexpected_categories)
        )

    return {
        "rows": rows_seen,
        "categories": sorted(categories_seen),
        "header": actual_header,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
    )
    parser.add_argument(
        "--global-file",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--countries-file",
        type=Path,
        required=True,
    )
    args = parser.parse_args()

    try:
        baseline = load_baseline(args.baseline)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = Report()
    sources = baseline["sources"]

    for required_source in ("global", "countries"):
        if required_source not in sources:
            report.error(
                f"schema baseline is missing source {required_source!r}"
            )

    summaries: dict[str, dict[str, Any]] = {}

    if "global" in sources:
        summaries["global"] = validate_source(
            "global",
            args.global_file,
            sources["global"],
            report,
        )

    if "countries" in sources:
        summaries["countries"] = validate_source(
            "countries",
            args.countries_file,
            sources["countries"],
            report,
        )

    print("# Netflix Tudum TSV Schema Check")
    print()
    print(f"- Baseline version: {baseline.get('version', 'UNKNOWN')}")

    for name in ("global", "countries"):
        summary = summaries.get(name)
        if not summary:
            continue

        print(f"- {name} rows scanned: {summary['rows']}")
        print(
            f"- {name} categories: "
            f"{', '.join(summary['categories']) or 'NONE'}"
        )
        print(
            f"- {name} header: "
            f"{format_columns(summary['header']) or 'NONE'}"
        )

    if report.warnings:
        print()
        print("## Warnings")
        for warning in report.warnings:
            print(f"- {warning}")

    if report.errors:
        print()
        print("## SCHEMA CHANGE / FAILURE DETECTED")
        for error in report.errors:
            print(f"- {error}")

        print()
        print(f"Result: UNHEALTHY ({len(report.errors)} error(s))")
        return 1

    print()
    print("## PASSED")
    print("- exact column names and order match the baseline")
    print("- category values match the baseline")
    print("- required fields are populated")
    print("- fields used by the generators retain expected basic types")
    print()
    print("Result: SCHEMA STABLE")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
