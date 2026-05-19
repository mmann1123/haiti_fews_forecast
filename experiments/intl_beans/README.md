# USDA vs FEWS — multi-commodity lag-scan

Tests whether **USDA NASS US-producer monthly prices** lead Haiti's
**FEWS retail prices** at any practical lag, across every commodity pair
that has a plausible USDA counterpart.

Originally this experiment compared a single USDA aggregate (dry edible
beans) against FEWS Black bean retail; the folder name `intl_beans` is
legacy from that version. The current pipeline runs 15 (FEWS commodity,
USDA series) pairs across 7 USDA series.

## TL;DR — Findings

**No USDA series cleanly leads Haiti FEWS retail at any practical lag once
HTG depreciation is removed.** The earlier headline ("Maize Meal +0.32 at
+15 months") was mostly an FX artifact — both sides shared HTG-vs-USD trend
that survived the Δlog transform.

After putting FEWS on a USD basis (using FEWS's own
`common_currency_price`, which divides each HTG observation by the
prevailing FX rate at observation time):

| Pair | Best lag (months) | r | n | p |
| --- | ---: | ---: | ---: | ---: |
| Beans (Pinto) Import ↔ beans_dry_edible | +18 | **−0.41** | 52 | 0.003 |
| Maize Meal (Gradoro) Import ↔ corn | +15 | +0.30 | 54 | 0.028 |
| Rice (4% Broken) Import ↔ rice | 0 | +0.25 | 70 | 0.039 |
| Rice (Milled) Local ↔ rice | +9 | +0.23 | 70 | 0.055 |
| Beans (Black) Local ↔ beans_dry_edible | −3 | +0.23 | 70 | 0.056 |
| Wheat Grain Local ↔ wheat | +9 | +0.22 | 127 | 0.014 |
| Refined Veg Oil Import ↔ soybeans | −6 | +0.21 | 166 | 0.007 |
| Milk (Bongu) Import ↔ milk | +12 | +0.20 | 127 | 0.027 |

GO threshold is `|r| ≥ 0.30` at the best lag. Only one pair clears it
(Beans (Pinto), but with **negative** sign — i.e. high US bean prices 18
months earlier predict *low* Haiti Pinto retail — which has no obvious
economic mechanism). With 15 pairs × 10 lags = 150 tests, one |r|≈0.4
hit is roughly what you'd expect from chance.

### What the FX control changed

Headline comparison on deseasonalized Δlog at the best lag for each pair:

| Pair | r before (HTG) | r after (USD) | Interpretation |
| --- | ---: | ---: | --- |
| Maize Meal (Gradoro) ↔ corn | +0.32 @ +15 ✅ | +0.30 @ +15 | Lost GO; mostly survived |
| Milk (Bongu) ↔ milk | +0.30 @ +6, p=0.0006 *** | +0.20 @ +12, p=0.027 | Most of the signal was FX co-movement |
| Refined Veg Oil ↔ soybeans | +0.25 @ +6 | +0.21 @ **−6** | Best-lag sign flipped — the "USDA leads" story was FX |
| Beans (Lima) ↔ beans | +0.28 @ +6 | −0.24 @ +18 | Sign flipped, lag shifted |
| Beans (Pinto) ↔ beans | +0.24 @ +1 | **−0.41 @ +18** | Picked up the only formal GO, but negatively |

The pattern is consistent: positive correlations weakened or flipped sign,
because the apparent "USDA → FEWS" tracking was partly each side moving
with nominal-USD inflation while HTG depreciated. The FX-adjusted picture
is the right one to read.

### Operational implication

Don't wire any of these USDA series into the forecast model as a leading
indicator — the signal isn't reliably there. If a global-prices regressor
is still wanted, candidates to try next:

- **IMF / FAO international food price indices** (deflate to real USD).
- **CIF / FOB import-price series** from BRH or Haiti customs (cuts out
  the US-producer → US-export → Haiti-import supply-chain steps).
- **Direct world prices** for refined goods (refined sugar, refined
  veg oil) rather than US-producer feedstocks.

## What the pipeline does

For each (FEWS commodity, USDA series) pair, computes Pearson r at lags
`-6, -3, 0, 1, 3, 6, 9, 12, 15, 18` (positive lag = USDA leads FEWS),
under four bases:

- **levels** — r on log prices. Trend-shared; sanity floor.
- **returns** — r on Δlog prices (monthly returns). Removes shared trend.
- **levels_des** — log prices, month-of-year partialled out.
- **returns_des** — Δlog with seasonality out. **Headline test.**

**FX control**: FEWS retail prices are taken in USD via
`common_currency_price` (FEWS's own HTG ÷ prevailing-FX conversion), not
HTG. This strips out the depreciation component that otherwise produces
spurious shared trend with any nominal USD series.

## Files

| File | Purpose |
| --- | --- |
| `prep_intl_prices.py` | Fetch USDA NASS series in `SERIES` (BEANS, CORN, WHEAT, RICE, SORGHUM, SOYBEANS, MILK) → long-form `intl_prices_usda.csv`. |
| `prep_fews_export.py` | Pull monthly market-average prices for every FEWS product from `fews_haiti.duckdb` → long-form `fews_haiti_prices.csv` (HTG **and** USD columns). |
| `correlations.R` | Per-pair lag-scan using FEWS USD prices. Outputs per-pair plots, summary heatmap, and `report.md` under `out/`. |
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

## Outputs (under `out/`)

- `cor_all_pairs.csv` — every (pair, basis, lag) cell.
- `best_lags.csv` — best |r| per (pair, basis).
- `plots/lag_scan_<pair>.png` — 4-panel lag-scan per pair.
- `plots/overlay_<pair>.png` — USD-basis level overlay since 2019.
- `plots/summary_heatmap.png` — deseasonalized-returns r heatmap across
  all pairs and lags.
- `report.md` — per-pair verdict + best-lag tables.

## Caveats

- **Multiple-testing**: 15 pairs × 10 lags = 150 individual r tests. With
  n≈50 the chance threshold for any |r|≥0.30 is roughly p≈0.05 already,
  so an isolated |r|≈0.4 hit doesn't survive a Bonferroni-style correction.
- **USDA bean aggregate**: `BEANS, DRY EDIBLE, (EXCL CHICKPEAS)` mixes
  Black, Pinto, Navy, Kidney, etc. — Black is ~10–15% by volume. All
  FEWS bean variants share this same USDA series, so per-variant
  correlations aren't independent.
- **Sugar is not paired**: NASS only publishes annual marketing-year
  sugarcane data, not monthly. Need a different source (e.g. ICE sugar
  #11 futures) to wire it in.
- **Vegetable oil** pairs against `SOYBEANS` (US oils are mostly soybean
  oil) rather than a direct refined-oil series — adds two extra
  supply-chain steps (crush + refine) to the signal.
- **US-producer vs Haiti-retail**: every pair compares US farmgate prices
  to Haiti consumer prices, so it crosses export → ocean shipping →
  Haiti import → wholesale → retail markup. A short-horizon lead would
  be surprising.
- **US CPI**: FEWS USD prices are real-terms in HTG-purchasing-power but
  still nominal in USD. USDA producer prices are also nominal USD. US
  CPI moves over the window are small enough not to materially affect
  monthly-returns r, but for a full real-terms test both sides should
  be deflated by US CPI.
- **p-values ignore residual autocorrelation** — treat as sanity signals,
  not formal tests.
