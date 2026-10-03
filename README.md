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

## Data sources

Netflix Tudum Top 10 public weekly datasets:

- `https://top10.netflix.com/data/all-weeks-global.tsv`
- `https://top10.netflix.com/data/all-weeks-countries.tsv`

The workflow downloads the TSV files with retry handling, validates the generated JSON, and commits the latest data back to the repository.

## Intended use

The Douban userscript can read small raw JSON files directly from this repository instead of downloading and parsing Netflix's much larger TSV datasets in the browser.
