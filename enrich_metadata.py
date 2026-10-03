#!/usr/bin/env python3
"""Enrich Netflix Top 10 mirror JSON files with browser-ready metadata.

Ranking data remains untouched and continues to come from Netflix Tudum.

This script adds display metadata used by the Douban userscript:
- cn_title: Chinese title only when matched from Douban
- year
- poster_candidates: Douban poster first, JustWatch poster second
- douban_id / douban_url
- imdb_id / tmdb_id

The userscript can therefore render the final JSON directly without making live
Douban/JustWatch metadata requests in the browser.

Successful metadata is cached in metadata_cache.json and is never replaced by an
empty transient network result.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
GLOBAL_PATH = ROOT / "global.json"
COUNTRIES_DIR = ROOT / "countries"
CACHE_PATH = ROOT / "metadata_cache.json"

CACHE_VERSION = 1
METADATA_VERSION = 1

META_TTL_SECONDS = 30 * 24 * 60 * 60
PARTIAL_TTL_SECONDS = 7 * 24 * 60 * 60
NEGATIVE_TTL_SECONDS = 6 * 60 * 60
REQUEST_TIMEOUT = 20
WORKERS = max(1, min(int(os.environ.get("METADATA_WORKERS", "4")), 8))
FORCE = os.environ.get("METADATA_FORCE", "").strip() == "1"

DOUBAN_SUGGEST = "https://movie.douban.com/j/subject_suggest?q={query}"
DOUBAN_DETAIL = {
    "movie": "https://m.douban.com/rexxar/api/v2/movie/{id}",
    "tv": "https://m.douban.com/rexxar/api/v2/tv/{id}",
}
JUSTWATCH_GRAPHQL = "https://apis.justwatch.com/graphql"

JUSTWATCH_QUERY = """
query SearchMeta(
  $f: TitleFilter,
  $c: Country!,
  $l: Language!,
  $n: Int!
) {
  popularTitles(
    filter: $f,
    country: $c,
    language: $l,
    first: $n
  ) {
    edges {
      node {
        id
        objectType
        content(country: $c, language: $l) {
          title
          originalTitle
          originalReleaseYear
          fullPath
          externalIds {
            imdbId
            tmdbId
          }
          posterUrl(format: JPG)
        }
      }
    }
  }
}
""".strip()

_print_lock = threading.Lock()
_jw_rate_lock = threading.Lock()
_jw_last_request_at = 0.0
JW_MIN_INTERVAL_SECONDS = float(
    os.environ.get("JUSTWATCH_MIN_INTERVAL_SECONDS", "0.75")
)


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return time.time()


def clean(value: Any, max_len: int = 1000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:max_len]


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("’", "'").replace("‘", "'").replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def has_han(value: Any) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", str(value or "")))


def parse_year(value: Any) -> str:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else ""


def normalize_image_url(value: Any) -> str:
    url = clean(value, 1500)
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    return re.sub(r"^http://", "https://", url, flags=re.I)


def normalize_justwatch_poster(value: Any) -> str:
    url = clean(value, 1500)
    if not url:
        return ""
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://images.justwatch.com" + url
    url = re.sub(r"\{profile\}", "s332", url, flags=re.I)
    return normalize_image_url(url)


def unique_strings(values: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()

    for value in values:
        text = clean(value, 1500)
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)

    return output


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return data


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = REQUEST_TIMEOUT,
    attempts: int = 2,
) -> Any:
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    request_headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/154 Safari/537.36"
        ),
        **(headers or {}),
    }

    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=request_headers,
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
            return json.loads(raw.decode("utf-8", errors="replace"))

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc

            if attempt < attempts:
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = max(float(retry_after), 2.0)
                    except (TypeError, ValueError):
                        delay = min(4.0 * attempt, 15.0)

                    log(
                        f"WARN HTTP 429; backing off {delay:.1f}s "
                        f"(attempt {attempt}/{attempts})"
                    )
                    time.sleep(delay)
                else:
                    time.sleep(0.8 * attempt)

    raise RuntimeError(
        f"{method} {url} failed: {type(last_error).__name__}: {last_error}"
    )


def score_title(candidate_title: str, candidate_subtitle: str, target: str) -> int:
    a = normalize_text(candidate_title)
    b = normalize_text(candidate_subtitle)
    q = normalize_text(target)

    if not q:
        return 0

    if a == q or b == q:
        return 110

    if (
        (a and a.startswith(q))
        or (b and b.startswith(q))
        or (a and q.startswith(a))
        or (b and q.startswith(b))
    ):
        return 70

    if (
        (a and q in a)
        or (b and q in b)
        or (a and a in q)
        or (b and b in q)
    ):
        return 45

    return 0


def douban_type_score(candidate: dict[str, Any], media_type: str) -> int:
    value = clean(candidate.get("type"), 50).lower()

    if not value:
        return 0

    if media_type == "movie":
        if value == "movie" or "movie" in value:
            return 22
        if "tv" in value or "episode" in value or "series" in value:
            return -35
    else:
        if "tv" in value or "episode" in value or "series" in value:
            return 22
        if value == "movie":
            return -25

    return 0


def select_douban_candidate(
    candidates: list[dict[str, Any]],
    title: str,
    media_type: str,
    search_term: str,
    year_hint: str = "",
) -> tuple[dict[str, Any], int] | None:
    best: dict[str, Any] | None = None
    best_score = -10_000

    for candidate in candidates:
        candidate_title = clean(candidate.get("title"), 220)
        subtitle = clean(candidate.get("sub_title"), 220)

        if not candidate_title and not subtitle:
            continue

        score = douban_type_score(candidate, media_type)
        score += max(
            score_title(candidate_title, subtitle, title),
            score_title(candidate_title, subtitle, search_term),
        )

        candidate_year = parse_year(
            candidate.get("year") or candidate.get("card_subtitle") or ""
        )
        expected_year = parse_year(year_hint)

        if candidate_year and expected_year:
            delta = abs(int(candidate_year) - int(expected_year))
            if delta == 0:
                score += 14
            elif delta == 1:
                score += 7
            elif delta >= 3:
                score -= 12

        if candidate.get("id"):
            score += 5
        if candidate.get("img") or candidate.get("pic"):
            score += 4

        if score > best_score:
            best_score = score
            best = candidate

    if best is None or best_score < 40:
        return None

    return best, best_score


def search_douban(query: str) -> list[dict[str, Any]]:
    if not clean(query):
        return []

    url = DOUBAN_SUGGEST.format(
        query=urllib.parse.quote(query, safe="")
    )

    data = request_json(
        url,
        headers={
            "Referer": "https://movie.douban.com/",
        },
    )

    return data if isinstance(data, list) else []



def ordered_justwatch_countries(country_hints: set[str]) -> list[str]:
    hints = {
        clean(code, 2).upper()
        for code in country_hints
        if clean(code, 2)
    }

    ordered: list[str] = []

    # Local catalog first for Asian markets where the US search index often
    # misses local titles; otherwise prefer the first market where Netflix
    # actually ranked the title.
    for code in ("KR", "JP"):
        if code in hints and code not in ordered:
            ordered.append(code)

    for code in sorted(hints):
        if code not in ordered:
            ordered.append(code)

    for fallback in ("US", "GB", "KR", "JP"):
        if fallback not in ordered:
            ordered.append(fallback)

    return ordered[:4]


def search_justwatch_multi(
    title: str,
    media_type: str,
    year_hint: str,
    country_hints: set[str],
) -> dict[str, Any] | None:
    best_without_poster: dict[str, Any] | None = None

    for country in ordered_justwatch_countries(country_hints):
        try:
            result = search_justwatch(
                title,
                media_type,
                year_hint,
                country=country,
            )
        except Exception as exc:
            log(
                f"WARN justwatch {country} {media_type} {title!r}: {exc}"
            )
            continue

        if not result:
            continue

        result["country"] = country

        if result.get("poster"):
            return result

        if best_without_poster is None:
            best_without_poster = result

    return best_without_poster

def fetch_douban_detail_poster(douban_id: str, media_type: str) -> str:
    if not douban_id:
        return ""

    try:
        data = request_json(
            DOUBAN_DETAIL[media_type].format(id=douban_id),
            headers={
                "Referer": "https://m.douban.com/",
            },
            attempts=1,
        )
    except Exception:
        return ""

    if not isinstance(data, dict):
        return ""

    pic = data.get("pic")
    pic = pic if isinstance(pic, dict) else {}

    return normalize_image_url(
        pic.get("large")
        or pic.get("normal")
        or pic.get("small")
        or data.get("cover_url")
        or ""
    )


def build_douban_meta(
    candidate: dict[str, Any],
    media_type: str,
    source_title: str,
) -> dict[str, Any]:
    candidate_title = clean(candidate.get("title"), 220)
    subtitle = clean(candidate.get("sub_title"), 220)

    cn_title = ""

    if has_han(candidate_title) and normalize_text(candidate_title) != normalize_text(
        source_title
    ):
        cn_title = candidate_title
    elif has_han(subtitle) and normalize_text(subtitle) != normalize_text(source_title):
        cn_title = subtitle

    douban_id = clean(candidate.get("id"), 30)

    poster = normalize_image_url(
        candidate.get("img")
        or candidate.get("pic")
        or candidate.get("cover")
        or ""
    )

    if not poster and douban_id:
        poster = fetch_douban_detail_poster(douban_id, media_type)

    douban_url = clean(candidate.get("url"), 1000)
    if not douban_url and douban_id:
        douban_url = f"https://movie.douban.com/subject/{douban_id}/"

    return {
        "matched": True,
        "cn_title": cn_title,
        "year": clean(
            candidate.get("year") or parse_year(candidate.get("card_subtitle")), 8
        ),
        "poster": poster,
        "douban_id": douban_id,
        "douban_url": douban_url,
    }


def score_justwatch_node(
    node: dict[str, Any],
    title: str,
    media_type: str,
    year_hint: str,
) -> int:
    expected_type = "MOVIE" if media_type == "movie" else "SHOW"
    object_type = clean(node.get("objectType"), 30).upper()

    if object_type and object_type != expected_type:
        return -1000

    content = node.get("content")
    content = content if isinstance(content, dict) else {}

    localized = clean(content.get("title"), 220)
    original = clean(content.get("originalTitle"), 220)

    score = max(
        score_title(original, "", title),
        score_title(localized, "", title),
    )

    year = parse_year(content.get("originalReleaseYear"))
    expected_year = parse_year(year_hint)

    if year and expected_year:
        delta = abs(int(year) - int(expected_year))
        if delta == 0:
            score += 14
        elif delta == 1:
            score += 6
        elif delta >= 3:
            score -= 10

    if content.get("posterUrl"):
        score += 4

    external = content.get("externalIds")
    external = external if isinstance(external, dict) else {}

    if external.get("tmdbId"):
        score += 3
    if external.get("imdbId"):
        score += 3

    return score


def wait_for_justwatch_slot() -> None:
    global _jw_last_request_at

    with _jw_rate_lock:
        now = time.monotonic()
        wait = JW_MIN_INTERVAL_SECONDS - (now - _jw_last_request_at)

        if wait > 0:
            time.sleep(wait)

        _jw_last_request_at = time.monotonic()


def search_justwatch(
    title: str,
    media_type: str,
    year_hint: str = "",
    country: str = "US",
) -> dict[str, Any] | None:
    object_type = "MOVIE" if media_type == "movie" else "SHOW"

    payload = {
        "query": JUSTWATCH_QUERY,
        "variables": {
            "f": {
                "searchQuery": title,
                "objectTypes": [object_type],
                "includeTitlesWithoutUrl": True,
            },
            "c": country.upper(),
            "l": "zh",
            "n": 8,
        },
    }

    wait_for_justwatch_slot()

    data = request_json(
        JUSTWATCH_GRAPHQL,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://www.justwatch.com",
            "Referer": "https://www.justwatch.com/",
            "X-Platform": "WEB",
        },
        body=payload,
        attempts=5,
    )

    if not isinstance(data, dict):
        return None

    if data.get("errors"):
        raise RuntimeError(
            "JustWatch GraphQL error: "
            + clean(data["errors"][0].get("message") if data["errors"] else "", 300)
        )

    edges = (
        ((data.get("data") or {}).get("popularTitles") or {}).get("edges") or []
    )

    nodes = [
        edge.get("node")
        for edge in edges
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
    ]

    best: dict[str, Any] | None = None
    best_score = -10_000

    for node in nodes:
        score = score_justwatch_node(node, title, media_type, year_hint)
        if score > best_score:
            best_score = score
            best = node

    if best is None or best_score < 45:
        return None

    content = best.get("content")
    content = content if isinstance(content, dict) else {}

    localized = clean(content.get("title"), 220)
    original = clean(content.get("originalTitle") or title, 220)

    external = content.get("externalIds")
    external = external if isinstance(external, dict) else {}

    localized_alias = (
        localized
        if localized and normalize_text(localized) != normalize_text(original)
        else ""
    )

    return {
        "matched": True,
        "score": best_score,
        "node_id": clean(best.get("id"), 120),
        "localized_alias": localized_alias,
        "original_title": original,
        "year": parse_year(content.get("originalReleaseYear")),
        "poster": normalize_justwatch_poster(content.get("posterUrl")),
        "imdb_id": clean(external.get("imdbId"), 40),
        "tmdb_id": clean(external.get("tmdbId"), 40),
    }


def empty_meta() -> dict[str, Any]:
    return {
        "cn_title": "",
        "year": "",
        "poster_candidates": [],
        "douban_matched": False,
        "justwatch_matched": False,
        "douban_id": "",
        "douban_url": "",
        "imdb_id": "",
        "tmdb_id": "",
        "douban_poster": "",
        "justwatch_poster": "",
        "localized_alias": "",
    }


def sanitize_meta(meta: Any) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return empty_meta()

    cn_title = clean(meta.get("cn_title") or meta.get("cnTitle"), 220)
    if cn_title == "暂无中文译名":
        cn_title = ""

    douban_poster = normalize_image_url(
        meta.get("douban_poster")
        or meta.get("doubanPoster")
        or ""
    )
    justwatch_poster = normalize_image_url(
        meta.get("justwatch_poster")
        or meta.get("justWatchPoster")
        or ""
    )

    posters = unique_strings(
        list(meta.get("poster_candidates") or meta.get("posterCandidates") or [])
        + [douban_poster, justwatch_poster]
    )

    return {
        "cn_title": cn_title,
        "year": clean(meta.get("year"), 8),
        "poster_candidates": posters,
        "douban_matched": bool(
            meta.get("douban_matched")
            if "douban_matched" in meta
            else meta.get("doubanMatched") or meta.get("matched")
        ),
        "justwatch_matched": bool(
            meta.get("justwatch_matched")
            if "justwatch_matched" in meta
            else meta.get("justWatchMatched")
        ),
        "douban_id": clean(meta.get("douban_id") or meta.get("doubanId"), 30),
        "douban_url": clean(meta.get("douban_url") or meta.get("doubanUrl"), 1000),
        "imdb_id": clean(meta.get("imdb_id") or meta.get("imdbId"), 40),
        "tmdb_id": clean(meta.get("tmdb_id") or meta.get("tmdbId"), 40),
        "douban_poster": douban_poster,
        "justwatch_poster": justwatch_poster,
        "localized_alias": clean(
            meta.get("localized_alias") or meta.get("localizedAlias"), 220
        ),
    }


def meta_useful(meta: dict[str, Any]) -> bool:
    return bool(
        meta.get("cn_title")
        or meta.get("year")
        or meta.get("douban_matched")
        or meta.get("justwatch_matched")
        or meta.get("poster_candidates")
    )


def merge_meta(old: Any, fresh: Any) -> dict[str, Any]:
    old_meta = sanitize_meta(old)
    fresh_meta = sanitize_meta(fresh)

    return {
        "cn_title": fresh_meta["cn_title"] or old_meta["cn_title"],
        "year": fresh_meta["year"] or old_meta["year"],
        "poster_candidates": unique_strings(
            fresh_meta["poster_candidates"]
            + old_meta["poster_candidates"]
        ),
        "douban_matched": bool(
            fresh_meta["douban_matched"] or old_meta["douban_matched"]
        ),
        "justwatch_matched": bool(
            fresh_meta["justwatch_matched"] or old_meta["justwatch_matched"]
        ),
        "douban_id": fresh_meta["douban_id"] or old_meta["douban_id"],
        "douban_url": fresh_meta["douban_url"] or old_meta["douban_url"],
        "imdb_id": fresh_meta["imdb_id"] or old_meta["imdb_id"],
        "tmdb_id": fresh_meta["tmdb_id"] or old_meta["tmdb_id"],
        "douban_poster": fresh_meta["douban_poster"] or old_meta["douban_poster"],
        "justwatch_poster": (
            fresh_meta["justwatch_poster"] or old_meta["justwatch_poster"]
        ),
        "localized_alias": (
            fresh_meta["localized_alias"] or old_meta["localized_alias"]
        ),
    }


def resolve_fresh_meta(
    title: str,
    media_type: str,
    year_hint: str = "",
    country_hints: set[str] | None = None,
) -> dict[str, Any]:
    direct_candidates: list[dict[str, Any]] = []

    try:
        direct_candidates = search_douban(title)
    except Exception as exc:
        log(f"WARN douban direct {media_type} {title!r}: {exc}")

    jw: dict[str, Any] | None = None

    try:
        jw = search_justwatch_multi(
            title,
            media_type,
            year_hint,
            country_hints or {"US"},
        )
    except Exception as exc:
        log(f"WARN justwatch multi {media_type} {title!r}: {exc}")

    best_douban = select_douban_candidate(
        direct_candidates,
        title,
        media_type,
        title,
        (jw or {}).get("year") or year_hint,
    )

    localized_alias = clean((jw or {}).get("localized_alias"), 220)

    if localized_alias and normalize_text(localized_alias) != normalize_text(title):
        try:
            second_candidates = search_douban(localized_alias)
            second_best = select_douban_candidate(
                second_candidates,
                title,
                media_type,
                localized_alias,
                (jw or {}).get("year") or year_hint,
            )

            if second_best and (
                best_douban is None or second_best[1] > best_douban[1]
            ):
                best_douban = second_best
        except Exception as exc:
            log(f"WARN douban alias {media_type} {title!r}: {exc}")

    db: dict[str, Any] | None = None

    if best_douban:
        db = build_douban_meta(best_douban[0], media_type, title)

    douban_poster = normalize_image_url((db or {}).get("poster"))
    justwatch_poster = normalize_image_url((jw or {}).get("poster"))

    # Display Chinese titles must come from Douban only.
    # JustWatch's zh localization is used only as a second Douban search term.
    return {
        "cn_title": clean((db or {}).get("cn_title"), 220),
        "year": clean((db or {}).get("year"), 8)
        or clean((jw or {}).get("year"), 8)
        or clean(year_hint, 8),
        "poster_candidates": unique_strings(
            [douban_poster, justwatch_poster]
        ),
        "douban_matched": bool((db or {}).get("matched")),
        "justwatch_matched": bool((jw or {}).get("matched")),
        "douban_id": clean((db or {}).get("douban_id"), 30),
        "douban_url": clean((db or {}).get("douban_url"), 1000),
        "imdb_id": clean((jw or {}).get("imdb_id"), 40),
        "tmdb_id": clean((jw or {}).get("tmdb_id"), 40),
        "douban_poster": douban_poster,
        "justwatch_poster": justwatch_poster,
        "localized_alias": localized_alias,
    }


def load_cache() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return {
            "version": CACHE_VERSION,
            "updated_at": None,
            "items": {},
        }

    try:
        data = load_json(CACHE_PATH)
    except Exception as exc:
        log(f"WARN unable to read metadata cache; starting empty: {exc}")
        return {
            "version": CACHE_VERSION,
            "updated_at": None,
            "items": {},
        }

    items = data.get("items")
    if not isinstance(items, dict):
        items = {}

    return {
        "version": CACHE_VERSION,
        "updated_at": data.get("updated_at"),
        "items": items,
    }


def cache_key(media_type: str, title: str) -> str:
    return f"{media_type}|{normalize_text(title)}"


def get_stable_meta(
    cache: dict[str, Any],
    media_type: str,
    title: str,
    year_hint: str,
    country_hints: set[str],
) -> tuple[dict[str, Any], bool]:
    key = cache_key(media_type, title)
    items = cache["items"]

    record = items.get(key)
    record = record if isinstance(record, dict) else {}

    existing = sanitize_meta(record.get("meta"))
    saved_at = record.get("saved_at")

    try:
        saved_at_value = float(saved_at)
    except (TypeError, ValueError):
        saved_at_value = 0.0

    has_poster = bool(existing.get("poster_candidates"))
    has_cn_title = bool(existing.get("cn_title"))

    if has_poster and has_cn_title:
        ttl = META_TTL_SECONDS
    elif has_poster or has_cn_title:
        ttl = PARTIAL_TTL_SECONDS
    else:
        ttl = NEGATIVE_TTL_SECONDS
    age = now_ts() - saved_at_value if saved_at_value else float("inf")

    if not FORCE and saved_at_value and age < ttl:
        return existing, True

    fresh = empty_meta()

    try:
        fresh = resolve_fresh_meta(
            title,
            media_type,
            year_hint,
            country_hints,
        )
    except Exception as exc:
        log(f"WARN metadata resolve {media_type} {title!r}: {exc}")

    merged = merge_meta(existing, fresh)

    if meta_useful(merged):
        items[key] = {
            "saved_at": now_ts(),
            "title": title,
            "media_type": media_type,
            "meta": merged,
        }
        return merged, False

    if meta_useful(existing):
        return existing, True

    items[key] = {
        "saved_at": now_ts(),
        "title": title,
        "media_type": media_type,
        "meta": empty_meta(),
    }

    return empty_meta(), False


def iter_unique_titles(
    global_data: dict[str, Any],
    country_data: dict[Path, dict[str, Any]],
) -> list[tuple[str, str, str, set[str]]]:
    items: dict[str, dict[str, Any]] = {}

    def add(
        media_type: str,
        row: Any,
        country_hint: str,
    ) -> None:
        if not isinstance(row, dict):
            return

        title = clean(row.get("title"), 220)
        if not title:
            return

        key = cache_key(media_type, title)

        item = items.setdefault(
            key,
            {
                "media_type": media_type,
                "title": title,
                "year_hint": clean(row.get("year"), 8),
                "country_hints": set(),
            },
        )

        if not item["year_hint"]:
            item["year_hint"] = clean(row.get("year"), 8)

        if country_hint:
            item["country_hints"].add(country_hint.upper())

    for row in global_data.get("films") or []:
        add("movie", row, "US")

    for row in global_data.get("tv") or []:
        add("tv", row, "US")

    for data in country_data.values():
        code = clean(data.get("country_iso2"), 2).upper()

        for row in data.get("films") or []:
            add("movie", row, code)

        for row in data.get("tv") or []:
            add("tv", row, code)

    return [
        (
            item["media_type"],
            item["title"],
            item["year_hint"],
            set(item["country_hints"]),
        )
        for item in items.values()
    ]


def apply_meta_to_rows(
    rows: Any,
    media_type: str,
    resolved: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(rows, list):
        return

    for row in rows:
        if not isinstance(row, dict):
            continue

        title = clean(row.get("title"), 220)
        meta = resolved.get(cache_key(media_type, title), empty_meta())

        row["cn_title"] = meta["cn_title"]
        row["year"] = meta["year"]
        row["poster_candidates"] = meta["poster_candidates"]
        row["douban_id"] = meta["douban_id"]
        row["douban_url"] = meta["douban_url"]
        row["imdb_id"] = meta["imdb_id"]
        row["tmdb_id"] = meta["tmdb_id"]


def metadata_stats(
    global_data: dict[str, Any],
    country_data: dict[Path, dict[str, Any]],
) -> dict[str, int]:
    rows: list[dict[str, Any]] = []

    rows.extend(
        row for row in (global_data.get("films") or []) if isinstance(row, dict)
    )
    rows.extend(
        row for row in (global_data.get("tv") or []) if isinstance(row, dict)
    )

    for data in country_data.values():
        rows.extend(row for row in (data.get("films") or []) if isinstance(row, dict))
        rows.extend(row for row in (data.get("tv") or []) if isinstance(row, dict))

    return {
        "rows": len(rows),
        "with_poster": sum(
            1 for row in rows if isinstance(row.get("poster_candidates"), list)
            and len(row["poster_candidates"]) > 0
        ),
        "with_cn_title": sum(1 for row in rows if clean(row.get("cn_title"), 220)),
        "with_douban": sum(1 for row in rows if clean(row.get("douban_id"), 30)),
        "with_tmdb": sum(1 for row in rows if clean(row.get("tmdb_id"), 40)),
    }


def main() -> int:
    if not GLOBAL_PATH.exists():
        raise RuntimeError("global.json is missing")

    global_data = load_json(GLOBAL_PATH)
    existing_metadata_enriched_at = clean(
        global_data.get("metadata_enriched_at"),
        80,
    )
    existing_metadata_version = global_data.get("metadata_version")

    country_paths = sorted(COUNTRIES_DIR.glob("*.json"))
    if not country_paths:
        raise RuntimeError("countries/*.json files are missing")

    country_data = {
        path: load_json(path)
        for path in country_paths
    }

    cache = load_cache()

    titles = iter_unique_titles(global_data, country_data)

    log(
        f"Metadata enrichment start: unique_titles={len(titles)} "
        f"workers={WORKERS} force={FORCE}"
    )

    resolved: dict[str, dict[str, Any]] = {}
    resolved_lock = threading.Lock()

    def work(
        item: tuple[str, str, str, set[str]],
    ) -> tuple[str, dict[str, Any], bool]:
        media_type, title, year_hint, country_hints = item
        meta, cached = get_stable_meta(
            cache,
            media_type,
            title,
            year_hint,
            country_hints,
        )
        return cache_key(media_type, title), meta, cached

    cached_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [
            executor.submit(work, item)
            for item in titles
        ]

        for index, future in enumerate(
            concurrent.futures.as_completed(futures),
            start=1,
        ):
            key, meta, cached = future.result()

            with resolved_lock:
                resolved[key] = meta

            if cached:
                cached_count += 1

            if index % 25 == 0 or index == len(futures):
                log(
                    f"Metadata progress: {index}/{len(futures)} "
                    f"cache_hits={cached_count}"
                )

    apply_meta_to_rows(global_data.get("films"), "movie", resolved)
    apply_meta_to_rows(global_data.get("tv"), "tv", resolved)

    did_query = cached_count < len(titles)

    if (
        not did_query
        and existing_metadata_version == METADATA_VERSION
        and existing_metadata_enriched_at
    ):
        enriched_at = existing_metadata_enriched_at
    else:
        enriched_at = now_iso()

    global_data["metadata_version"] = METADATA_VERSION
    global_data["metadata_enriched_at"] = enriched_at

    for path, data in country_data.items():
        apply_meta_to_rows(data.get("films"), "movie", resolved)
        apply_meta_to_rows(data.get("tv"), "tv", resolved)

        data["metadata_version"] = METADATA_VERSION
        data["metadata_enriched_at"] = enriched_at

    stats = metadata_stats(global_data, country_data)

    cache["version"] = CACHE_VERSION
    cache["updated_at"] = enriched_at

    write_json_atomic(GLOBAL_PATH, global_data)

    for path, data in country_data.items():
        write_json_atomic(path, data)

    write_json_atomic(CACHE_PATH, cache)

    log(
        "Metadata enrichment complete: "
        f"rows={stats['rows']} "
        f"poster={stats['with_poster']} "
        f"cn_title={stats['with_cn_title']} "
        f"douban={stats['with_douban']} "
        f"tmdb={stats['with_tmdb']} "
        f"cache_hits={cached_count}/{len(titles)}"
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
