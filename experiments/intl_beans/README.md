# USDA vs FEWS — multi-commodity lag-scan

Tests whether **USDA NASS US-producer monthly prices** lead Haiti's
**FEWS retail prices** at any practical lag, across every commodity pair
that has a plausible USDA counterpart.

Originally this experiment compared a single USDA aggregate (dry edible
beans) against FEWS Black bean retail; the folder name `intl_beans` is
legacy from that version. The current pipeline expands to 8 USDA series
covering bean, grain, oil, sugar, and dairy staples and ~19 FEWS products.

## Files

| File | Purpose |
| --- | --- |
| `prep_intl_prices.py` | Fetch every USDA NASS series in `SERIES` (`BEANS`, `CORN`, `WHEAT`, `RICE`, `SORGHUM`, `SOYBEANS`, `MILK`) → long-form `intl_prices_usda.csv`. |
| `prep_fews_export.py` | Pull monthly market-average prices for every FEWS product from `fews_haiti.duckdb` → long-form `fews_haiti_prices.csv`. |
| `correlations.R` | Run lag-scan correlation for each (FEWS, USDA) pair in `PAIRS`. Outputs per-pair plots, summary heatmap, and `report.md` under `out/`. |
| `intl_beans_usda.csv` | Legacy beans-only USDA pull. Used as fallback by `correlations.R` if `intl_prices_usda.csv` is missing. |

## Run

```bash
# 1. Get a NASS key once at https://quickstats.nass.usda.gov/api
export NASS_API_KEY='your-key'

# 2. Fetch USDA + export FEWS
python3 experiments/intl_beans/prep_intl_prices.py
python3 experiments/intl_beans/prep_fews_export.py

# 3. Run the lag-scan
Rscript experiments/intl_beans/correlations.R
```

`correlations.R` does NOT need the NASS key — it just consumes the CSVs.

## What it measures

For each pair, four bases at lags `-6, -3, 0, 1, 3, 6, 9, 12, 15, 18`:

- **levels** — Pearson r on log prices. Trend-shared, hardest to
  interpret; useful as a sanity floor.
- **returns** — r on Δlog prices (monthly returns). Removes the shared
  trend.
- **levels_des** — log prices with month-of-year partialled out.
- **returns_des** — Δlog with month-of-year out. The cleanest test;
  this is the headline number in `report.md` and the heatmap.

Positive lag = USDA leads FEWS. A GO verdict is `|r| ≥ 0.30` at the best
lag on deseasonalized returns.

## Outputs (under `out/`)

- `cor_all_pairs.csv` — every (pair, basis, lag) cell.
- `best_lags.csv` — best |r| per (pair, basis).
- `plots/lag_scan_<pair>.png` — 4-panel lag-scan per pair.
- `plots/overlay_<pair>.png` — level overlay since 2019, dual axis.
- `plots/summary_heatmap.png` — deseasonalized-returns r heatmap across
  all pairs and lags.
- `report.md` — per-pair verdict + best-lag tables.

## Caveats

- USDA `BEANS, DRY EDIBLE, (EXCL CHICKPEAS)` is the aggregate dry-bean
  class. Black beans are ~10–15% of US production by volume, so this is
  a noisy proxy for Black-specific prices.
- Sugar is **not paired**: NASS only publishes annual marketing-year sugarcane
  data, not monthly. Need a different source (e.g. ICE sugar #11 futures)
  to wire it in.
- Vegetable oil pairs against `SOYBEANS` (US oils are mostly soybean
  oil) rather than a direct refined-oil series.
- All USDA series are US producer; FEWS is Haitian retail. Each pair
  is a global producer-price signal vs a country-level retail signal
  with import + markup in between.
- p-values ignore residual autocorrelation — treat as sanity signals,
  not statistical tests.
