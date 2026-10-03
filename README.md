# Netflix Top 10 Mirror

This repository provides small, browser-friendly JSON mirrors of Netflix Tudum's official weekly Top 10 data for a Douban userscript.

## Outputs

### Global

`global.json`

Netflix publishes four official global charts:

1. Films (English)
2. Films (Non-English)
3. TV (English)
4. TV (Non-English)

The repository preserves all four official charts in `official_categories`.

It also exposes simplified:

- `films` — derived global Movies Top 10
- `tv` — derived global TV Top 10

The two language charts within each medium are merged and sorted by Netflix's own `weekly_views` metric. `hours_viewed`, original source rank, and title are deterministic tie-breakers.

The simplified global lists are therefore **derived views of Netflix's official data**, not separately published Netflix charts.

### Countries

`countries/<iso2>.json`

The repository currently mirrors the 23 country files needed by the userscript:

- United States: `countries/us.json`
- South Korea: `countries/kr.json`
- Japan: `countries/jp.json`
- Europe aggregate inputs:
  - GB, FR, DE, ES, IT
  - NL, PL, SE, NO, DK, FI
  - BE, AT, CH, PT, IE
  - CZ, GR, HU, RO

Each country JSON contains:

- `week`
- `country_iso2`
- `country_name`
- `films` — official country Films Top 10
- `tv` — official country TV Top 10
- each item includes rank, title, cumulative weeks in Top 10, and season where applicable

The Europe Top 10 shown in the userscript is **not an official Netflix Europe chart**. It is derived in the userscript from the 20 European country files.

## Update schedule

GitHub Actions runs every Tuesday at 16:30 UTC and can also be triggered manually.

Workflow:

`.github/workflows/netflix-global.yml`

Generators:

- `update.py` — global data
- `update_countries.py` — US / KR / JP + 20 European markets

No API key or secret is required.

## Douban userscript

Canonical userscript:

- `douban-hot.user.js`

Version 1.6.1 reads browser-ready metadata only from this repository. For Netflix markets it does not call Douban `subject_suggest`, JustWatch, or IMDb from the browser, and it no longer reads the legacy local metadata cache. The browser is now a pure display client for GitHub-enriched Netflix JSON.

The small “详情” control also suppresses the browser's default blue focus outline.

## Backend display metadata

The mirror JSON is enriched in GitHub Actions before publication so the Douban userscript does not need to query Douban or JustWatch at page-load time.

Generator:

- `enrich_metadata.py`

Persistent cache:

- `metadata_cache.json`

Browser-facing ranking rows include:

- `cn_title` — only a Chinese title confirmed through Douban; otherwise an empty string
- `year`
- `poster_candidates` — ordered fallback list, normally Douban first, JustWatch second, and IMDb as a final poster fallback
- `douban_id`
- `douban_url`
- `imdb_id`
- `tmdb_id`

Ranking order and Netflix metrics are never changed by metadata enrichment.

The weekly update pipeline is now:

1. download official Netflix TSV files
2. generate global and country ranking JSON
3. validate ranking data and reporting-week consistency
4. enrich display metadata from the persistent cache / Douban / JustWatch / IMDb
5. validate the final browser-ready mirror
6. publish all files in one Git commit

Successful metadata is cached for 30 days. Empty lookups are retried after 6 hours. Existing successful metadata is preserved when an upstream metadata request fails.

A separate daily workflow retries incomplete metadata without regenerating Netflix rankings:

`.github/workflows/netflix-metadata.yml`

It runs daily at 19:15 UTC. If the final mirror is not healthy enough to publish, only cache progress is preserved; the committed browser-facing JSON remains unchanged.

## Independent health check

A separate health workflow validates the committed mirror independently from the weekly updater.

Files:

- `health_check.py`
- `.github/workflows/netflix-health.yml`

The health check runs every day at 18:45 UTC, can be triggered manually, and also runs when health-check or mirror JSON files are changed outside a skipped-CI update commit.

It verifies:

- `global.json` exists and is valid JSON
- the derived global Films and TV lists each contain ranks 1–10
- all four official global categories contain 10 rows
- all 23 required country JSON files exist
- every country has Films Top 10 and TV Top 10 with ranks 1–10
- all country files use one reporting week
- the country reporting week matches `global.json`
- all 20 European aggregate inputs are available
- the reporting week and `generated_at` timestamp are not stale

Default freshness limits:

- reporting week: no more than 10 days old
- `global.json generated_at`: no more than 192 hours old

The limits can be overridden with `MAX_WEEK_AGE_DAYS` and `MAX_GENERATED_AGE_HOURS`.

If the check fails, the workflow fails and automatically opens or refreshes one repository issue titled:

`[Health Check] Netflix Top 10 mirror unhealthy`

When the mirror becomes healthy again, the workflow closes that issue automatically.

## Official TSV schema monitor

A second independent workflow watches Netflix's official TSV structure itself, before the mirror generator depends on it.

Files:

- `schema_baseline.json` — the accepted Netflix TSV schema
- `schema_check.py` — structural/semantic schema validator
- `.github/workflows/netflix-schema.yml` — scheduled schema monitor

Current accepted schemas:

- `all-weeks-global.tsv`: 9 columns
  - `week`
  - `category`
  - `weekly_rank`
  - `show_title`
  - `season_title`
  - `weekly_hours_viewed`
  - `runtime`
  - `weekly_views`
  - `cumulative_weeks_in_top_10`
- `all-weeks-countries.tsv`: 8 columns
  - `country_name`
  - `country_iso2`
  - `week`
  - `category`
  - `weekly_rank`
  - `show_title`
  - `season_title`
  - `cumulative_weeks_in_top_10`

The monitor checks:

- exact column names
- exact column order
- added or removed columns
- malformed tab-separated rows
- global category values:
  - Films (English)
  - Films (Non-English)
  - TV (English)
  - TV (Non-English)
- country category values:
  - Films
  - TV
- required non-empty fields
- date, rank, country-code and numeric field formats used by the generators

Schedule:

- Monday 15:15 UTC — preflight check
- Tuesday 15:15 UTC — final schema check before the 16:30 UTC mirror update
- manual `workflow_dispatch` is also available

If the schema check fails, the workflow automatically opens or refreshes:

`[Schema Check] Netflix Tudum TSV schema changed`

When the schema becomes healthy again, the issue is closed automatically.

A schema alert must be reviewed before editing `schema_baseline.json`. The baseline should not be updated automatically, because an added or renamed field may require changes to `update.py` or `update_countries.py`.

## Data sources

Netflix Tudum Top 10 public weekly datasets:

- `https://top10.netflix.com/data/all-weeks-global.tsv`
- `https://top10.netflix.com/data/all-weeks-countries.tsv`

The weekly update workflow downloads the TSV files with retry handling, validates the generated JSON, and commits the latest data back to the repository.

## Intended use

The Douban userscript can read small raw JSON files directly from this repository instead of downloading and parsing Netflix's much larger TSV datasets in the browser.
