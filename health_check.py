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
import re
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
MAX_METADATA_AGE_HOURS = int(
    os.environ.get("MAX_METADATA_AGE_HOURS", str(8 * 24))
)
MIN_PRIORITY_POSTER_COVERAGE = float(
    os.environ.get("MIN_PRIORITY_POSTER_COVERAGE", "0.70")
)
PREFERRED_POSTER_COVERAGE = float(
    os.environ.get("PREFERRED_POSTER_COVERAGE", "0.90")
)
EXPECTED_METADATA_VERSION = 2


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
    require_metadata: bool = False,
    metadata_counter: dict[str, int] | None = None,
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

        if require_metadata:
            if metadata_counter is not None:
                metadata_counter["rows"] = metadata_counter.get("rows", 0) + 1

            cn_title = item.get("cn_title")
            if not isinstance(cn_title, str):
                report.error(f"{item_label}: cn_title must be a string")

            year = item.get("year")
            if not isinstance(year, str):
                report.error(f"{item_label}: year must be a string")

            posters = item.get("poster_candidates")
            if not isinstance(posters, list):
                report.error(f"{item_label}: poster_candidates must be a list")
            else:
                if metadata_counter is not None and posters:
                    metadata_counter["with_poster"] = (
                        metadata_counter.get("with_poster", 0) + 1
                    )
                for poster_index, poster in enumerate(posters, start=1):
                    if not isinstance(poster, str) or not poster.strip():
                        report.error(
                            f"{item_label}: poster_candidates[{poster_index}] "
                            "must be a non-empty string"
                        )

            for key in ("douban_id", "douban_url", "imdb_id", "tmdb_id"):
                if not isinstance(item.get(key), str):
                    report.error(f"{item_label}: {key} must be a string")

            match_version = item.get("metadata_match_version")
            if match_version != EXPECTED_METADATA_VERSION:
                report.error(
                    f"{item_label}: metadata_match_version={match_version!r}, "
                    f"expected {EXPECTED_METADATA_VERSION}"
                )

            confidence = item.get("metadata_confidence")
            if confidence not in {"high", "medium", "none"}:
                report.error(
                    f"{item_label}: invalid metadata_confidence={confidence!r}"
                )

            evidence = item.get("metadata_evidence")
            if not isinstance(evidence, list) or not all(
                isinstance(value, str) and value.strip()
                for value in evidence
            ):
                report.error(
                    f"{item_label}: metadata_evidence must be a list of strings"
                )

            if isinstance(posters, list) and posters and confidence == "none":
                report.error(
                    f"{item_label}: poster exists with metadata_confidence='none'"
                )

            if metadata_counter is not None:
                if confidence == "high":
                    metadata_counter["high_confidence"] = (
                        metadata_counter.get("high_confidence", 0) + 1
                    )
                elif confidence == "medium":
                    metadata_counter["medium_confidence"] = (
                        metadata_counter.get("medium_confidence", 0) + 1
                    )

    if len(ranks) == len(rows):
        expected = list(range(1, len(rows) + 1))
        if sorted(ranks) != expected:
            report.error(
                f"{label}: {rank_key} values must be exactly {expected}; "
                f"found {sorted(ranks)}"
            )


def validate_metadata_header(
    label: str,
    data: dict[str, Any],
    report: Report,
) -> None:
    version = data.get("metadata_version")
    if version != EXPECTED_METADATA_VERSION:
        report.error(
            f"{label}: metadata_version={version!r}, "
            f"expected {EXPECTED_METADATA_VERSION}"
        )

    value = data.get("metadata_enriched_at")
    if not isinstance(value, str) or not value.strip():
        report.error(f"{label}: missing metadata_enriched_at")
        return

    text = value.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        report.error(f"{label}: invalid metadata_enriched_at {value!r}")
        return

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    parsed = parsed.astimezone(timezone.utc)
    age_hours = (datetime.now(timezone.utc) - parsed).total_seconds() / 3600

    if age_hours < -1:
        report.error(
            f"{label}: metadata_enriched_at is "
            f"{abs(age_hours):.1f} hours in the future"
        )
    elif age_hours > MAX_METADATA_AGE_HOURS:
        report.error(
            f"{label}: metadata is stale: {age_hours:.1f} hours old "
            f"(limit {MAX_METADATA_AGE_HOURS}h)"
        )


def validate_global(
    data: dict[str, Any],
    report: Report,
    metadata_counter: dict[str, int],
) -> tuple[str | None, date | None]:
    validate_metadata_header("global.json", data, report)

    week_text = data.get("week")
    week_date = parse_week("global.json", week_text, report)
    week = week_text.strip() if isinstance(week_text, str) else None

    validate_top10(
        "global.json films",
        data.get("films"),
        report,
        require_views=True,
        require_metadata=True,
        metadata_counter=metadata_counter,
    )
    validate_top10(
        "global.json tv",
        data.get("tv"),
        report,
        require_views=True,
        require_metadata=True,
        metadata_counter=metadata_counter,
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
    metadata_counter: dict[str, int],
) -> str | None:
    label = f"countries/{code.lower()}.json"

    validate_metadata_header(label, data, report)

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
        require_metadata=True,
        metadata_counter=metadata_counter,
    )
    validate_top10(
        f"{label} tv",
        data.get("tv"),
        report,
        require_metadata=True,
        metadata_counter=metadata_counter,
    )

    return week



def row_has_poster(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("poster_candidates"), list)
        and any(
            isinstance(value, str) and value.strip()
            for value in row["poster_candidates"]
        )
    )


def normalize_key(value: Any) -> str:
    return re.sub(r"[^\w]+", " ", str(value or "").casefold()).strip()


def build_europe_top10(
    country_payloads: dict[str, dict[str, Any]],
    media_key: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for code in sorted(EUROPE_CODES):
        payload = country_payloads.get(code)
        if not isinstance(payload, dict):
            continue

        rows = payload.get(media_key)
        if not isinstance(rows, list):
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue

            title = str(row.get("title") or "").strip()
            season = (
                str(row.get("season") or "").strip()
                if media_key == "tv"
                else ""
            )

            rank = row.get("rank")

            if not title or not isinstance(rank, int) or not (1 <= rank <= 10):
                continue

            key = f"{normalize_key(title)}|{normalize_key(season)}"

            item = grouped.setdefault(
                key,
                {
                    "title": title,
                    "season": season,
                    "country_count": 0,
                    "total_points": 0,
                    "rank_sum": 0,
                    "metadata_row": row,
                },
            )

            item["country_count"] += 1
            item["total_points"] += 11 - rank
            item["rank_sum"] += rank

            if (
                not row_has_poster(item["metadata_row"])
                and row_has_poster(row)
            ):
                item["metadata_row"] = row

    ranked = list(grouped.values())

    ranked.sort(
        key=lambda item: (
            -item["country_count"],
            -item["total_points"],
            item["rank_sum"] / item["country_count"],
            item["title"].casefold(),
        )
    )

    return [
        item["metadata_row"]
        for item in ranked[:10]
    ]


def validate_priority_poster_coverage(
    global_data: dict[str, Any] | None,
    country_payloads: dict[str, dict[str, Any]],
    report: Report,
) -> list[tuple[str, int, int, float]]:
    checks: list[tuple[str, list[dict[str, Any]]]] = []

    if isinstance(global_data, dict):
        checks.append(
            (
                "global films",
                [
                    row
                    for row in (global_data.get("films") or [])
                    if isinstance(row, dict)
                ],
            )
        )
        checks.append(
            (
                "global tv",
                [
                    row
                    for row in (global_data.get("tv") or [])
                    if isinstance(row, dict)
                ],
            )
        )

    for code in ("US", "KR", "JP"):
        payload = country_payloads.get(code)
        if not isinstance(payload, dict):
            continue

        for media_key in ("films", "tv"):
            checks.append(
                (
                    f"{code} {media_key}",
                    [
                        row
                        for row in (payload.get(media_key) or [])
                        if isinstance(row, dict)
                    ],
                )
            )

    checks.append(
        (
            "Europe aggregate films",
            build_europe_top10(country_payloads, "films"),
        )
    )
    checks.append(
        (
            "Europe aggregate tv",
            build_europe_top10(country_payloads, "tv"),
        )
    )

    results: list[tuple[str, int, int, float]] = []

    for label, rows in checks:
        total = len(rows)
        with_poster = sum(1 for row in rows if row_has_poster(row))
        coverage = with_poster / total if total else 0.0

        results.append((label, with_poster, total, coverage))

        if total != 10:
            report.error(
                f"{label}: expected 10 browser-facing rows, found {total}"
            )
            continue

        if coverage < MIN_PRIORITY_POSTER_COVERAGE:
            report.warning(
                f"{label}: poster coverage {with_poster}/{total} "
                f"({coverage:.1%}) is below the quality floor "
                f"{MIN_PRIORITY_POSTER_COVERAGE:.0%}, but publication is allowed "
                "because anti-mismatch mode prefers missing posters over wrong posters"
            )
        elif coverage < PREFERRED_POSTER_COVERAGE:
            report.warning(
                f"{label}: poster coverage is {with_poster}/{total} "
                f"({coverage:.1%}); preferred is "
                f"{PREFERRED_POSTER_COVERAGE:.0%}"
            )

    return results

def main() -> int:
    report = Report()

    metadata_counter: dict[str, int] = {
        "rows": 0,
        "with_poster": 0,
    }

    global_data = load_json(GLOBAL_FILE, report)
    global_week: str | None = None

    if global_data is not None:
        global_week, _ = validate_global(
            global_data,
            report,
            metadata_counter,
        )

    country_weeks: dict[str, str] = {}
    country_payloads: dict[str, dict[str, Any]] = {}

    for code, expected_name in EXPECTED_COUNTRIES.items():
        path = COUNTRIES_DIR / f"{code.lower()}.json"
        data = load_json(path, report)

        if data is None:
            continue

        country_payloads[code] = data

        week = validate_country(
            code,
            expected_name,
            data,
            report,
            metadata_counter,
        )
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

    metadata_rows = metadata_counter.get("rows", 0)
    metadata_with_poster = metadata_counter.get("with_poster", 0)
    poster_coverage = (
        metadata_with_poster / metadata_rows
        if metadata_rows
        else 0.0
    )

    if metadata_rows == 0:
        report.error("No browser-facing rows were checked for metadata")
    elif poster_coverage < 0.70:
        report.warning(
            f"Overall poster coverage is low across all country rows: "
            f"{metadata_with_poster}/{metadata_rows} ({poster_coverage:.1%})"
        )

    priority_results = validate_priority_poster_coverage(
        global_data,
        country_payloads,
        report,
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
        f"- Browser metadata posters across all rows: "
        f"{metadata_with_poster}/{metadata_rows} ({poster_coverage:.1%})"
    )
    print(
        f"- Metadata confidence: "
        f"high={metadata_counter.get('high_confidence', 0)} "
        f"medium={metadata_counter.get('medium_confidence', 0)}"
    )
    for label, with_poster, total, coverage in priority_results:
        print(
            f"- Priority poster coverage {label}: "
            f"{with_poster}/{total} ({coverage:.1%})"
        )
    print(
        f"- Freshness limits: week <= {MAX_WEEK_AGE_DAYS} days, "
        f"generated_at <= {MAX_GENERATED_AGE_HOURS} hours, "
        f"metadata <= {MAX_METADATA_AGE_HOURS} hours"
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
    print("- browser-facing metadata is present in committed JSON")
    print("- metadata rows passed strict-match version and confidence validation")
    print("- priority user-facing poster coverage is within the configured minimum")
    print("- data and metadata freshness are within the configured limits")
    print()
    print("Result: HEALTHY")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
