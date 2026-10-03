#!/usr/bin/env python3
"""Generate compact Netflix country Top 10 JSON files from Netflix Tudum data.

This repository only needs:
- United States
- South Korea
- Japan
- the 20 European markets used by the Douban userscript's Europe aggregate

The output schema intentionally matches the country JSON shape already used by the
userscript so migration requires only changing the raw GitHub base URL.

No API key is required.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from pathlib import Path


TSV_URL = "https://top10.netflix.com/data/all-weeks-countries.tsv"

TARGET_COUNTRIES = {
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

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "countries"


def _to_int(value: str | None) -> int:
    value = (value or "").replace(",", "").strip()
    if not value:
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def load_tsv() -> str:
    local_file = os.environ.get("NETFLIX_COUNTRIES_TSV_FILE", "").strip()

    if not local_file:
        raise RuntimeError(
            "NETFLIX_COUNTRIES_TSV_FILE is required in the GitHub Actions workflow"
        )

    path = Path(local_file)
    if not path.exists():
        raise RuntimeError(f"NETFLIX_COUNTRIES_TSV_FILE does not exist: {path}")

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    first_line = text.splitlines()[0] if text else ""

    required_header_terms = [
        "country_name",
        "country_iso2",
        "week",
        "weekly_rank",
        "show_title",
    ]

    if not all(term in first_line for term in required_header_terms):
        raise RuntimeError(
            f"Netflix country TSV has unexpected header: {first_line[:240]!r}"
        )

    return text


def category_kind(category: str) -> str | None:
    category = (category or "").strip()

    if category.startswith("Films"):
        return "films"

    if category.startswith("TV"):
        return "tv"

    return None


def build_country_payloads(tsv_text: str) -> tuple[str, dict[str, dict]]:
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    rows = list(reader)
    header = set(reader.fieldnames or [])

    required = {
        "country_name",
        "country_iso2",
        "week",
        "category",
        "weekly_rank",
        "show_title",
        "season_title",
        "cumulative_weeks_in_top_10",
    }

    missing = sorted(required.difference(header))
    if missing:
        raise RuntimeError(
            "Netflix country TSV schema changed; missing columns: "
            + ", ".join(missing)
        )

    weeks = [
        (row.get("week") or "").strip()
        for row in rows
        if (row.get("week") or "").strip()
    ]

    if not weeks:
        raise RuntimeError("Netflix country TSV contains no week values")

    latest_week = max(weeks)

    grouped: dict[str, dict] = {}

    for row in rows:
        if (row.get("week") or "").strip() != latest_week:
            continue

        code = (row.get("country_iso2") or "").strip().upper()

        if code not in TARGET_COUNTRIES:
            continue

        kind = category_kind(row.get("category") or "")
        if kind is None:
            continue

        rank = _to_int(row.get("weekly_rank"))
        title = (row.get("show_title") or "").strip()

        if not (1 <= rank <= 10) or not title:
            continue

        payload = grouped.setdefault(
            code,
            {
                "week": latest_week,
                "country_iso2": code,
                "country_name": (row.get("country_name") or "").strip()
                or TARGET_COUNTRIES[code],
                "films": [],
                "tv": [],
            },
        )

        entry = {
            "rank": rank,
            "title": title,
            "weeks_in_top_10": _to_int(
                row.get("cumulative_weeks_in_top_10")
            ),
        }

        if kind == "tv":
            season = (row.get("season_title") or "").strip()
            if season and season != "N/A":
                entry["season"] = season

        payload[kind].append(entry)

    missing_countries = sorted(set(TARGET_COUNTRIES).difference(grouped))
    if missing_countries:
        raise RuntimeError(
            "Latest Netflix week is missing target countries: "
            + ", ".join(missing_countries)
        )

    for code, payload in grouped.items():
        payload["films"].sort(key=lambda item: item["rank"])
        payload["tv"].sort(key=lambda item: item["rank"])

        payload["films"] = payload["films"][:10]
        payload["tv"] = payload["tv"][:10]

        if len(payload["films"]) != 10 or len(payload["tv"]) != 10:
            raise RuntimeError(
                f"{code} latest week incomplete: "
                f"films={len(payload['films'])} tv={len(payload['tv'])}"
            )

    return latest_week, grouped


def write_payloads(payloads: dict[str, dict]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    expected_names = {f"{code.lower()}.json" for code in TARGET_COUNTRIES}

    # Remove stale JSONs left by an older target set.
    for path in OUTPUT_DIR.glob("*.json"):
        if path.name not in expected_names:
            path.unlink()

    for code, payload in sorted(payloads.items()):
        path = OUTPUT_DIR / f"{code.lower()}.json"
        temp = path.with_suffix(".json.tmp")

        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        os.replace(temp, path)


def main() -> int:
    text = load_tsv()
    week, payloads = build_country_payloads(text)
    write_payloads(payloads)

    print(
        f"Netflix country Top 10 updated: week={week} "
        f"countries={len(payloads)}"
    )

    for code in sorted(payloads):
        payload = payloads[code]
        print(
            f"{code}: {payload['country_name']} "
            f"films={len(payload['films'])} tv={len(payload['tv'])}"
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
