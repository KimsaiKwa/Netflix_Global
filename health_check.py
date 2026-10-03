#!/usr/bin/env python3
"""Independent health check for the Netflix Top 10 mirror.

This script does not download or regenerate Netflix data. It validates the files
already committed to the repository so update failures, partial country data,
week mismatches, schema drift, and stale data are detected independently from
the weekly update workflow.

Exit codes:
- 0: healthy (warnings may still be printed)
- 1: unhealthy
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
GLOBAL_FILE = ROOT / "global.json"
COUNTRIES_DIR = ROOT / "countries"

EXPECTED_COUNTRIES = {
    "US": "United States",
    "KR": "South Korea",
    "JP": "Japan",
    "GB": "United Kingdom",
    "FR": "France",
    "DE": "Germany",
    "ES": "Spain",
    "IT": "Italy",
    "NL": "Netherlands",
    "PL": "Poland",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
    "FI": "Finland",
    "BE": "Belgium",
    "AT": "Austria",
    "CH": "Switzerland",
    "PT": "Portugal",
    "IE": "Ireland",
    "CZ": "Czech Republic",
    "GR": "Greece",
    "HU": "Hungary",
    "RO": "Romania",
}

EUROPE_CODES = {
    "GB", "FR", "DE", "ES", "IT", "NL", "PL", "SE", "NO", "DK",
    "FI", "BE", "AT", "CH", "PT", "IE", "CZ", "GR", "HU", "RO",
}

OFFICIAL_GLOBAL_CATEGORIES = [
    "Films (English)",
    "Films (Non-English)",
    "TV (English)",
    "TV (Non-English)",
]

MAX_WEEK_AGE_DAYS = int(os.environ.get("MAX_WEEK_AGE_DAYS", "10"))
MAX_GENERATED_AGE_HOURS = int(
    os.environ.get("MAX_GENERATED_AGE_HOURS", str(8 * 24))
)


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def load_json(path: Path, report: Report) -> dict[str, Any] | None:
    if not path.exists():
        report.error(f"Missing file: {path.relative_to(ROOT)}")
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        report.error(
            f"Invalid JSON in {path.relative_to(ROOT)}: "
            f"{type(exc).__name__}: {exc}"
        )
        return None

    if not isinstance(data, dict):
        report.error(f"{path.relative_to(ROOT)} must contain a JSON object")
        return None

    return data


def parse_week(label: str, value: Any, report: Report) -> date | None:
    if not isinstance(value, str) or not value.strip():
        report.error(f"{label}: missing week")
        return None

    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        report.error(f"{label}: invalid week {value!r}; expected YYYY-MM-DD")
        return None


def parse_generated_at(value: Any, report: Report) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        report.error("global.json: missing generated_at")
        return None

    text = value.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        report.error(f"global.json: invalid generated_at {value!r}")
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def validate_top10(
    label: str,
    rows: Any,
    report: Report,
    *,
    rank_key: str = "rank",
    require_views: bool = False,
) -> None:
    if not isinstance(rows, list):
        report.error(f"{label}: expected a list")
        return

    if len(rows) != 10:
        report.error(f"{label}: expected 10 rows, found {len(rows)}")

    ranks: list[int] = []

    for index, item in enumerate(rows, start=1):
        item_label = f"{label}[{index}]"

        if not isinstance(item, dict):
            report.error(f"{item_label}: expected an object")
            continue

        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            report.error(f"{item_label}: missing title")

        rank = item.get(rank_key)
        if not isinstance(rank, int):
            report.error(f"{item_label}: {rank_key} must be an integer")
        else:
            ranks.append(rank)

        weeks = item.get("weeks_in_top_10")
        if weeks is not None and (
            not isinstance(weeks, int) or isinstance(weeks, bool) or weeks < 0
        ):
            report.error(
                f"{item_label}: weeks_in_top_10 must be a non-negative integer"
            )

        if require_views:
            views = item.get("views")
            if not isinstance(views, int) or isinstance(views, bool) or views <= 0:
                report.error(f"{item_label}: views must be a positive integer")

    if len(ranks) == len(rows):
        expected = list(range(1, len(rows) + 1))
        if sorted(ranks) != expected:
            report.error(
                f"{label}: {rank_key} values must be exactly {expected}; "
                f"found {sorted(ranks)}"
            )


def validate_global(
    data: dict[str, Any],
    report: Report,
) -> tuple[str | None, date | None]:
    week_text = data.get("week")
    week_date = parse_week("global.json", week_text, report)
    week = week_text.strip() if isinstance(week_text, str) else None

    validate_top10(
        "global.json films",
        data.get("films"),
        report,
        require_views=True,
    )
    validate_top10(
        "global.json tv",
        data.get("tv"),
        report,
        require_views=True,
    )

    official = data.get("official_categories")
    if not isinstance(official, dict):
        report.error("global.json: official_categories must be an object")
    else:
        for category in OFFICIAL_GLOBAL_CATEGORIES:
            rows = official.get(category)
            validate_top10(
                f"global.json official_categories[{category!r}]",
                rows,
                report,
                rank_key="source_rank",
                require_views=True,
            )

    if data.get("derived") is not True:
        report.warning(
            "global.json: derived is not true; verify the global merge methodology"
        )

    generated_at = parse_generated_at(data.get("generated_at"), report)
    if generated_at is not None:
        now = datetime.now(timezone.utc)
        age_hours = (now - generated_at).total_seconds() / 3600

        if age_hours < -1:
            report.error(
                f"global.json: generated_at is {abs(age_hours):.1f} hours in the future"
            )
        elif age_hours > MAX_GENERATED_AGE_HOURS:
            report.error(
                f"global.json is stale: generated_at is {age_hours:.1f} hours old "
                f"(limit {MAX_GENERATED_AGE_HOURS}h)"
            )

    if week_date is not None:
        age_days = (datetime.now(timezone.utc).date() - week_date).days

        if age_days < -2:
            report.error(
                f"global.json: reporting week is {-age_days} days in the future"
            )
        elif age_days > MAX_WEEK_AGE_DAYS:
            report.error(
                f"global.json reporting week is stale: {age_days} days old "
                f"(limit {MAX_WEEK_AGE_DAYS} days)"
            )

    return week, week_date


def validate_country(
    code: str,
    expected_name: str,
    data: dict[str, Any],
    report: Report,
) -> str | None:
    label = f"countries/{code.lower()}.json"

    actual_code = data.get("country_iso2")
    if actual_code != code:
        report.error(
            f"{label}: country_iso2={actual_code!r}, expected {code!r}"
        )

    country_name = data.get("country_name")
    if not isinstance(country_name, str) or not country_name.strip():
        report.error(f"{label}: missing country_name")
    elif country_name.strip() != expected_name:
        report.warning(
            f"{label}: country_name={country_name!r}, expected {expected_name!r}"
        )

    week_value = data.get("week")
    parse_week(label, week_value, report)
    week = week_value.strip() if isinstance(week_value, str) else None

    validate_top10(
        f"{label} films",
        data.get("films"),
        report,
    )
    validate_top10(
        f"{label} tv",
        data.get("tv"),
        report,
    )

    return week


def main() -> int:
    report = Report()

    global_data = load_json(GLOBAL_FILE, report)
    global_week: str | None = None

    if global_data is not None:
        global_week, _ = validate_global(global_data, report)

    country_weeks: dict[str, str] = {}

    for code, expected_name in EXPECTED_COUNTRIES.items():
        path = COUNTRIES_DIR / f"{code.lower()}.json"
        data = load_json(path, report)

        if data is None:
            continue

        week = validate_country(code, expected_name, data, report)
        if week:
            country_weeks[code] = week

    expected_files = {f"{code.lower()}.json" for code in EXPECTED_COUNTRIES}

    if COUNTRIES_DIR.exists():
        actual_files = {path.name for path in COUNTRIES_DIR.glob("*.json")}
        extras = sorted(actual_files - expected_files)

        if extras:
            report.warning(
                "Unexpected country JSON files present: " + ", ".join(extras)
            )

    missing_codes = sorted(set(EXPECTED_COUNTRIES) - set(country_weeks))
    if missing_codes:
        report.error(
            "Missing or invalid country week data for: " + ", ".join(missing_codes)
        )

    unique_country_weeks = sorted(set(country_weeks.values()))

    if len(unique_country_weeks) > 1:
        distribution: dict[str, list[str]] = {}
        for code, week in country_weeks.items():
            distribution.setdefault(week, []).append(code)

        pretty = "; ".join(
            f"{week}: {','.join(sorted(codes))}"
            for week, codes in sorted(distribution.items())
        )
        report.error(f"Country JSON files are not on one week: {pretty}")

    if global_week and unique_country_weeks:
        if len(unique_country_weeks) == 1 and unique_country_weeks[0] != global_week:
            report.error(
                f"Global/country week mismatch: global={global_week}, "
                f"countries={unique_country_weeks[0]}"
            )

    europe_missing = sorted(EUROPE_CODES - set(country_weeks))
    if europe_missing:
        report.error(
            "Europe aggregate inputs missing: " + ", ".join(europe_missing)
        )

    print("# Netflix Top 10 Mirror Health Check")
    print()
    print(f"- Global week: {global_week or 'UNKNOWN'}")
    print(
        f"- Country files valid: "
        f"{len(country_weeks)}/{len(EXPECTED_COUNTRIES)}"
    )
    print(
        f"- Country week(s): "
        f"{', '.join(unique_country_weeks) if unique_country_weeks else 'UNKNOWN'}"
    )
    print(
        f"- Europe inputs valid: "
        f"{len(EUROPE_CODES - set(europe_missing))}/{len(EUROPE_CODES)}"
    )
    print(
        f"- Freshness limits: week <= {MAX_WEEK_AGE_DAYS} days, "
        f"generated_at <= {MAX_GENERATED_AGE_HOURS} hours"
    )

    if report.warnings:
        print()
        print("## Warnings")
        for warning in report.warnings:
            print(f"- {warning}")

    if report.errors:
        print()
        print("## FAILED")
        for error in report.errors:
            print(f"- {error}")

        print()
        print(f"Result: UNHEALTHY ({len(report.errors)} error(s))")
        return 1

    print()
    print("## PASSED")
    print("- global.json structure is valid")
    print("- all required country JSON files are valid")
    print("- global and country data weeks are consistent")
    print("- all 20 Europe aggregate inputs are available")
    print("- data freshness is within the configured limits")
    print()
    print("Result: HEALTHY")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
