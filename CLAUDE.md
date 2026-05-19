# Repo conventions

## Price data source: FEWS NET only

FEWS NET is the canonical price source for this project. Do **not** wire WB
RTFP (catalog 4494) prices into the dashboard, forecast pipeline, or any
downstream feature. The WB sync code in [FEWS_Price_data/sync_worldbank.py](FEWS_Price_data/sync_worldbank.py)
and `wb_rtfp_*` DB tables stay for ad-hoc cross-checks only.

Why: see [experiments/wb_vs_fews_compare/README.md](experiments/wb_vs_fews_compare/README.md).
WB has ~5× more missing observations than FEWS (37% vs. 8%), has dropped
pasta/rice/sugar updates since 2024-12, and is not an independent source for
the other 8 commodities (its modeled prices ingest FEWS as input, so the
correlations are mechanical, not validation).

## One-time / exploratory scripts → `experiments/`

Anything that is run once for analysis, sanity-checking, data exploration,
ad-hoc comparison, or a writeup — i.e. not part of the production sync /
dashboard pipeline — lives under `experiments/`, and each experiment gets its
own named subfolder:

```text
experiments/
  <descriptive-name>/
    README.md           # one-paragraph what/why and how to run
    <scripts>.py
    output/             # plots, CSVs, etc. produced by the script
```

Do not leave one-off scripts at the repo root or inside `FEWS_Price_data/`
next to the production code — those directories are for code that runs on the
sync schedule or serves the dashboard. Production code should never `import`
from `experiments/`.
