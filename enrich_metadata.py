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
import functools
import hashlib
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
VERIFIED_ALIASES_PATH = ROOT / "verified_title_aliases.json"

CACHE_VERSION = 2
METADATA_VERSION = 2
MATCH_VERSION = 2

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
IMDB_SUGGEST = "https://v2.sg.media-imdb.com/suggestion/{bucket}/{query}.json"

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


TITLE_ARTICLES = {"a", "an", "the"}
EDITION_TOKENS = {
    "4k", "uhd", "hdr", "remaster", "remastered",
    "restore", "restored", "restoration", "edition",
}


def canonical_title(value: Any) -> str:
    tokens = [
        token
        for token in normalize_text(value).split()
        if token not in EDITION_TOKENS
    ]
    return " ".join(tokens)


def significant_title_tokens(value: Any) -> list[str]:
    return [
        token
        for token in canonical_title(value).split()
        if token not in TITLE_ARTICLES
    ]


def strict_title_score(candidate: Any, target: Any) -> int:
    """Return a score only for high-confidence title equivalence.

    Substring/prefix-only matching is intentionally rejected. Edition-only
    tokens such as 4K/remastered may differ, but meaningful subtitle/franchise
    tokens (Final, Stay Alive, part names, etc.) must be preserved.
    """
    candidate_norm = normalize_text(candidate)
    target_norm = normalize_text(target)

    if not candidate_norm or not target_norm:
        return 0

    if candidate_norm == target_norm:
        return 140

    candidate_canonical = canonical_title(candidate_norm)
    target_canonical = canonical_title(target_norm)

    if candidate_canonical and candidate_canonical == target_canonical:
        return 132

    candidate_tokens = significant_title_tokens(candidate_canonical)
    target_tokens = significant_title_tokens(target_canonical)

    if not candidate_tokens or not target_tokens:
        return 0

    candidate_set = set(candidate_tokens)
    target_set = set(target_tokens)

    # Every meaningful token from the Netflix title must be present.
    if target_set - candidate_set:
        return 0

    # Do not silently accept a different subtitle/part name from the candidate.
    extra = candidate_set - target_set
    harmless_extra = {"movie", "film"}

    if extra and not extra.issubset(harmless_extra):
        return 0

    if len(target_set) < 3:
        return 0

    return 118


def score_title(candidate_title: str, candidate_subtitle: str, target: str) -> int:
    return max(
        strict_title_score(candidate_title, target),
        strict_title_score(candidate_subtitle, target),
    )


def years_compatible(first: Any, second: Any) -> bool:
    a = parse_year(first)
    b = parse_year(second)

    if not a or not b:
        return True

    return abs(int(a) - int(b)) <= 1


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

        type_score = douban_type_score(candidate, media_type)
        if type_score < 0:
            continue

        title_score = max(
            strict_title_score(candidate_title, title),
            strict_title_score(subtitle, title),
            strict_title_score(candidate_title, search_term),
            strict_title_score(subtitle, search_term),
        )

        if title_score <= 0:
            continue

        candidate_year = parse_year(
            candidate.get("year") or candidate.get("card_subtitle") or ""
        )
        expected_year = parse_year(year_hint)

        if (
            candidate_year
            and expected_year
            and abs(int(candidate_year) - int(expected_year)) > 1
        ):
            continue

        score = title_score + type_score

        if candidate_year and expected_year:
            score += 14 if candidate_year == expected_year else 7

        if candidate.get("id"):
            score += 5
        if candidate.get("img") or candidate.get("pic"):
            score += 4

        if score > best_score:
            best_score = score
            best = candidate

    if best is None or best_score < 118:
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

    title_score = max(
        strict_title_score(original, title),
        strict_title_score(localized, title),
    )

    if title_score <= 0:
        return -1000

    year = parse_year(content.get("originalReleaseYear"))
    expected_year = parse_year(year_hint)

    if (
        year
        and expected_year
        and abs(int(year) - int(expected_year)) > 1
    ):
        return -1000

    score = title_score

    if year and expected_year:
        score += 14 if year == expected_year else 7

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

    if best is None or best_score < 118:
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


def imdb_type_score(item: dict[str, Any], media_type: str) -> int:
    q = clean(item.get("q") or item.get("qid"), 80).lower()

    if not q:
        return 0

    if media_type == "movie":
        if any(token in q for token in ("feature", "movie", "film")):
            return 18
        if any(token in q for token in ("tv", "series", "episode")):
            return -25
    else:
        if any(token in q for token in ("tv", "series", "episode")):
            return 18
        if any(token in q for token in ("feature", "movie", "film")):
            return -20

    return 0


class IdentityRejected(RuntimeError):
    """The expected provider ID was observed with contradictory identity data."""


def search_imdb(
    title: str,
    media_type: str,
    year_hint: str = "",
    imdb_id_hint: str = "",
    *,
    verified_aliases: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    query = clean(imdb_id_hint, 40) or clean(title, 220)
    if not query:
        return None

    bucket = (
        "t"
        if query.lower().startswith("tt")
        else (query[0].lower() if query[0].isalnum() else "x")
    )

    url = IMDB_SUGGEST.format(
        bucket=urllib.parse.quote(bucket, safe=""),
        query=urllib.parse.quote(query, safe=""),
    )

    data = request_json(
        url,
        headers={
            "Referer": "https://www.imdb.com/",
        },
        attempts=3,
    )

    if not isinstance(data, dict):
        return None

    items = data.get("d")
    if not isinstance(items, list):
        return None

    expected_year = parse_year(year_hint)
    exact_id = clean(imdb_id_hint, 40)

    best: dict[str, Any] | None = None
    best_score = -10_000
    rejected_identity = False

    for item in items:
        if not isinstance(item, dict):
            continue

        item_id = clean(item.get("id"), 40)
        item_title = clean(item.get("l"), 220)

        # An ID hint is an identity constraint, never merely a ranking bonus.
        if exact_id and item_id != exact_id:
            continue

        title_score = max(
            strict_title_score(item_title, allowed_title)
            for allowed_title in (title, *verified_aliases)
        )
        if title_score <= 0:
            rejected_identity = rejected_identity or bool(verified_aliases and exact_id and item_title)
            continue

        type_score = imdb_type_score(item, media_type)
        if type_score < 0:
            rejected_identity = rejected_identity or bool(verified_aliases and exact_id)
            continue
        if verified_aliases and type_score == 0:
            continue

        item_year = parse_year(item.get("y"))

        if (
            item_year
            and expected_year
            and abs(int(item_year) - int(expected_year)) > 1
        ):
            rejected_identity = rejected_identity or bool(verified_aliases and exact_id)
            continue

        score = title_score + type_score

        if exact_id and item_id == exact_id:
            score += 50

        if item_year and expected_year:
            score += 14 if item_year == expected_year else 7

        image = item.get("i")
        image = image if isinstance(image, dict) else {}
        image_url = normalize_image_url(image.get("imageUrl"))

        if image_url:
            score += 5

        if score > best_score:
            best_score = score
            best = item

    if best is None or best_score < 118:
        if rejected_identity:
            raise IdentityRejected("Expected IMDb ID failed title/type/year validation")
        return None

    image = best.get("i")
    image = image if isinstance(image, dict) else {}

    return {
        "matched": True,
        "score": best_score,
        "matched_title": clean(best.get("l"), 220),
        "imdb_id": clean(best.get("id"), 40),
        "year": parse_year(best.get("y")),
        "poster": normalize_image_url(image.get("imageUrl")),
    }



def empty_meta() -> dict[str, Any]:
    return {
        "cn_title": "",
        "year": "",
        "poster_candidates": [],
        "douban_matched": False,
        "justwatch_matched": False,
        "imdb_matched": False,
        "douban_id": "",
        "douban_url": "",
        "imdb_id": "",
        "tmdb_id": "",
        "douban_poster": "",
        "justwatch_poster": "",
        "imdb_poster": "",
        "localized_alias": "",
        "confidence": "none",
        "evidence": [],
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
    imdb_poster = normalize_image_url(
        meta.get("imdb_poster")
        or meta.get("imdbPoster")
        or ""
    )

    posters = unique_strings(
        list(meta.get("poster_candidates") or meta.get("posterCandidates") or [])
        + [douban_poster, justwatch_poster, imdb_poster]
    )

    confidence = clean(meta.get("confidence"), 20).lower()
    if confidence not in {"high", "medium", "none"}:
        confidence = "none"

    evidence = [
        clean(item, 80)
        for item in (meta.get("evidence") or [])
        if clean(item, 80)
    ]

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
        "imdb_matched": bool(
            meta.get("imdb_matched")
            if "imdb_matched" in meta
            else meta.get("imdbMatched")
        ),
        "douban_id": clean(meta.get("douban_id") or meta.get("doubanId"), 30),
        "douban_url": clean(meta.get("douban_url") or meta.get("doubanUrl"), 1000),
        "imdb_id": clean(meta.get("imdb_id") or meta.get("imdbId"), 40),
        "tmdb_id": clean(meta.get("tmdb_id") or meta.get("tmdbId"), 40),
        "douban_poster": douban_poster,
        "justwatch_poster": justwatch_poster,
        "imdb_poster": imdb_poster,
        "localized_alias": clean(
            meta.get("localized_alias") or meta.get("localizedAlias"), 220
        ),
        "confidence": confidence,
        "evidence": unique_strings(evidence),
    }


def meta_useful(meta: dict[str, Any]) -> bool:
    return bool(
        meta.get("cn_title")
        or meta.get("year")
        or meta.get("douban_matched")
        or meta.get("justwatch_matched")
        or meta.get("imdb_matched")
        or meta.get("poster_candidates")
    )


def merge_meta(old: Any, fresh: Any) -> dict[str, Any]:
    old_meta = sanitize_meta(old)
    fresh_meta = sanitize_meta(fresh)

    confidence_order = {"none": 0, "medium": 1, "high": 2}
    confidence = (
        fresh_meta["confidence"]
        if confidence_order[fresh_meta["confidence"]]
        >= confidence_order[old_meta["confidence"]]
        else old_meta["confidence"]
    )

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
        "imdb_matched": bool(
            fresh_meta["imdb_matched"] or old_meta["imdb_matched"]
        ),
        "douban_id": fresh_meta["douban_id"] or old_meta["douban_id"],
        "douban_url": fresh_meta["douban_url"] or old_meta["douban_url"],
        "imdb_id": fresh_meta["imdb_id"] or old_meta["imdb_id"],
        "tmdb_id": fresh_meta["tmdb_id"] or old_meta["tmdb_id"],
        "douban_poster": fresh_meta["douban_poster"] or old_meta["douban_poster"],
        "justwatch_poster": (
            fresh_meta["justwatch_poster"] or old_meta["justwatch_poster"]
        ),
        "imdb_poster": (
            fresh_meta["imdb_poster"] or old_meta["imdb_poster"]
        ),
        "localized_alias": (
            fresh_meta["localized_alias"] or old_meta["localized_alias"]
        ),
        "confidence": confidence,
        "evidence": unique_strings(
            fresh_meta["evidence"] + old_meta["evidence"]
        ),
    }


@functools.lru_cache(maxsize=1)
def verified_identities() -> dict[str, dict[str, Any]]:
    """Read reviewed identity links, not fuzzy rules or hand-entered posters.

    Each record links Netflix's exact ranked title to a specific provider work.
    Source URLs are retained in the manifest for human review. Runtime still
    verifies the returned ID, complete title/alias, media type, year and image.
    """
    data = load_json(VERIFIED_ALIASES_PATH)
    if data.get("version") != 1 or not isinstance(data.get("titles"), list):
        raise RuntimeError("Invalid verified-title alias manifest")
    result: dict[str, dict[str, Any]] = {}
    for item in data["titles"]:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid verified-title identity")
        title = clean(item.get("netflix_title"), 220)
        media_type = item.get("media_type")
        aliases = item.get("imdb_titles")
        sources = item.get("sources")
        if (
            not title or media_type not in {"movie", "tv"}
            or not re.fullmatch(r"tt[0-9]+", str(item.get("imdb_id", "")))
            or not re.fullmatch(r"(?:19|20)[0-9]{2}", str(item.get("year", "")))
            or not isinstance(aliases, list) or not aliases
            or not all(isinstance(alias, str) and alias.strip() for alias in aliases)
            or not isinstance(sources, list) or len(sources) < 2
            or not all(isinstance(source, str) and source.startswith("https://") for source in sources)
        ):
            raise RuntimeError(f"Incomplete verified identity for {title!r}")
        key = cache_key(media_type, title)
        if key in result:
            raise RuntimeError(f"Duplicate verified identity for {title!r}")
        result[key] = item
    return result


def identity_revision(title: str, media_type: str) -> str:
    identity = verified_identities().get(cache_key(media_type, title))
    if not identity:
        return ""
    return hashlib.sha256(
        json.dumps(
            {key: identity[key] for key in ("netflix_title", "media_type", "year", "imdb_id", "imdb_titles")},
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def resolve_verified_identity(
    title: str, media_type: str, identity: dict[str, Any]
) -> dict[str, Any]:
    meta = empty_meta()
    try:
        imdb = search_imdb(
            title, media_type, str(identity["year"]), identity["imdb_id"],
            verified_aliases=tuple(identity["imdb_titles"]),
        )
    except IdentityRejected as exc:
        log(f"REJECT verified IMDb identity {media_type} {title!r}: {exc}")
        meta["_identity_rejected"] = True
        return meta
    except Exception as exc:
        log(f"WARN verified IMDb identity {media_type} {title!r}: {exc}")
        return meta

    # An absent/truncated provider result is not evidence against cached data.
    if not imdb or not imdb.get("year"):
        return meta
    # Reviewed links need an exact year, not the generic ±1-year allowance.
    if imdb.get("year") != str(identity["year"]):
        log(f"REJECT verified IMDb identity {media_type} {title!r}: identity/year mismatch")
        meta["_identity_rejected"] = True
        return meta
    poster = normalize_image_url(imdb.get("poster"))
    meta.update({
        "year": imdb["year"],
        "imdb_id": imdb["imdb_id"],
        "imdb_matched": True,
        "imdb_poster": poster,
        "poster_candidates": [poster] if poster else [],
        "confidence": "medium",
        "evidence": ["imdb_verified_title_alias", "verified_netflix_identity", "exact_imdb_id_year"],
    })

    # Chinese titles remain Douban-only. A reviewed IMDb identity does not
    # authorize translating a title or copying one from a different provider.
    # Keep enrichment available, but require a complete verified title plus a
    # known compatible release year and explicit media type for this link.
    for query in unique_strings([title, imdb.get("matched_title")]):
        try:
            candidates = [
                candidate for candidate in search_douban(query)
                if isinstance(candidate, dict)
                and douban_type_score(candidate, media_type) > 0
                and parse_year(candidate.get("year") or candidate.get("card_subtitle"))
            ]
            selected = select_douban_candidate(
                candidates, title, media_type, query, str(identity["year"])
            )
            if not selected:
                continue
            db = build_douban_meta(selected[0], media_type, title)
            meta.update({
                "cn_title": db["cn_title"],
                "douban_id": db["douban_id"],
                "douban_url": db["douban_url"],
                "douban_matched": True,
                "douban_poster": db["poster"],
                "poster_candidates": unique_strings([db["poster"], poster]),
                "confidence": "high",
                "evidence": meta["evidence"] + ["douban_strict_title", "year_consensus"],
            })
            break
        except Exception as exc:
            log(f"WARN verified Douban alias {media_type} {title!r}: {exc}")
    return meta


def resolve_fresh_meta(
    title: str,
    media_type: str,
    year_hint: str = "",
    country_hints: set[str] | None = None,
) -> dict[str, Any]:
    identity = verified_identities().get(cache_key(media_type, title))
    if identity:
        # Never fall back to an unrelated exact-name work when a reviewed
        # identity exists. A temporary provider failure stays empty/cached.
        return resolve_verified_identity(title, media_type, identity)

    # Existing enriched years are not treated as authoritative during v2
    # revalidation. Provider-to-provider agreement is preferred instead.
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
            "",
            country_hints or {"US"},
        )
    except Exception as exc:
        log(f"WARN justwatch multi {media_type} {title!r}: {exc}")

    best_douban = select_douban_candidate(
        direct_candidates,
        title,
        media_type,
        title,
        (jw or {}).get("year") or "",
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
                (jw or {}).get("year") or "",
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

    imdb: dict[str, Any] | None = None
    imdb_lookup_failed = False

    try:
        imdb = search_imdb(
            title,
            media_type,
            (jw or {}).get("year") or "",
            clean((jw or {}).get("imdb_id"), 40),
        )
    except Exception as exc:
        imdb_lookup_failed = True
        log(f"WARN imdb {media_type} {title!r}: {exc}")

    # If JustWatch supplied an IMDb ID but IMDb can positively state that the
    # linked title is not equivalent to the Netflix title, reject JustWatch.
    if (
        jw
        and clean(jw.get("imdb_id"), 40)
        and not imdb
        and not imdb_lookup_failed
    ):
        log(
            f"REJECT justwatch {media_type} {title!r}: "
            "IMDb ID failed strict title/year validation"
        )
        jw = None

    # Two strict-title providers disagreeing by >1 year means the title is
    # ambiguous (often a remake or same-name work). Prefer no external poster.
    if jw and imdb and not years_compatible(jw.get("year"), imdb.get("year")):
        log(
            f"REJECT external ambiguity {media_type} {title!r}: "
            f"JustWatch year={jw.get('year')} IMDb year={imdb.get('year')}"
        )
        jw = None
        imdb = None

    if db and jw and not years_compatible(db.get("year"), jw.get("year")):
        log(
            f"REJECT justwatch year conflict {media_type} {title!r}: "
            f"Douban year={db.get('year')} JustWatch year={jw.get('year')}"
        )
        jw = None

    if db and imdb and not years_compatible(db.get("year"), imdb.get("year")):
        log(
            f"REJECT imdb year conflict {media_type} {title!r}: "
            f"Douban year={db.get('year')} IMDb year={imdb.get('year')}"
        )
        imdb = None

    douban_poster = normalize_image_url((db or {}).get("poster"))
    justwatch_poster = normalize_image_url((jw or {}).get("poster"))
    imdb_poster = normalize_image_url((imdb or {}).get("poster"))

    evidence: list[str] = []

    if db:
        evidence.append("douban_strict_title")
    if jw:
        evidence.append("justwatch_strict_title")
    if imdb:
        evidence.append("imdb_strict_title")

    accepted_years = [
        parse_year(provider.get("year"))
        for provider in (db, jw, imdb)
        if provider and parse_year(provider.get("year"))
    ]

    if len(accepted_years) >= 2:
        compatible_pairs = all(
            abs(int(a) - int(b)) <= 1
            for index, a in enumerate(accepted_years)
            for b in accepted_years[index + 1:]
        )
        if compatible_pairs:
            evidence.append("year_consensus")

    provider_count = sum(bool(provider) for provider in (db, jw, imdb))
    confidence = "high" if provider_count >= 2 else ("medium" if provider_count == 1 else "none")

    return {
        "cn_title": clean((db or {}).get("cn_title"), 220),
        "year": clean((db or {}).get("year"), 8)
        or clean((jw or {}).get("year"), 8)
        or clean((imdb or {}).get("year"), 8),
        "poster_candidates": unique_strings(
            [douban_poster, justwatch_poster, imdb_poster]
        ),
        "douban_matched": bool((db or {}).get("matched")),
        "justwatch_matched": bool((jw or {}).get("matched")),
        "imdb_matched": bool((imdb or {}).get("matched")),
        "douban_id": clean((db or {}).get("douban_id"), 30),
        "douban_url": clean((db or {}).get("douban_url"), 1000),
        "imdb_id": clean((imdb or {}).get("imdb_id"), 40)
        or clean((jw or {}).get("imdb_id"), 40),
        "tmdb_id": clean((jw or {}).get("tmdb_id"), 40),
        "douban_poster": douban_poster,
        "justwatch_poster": justwatch_poster,
        "imdb_poster": imdb_poster,
        "localized_alias": clean((jw or {}).get("localized_alias"), 220),
        "confidence": confidence,
        "evidence": evidence,
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

    record_match_version = int(record.get("match_version") or 0)
    revision = identity_revision(title, media_type)
    identity_current = (record.get("identity_revision") or "") == revision
    existing = sanitize_meta(record.get("meta"))
    trusted_existing = (
        existing
        if record_match_version == MATCH_VERSION and identity_current
        else empty_meta()
    )

    saved_at = record.get("saved_at")

    try:
        saved_at_value = float(saved_at)
    except (TypeError, ValueError):
        saved_at_value = 0.0

    has_poster = bool(trusted_existing.get("poster_candidates"))
    has_cn_title = bool(trusted_existing.get("cn_title"))

    if has_poster and has_cn_title:
        ttl = META_TTL_SECONDS
    elif has_poster or has_cn_title:
        ttl = PARTIAL_TTL_SECONDS
    else:
        ttl = NEGATIVE_TTL_SECONDS

    age = now_ts() - saved_at_value if saved_at_value else float("inf")

    if (
        record_match_version == MATCH_VERSION
        and identity_current
        and not FORCE
        and saved_at_value
        and age < ttl
    ):
        return trusted_existing, True

    fresh = empty_meta()

    try:
        fresh = resolve_fresh_meta(
            title,
            media_type,
            "",
            country_hints,
        )
    except Exception as exc:
        log(f"WARN metadata resolve {media_type} {title!r}: {exc}")

    # A successful response contradicting a pinned identity is not a transient
    # outage. Do not resurrect the now-rejected cached poster on every retry.
    if fresh.get("_identity_rejected"):
        trusted_existing = empty_meta()

    # v1 matches are intentionally not merged into v2. This is the key cleanup
    # that removes previously cached fuzzy-title false positives.
    merged = (
        merge_meta(trusted_existing, fresh)
        if record_match_version == MATCH_VERSION and identity_current
        else sanitize_meta(fresh)
    )

    items[key] = {
        "saved_at": now_ts(),
        "title": title,
        "media_type": media_type,
        "match_version": MATCH_VERSION,
        "identity_revision": revision,
        "meta": merged if meta_useful(merged) else empty_meta(),
    }

    return items[key]["meta"], False



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
        row["metadata_match_version"] = MATCH_VERSION
        row["metadata_confidence"] = meta["confidence"]
        row["metadata_evidence"] = meta["evidence"]


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

