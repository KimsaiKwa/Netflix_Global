#!/usr/bin/env python3
"""Generate a compact Netflix Global Top 10 JSON from Netflix Tudum data.

Netflix publishes four official weekly global charts:
- Films (English)
- Films (Non-English)
- TV (English)
- TV (Non-English)

This script preserves those four official charts and also derives:
- films: combined Movies Top 10
- tv: combined TV Top 10

The derived lists are ranked by Netflix's official weekly_views metric.
No API key is required.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


TSV_URLS = [
    "https://top10.netflix.com/data/all-weeks-global.tsv",
    "https://www.netflix.com/tudum/top10/data/all-weeks-global.tsv",
]

USER_AGENTS = [
    "NetflixGlobalMirror/1.0 (+https://github.com/KimsaiKwa/Netflix_Global)",
    "python-urllib/3 NetflixGlobalMirror",
    "Mozilla/5.0 NetflixGlobalMirror",
]

EXPECTED_CATEGORIES = [
    "Films (English)",
    "Films (Non-English)",
    "TV (English)",
    "TV (Non-English)",
]

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "global.json"


def to_int(value: str | None) -> int | None:
    value = (value or "").replace(",", "").strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def to_float(value: str | None) -> float | None:
    value = (value or "").replace(",", "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def fetch_tsv() -> tuple[str, str]:
    errors: list[str] = []

    for attempt in range(1, 4):
        for url in TSV_URLS:
            for ua in USER_AGENTS:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": ua,
                        "Accept": "text/tab-separated-values,text/plain,*/*",
                    },
                )

                try:
                    with urllib.request.urlopen(req, timeout=180) as response:
                        raw = response.read()
                        text = raw.decode("utf-8-sig", errors="replace")
                        final_url = response.geturl()

                    first_line = text.splitlines()[0] if text else ""
                    if "show_title" not in first_line or "weekly_rank" not in first_line:
                        errors.append(
                            f"attempt={attempt} url={url} ua={ua!r}: "
                            f"unexpected response header {first_line[:160]!r}"
                        )
                        continue

                    return text, final_url

                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                    errors.append(
                        f"attempt={attempt} url={url} ua={ua!r}: "
                        f"{type(exc).__name__}: {exc}"
                    )

        if attempt < 3:
            time.sleep(attempt * 5)

    raise RuntimeError(
        "Unable to download Netflix global Top 10 TSV.\n"
        + "\n".join(errors[-18:])
    )


def latest_week_rows(tsv_text: str) -> tuple[str, list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    rows = list(reader)
    header = list(reader.fieldnames or [])

    required = {
        "week",
        "category",
        "weekly_rank",
        "show_title",
        "weekly_views",
        "weekly_hours_viewed",
        "cumulative_weeks_in_top_10",
    }

    missing = sorted(required.difference(header))
    if missing:
        raise RuntimeError(
            "Netflix TSV schema changed; missing columns: "
            + ", ".join(missing)
        )

    weeks = [
        (row.get("week") or "").strip()
        for row in rows
        if (row.get("week") or "").strip()
    ]
    if not weeks:
        raise RuntimeError("Netflix TSV contains no week values")

    latest_week = max(weeks)
    latest = [
        row
        for row in rows
        if (row.get("week") or "").strip() == latest_week
    ]

    return latest_week, latest


def normalize_row(row: dict[str, str]) -> dict:
    season = (row.get("season_title") or "").strip()
    if season == "N/A":
        season = ""

    return {
        "source_rank": to_int(row.get("weekly_rank")),
        "title": (row.get("show_title") or "").strip(),
        "season": season or None,
        "views": to_int(row.get("weekly_views")),
        "hours_viewed": to_int(row.get("weekly_hours_viewed")),
        "runtime_hours": to_float(row.get("runtime")),
        "weeks_in_top_10": to_int(row.get("cumulative_weeks_in_top_10")),
    }


def build_official_categories(rows: list[dict[str, str]]) -> dict[str, list[dict]]:
    official: dict[str, list[dict]] = {}

    for category in EXPECTED_CATEGORIES:
        items = [
            normalize_row(row)
            for row in rows
            if (row.get("category") or "").strip() == category
        ]

        items = [
            item
            for item in items
            if item["title"] and item["source_rank"] is not None
        ]

        items.sort(key=lambda item: item["source_rank"])
        official[category] = items[:10]

    incomplete = {
        category: len(items)
        for category, items in official.items()
        if len(items) != 10
    }

    if incomplete:
        raise RuntimeError(
            f"Latest Netflix global charts are incomplete: {incomplete}"
        )

    if any(
        item["views"] is None
        for items in official.values()
        for item in items
    ):
        raise RuntimeError(
            "weekly_views is missing from one or more latest-week rows; "
            "refusing to silently change the merge methodology"
        )

    return official


def combine_top10(
    official: dict[str, list[dict]],
    categories: tuple[str, str],
) -> list[dict]:
    candidates: list[dict] = []

    for category in categories:
        for item in official[category]:
            candidates.append(
                {
                    **item,
                    "source_category": category,
                }
            )

    candidates.sort(
        key=lambda item: (
            -(item["views"] or 0),
            -(item["hours_viewed"] or 0),
            item["source_rank"] or 999,
            item["title"].casefold(),
            (item["season"] or "").casefold(),
        )
    )

    return [
        {
            "rank": rank,
            **item,
        }
        for rank, item in enumerate(candidates[:10], start=1)
    ]


def build_payload(tsv_text: str, source_url: str) -> dict:
    week, rows = latest_week_rows(tsv_text)
    official = build_official_categories(rows)

    films = combine_top10(
        official,
        ("Films (English)", "Films (Non-English)"),
    )
    tv = combine_top10(
        official,
        ("TV (English)", "TV (Non-English)"),
    )

    return {
        "week": week,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Netflix Tudum Top 10",
        "source_url": source_url,
        "derived": True,
        "methodology": (
            "Netflix publishes four official global weekly charts: "
            "Films (English), Films (Non-English), TV (English), and "
            "TV (Non-English). The films and tv arrays merge the two "
            "language charts within each medium and rank candidates by "
            "Netflix weekly_views descending. hours_viewed, source_rank, "
            "and title are deterministic tie-breakers."
        ),
        "official_categories": official,
        "films": films,
        "tv": tv,
    }


def write_json(payload: dict) -> None:
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, OUTPUT)


def main() -> int:
    tsv_text, source_url = fetch_tsv()
    payload = build_payload(tsv_text, source_url)
    write_json(payload)

    print(
        f"Netflix global Top 10 updated: week={payload['week']} "
        f"films={len(payload['films'])} tv={len(payload['tv'])}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
