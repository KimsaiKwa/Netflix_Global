# Netflix Global Top 10

This repository provides a small, browser-friendly JSON mirror of Netflix's official weekly global Top 10 data.

## Output

`global.json`

The file contains:

- `week` — Netflix reporting week
- `films` — derived global Movies Top 10
- `tv` — derived global TV Top 10
- `official_categories` — the four original Netflix global weekly charts

Netflix publishes four official global charts:

1. Films (English)
2. Films (Non-English)
3. TV (English)
4. TV (Non-English)

The repository keeps those four source charts intact. For the simplified `films` and `tv` arrays, the two language charts for each medium are merged and sorted by Netflix's own `weekly_views` metric. `hours_viewed`, original source rank, and title are used only as deterministic tie-breakers.

The merged `films` and `tv` rankings are therefore **derived views of Netflix's official data**, not separately published Netflix charts.

## Update schedule

GitHub Actions runs every Tuesday at 16:30 UTC and can also be triggered manually.

Workflow:

`.github/workflows/netflix-global.yml`

Generator:

`update.py`

No API key or secret is required.

## Data source

Netflix Tudum Top 10 public weekly data:

`https://top10.netflix.com/data/all-weeks-global.tsv`

## Intended use

This repository exists to provide a small static JSON endpoint for a Douban userscript. The userscript can read `global.json` directly instead of downloading and parsing the much larger Netflix TSV in the browser.
