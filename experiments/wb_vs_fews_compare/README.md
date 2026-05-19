# WB RTFP vs. FEWS NET price comparison (Haiti)

One-off comparison of the World Bank Real-Time Food Prices (catalog 4494,
Haiti) data against the FEWS NET monthly market price series already in the
DuckDB. Answers:

- Which commodities overlap between the two sources?
- Which markets overlap?
- How correlated are the two series, in levels and in monthly log-returns?

Reads from `FEWS_Price_data/database/fews_haiti.duckdb` (after running
`python FEWS_Price_data/sync_worldbank.py sync` and `sync_fews_db.py --sync`).
Writes plots and a summary CSV to `output/`.

Run:

```bash
python experiments/wb_vs_fews_compare/compare.py
python experiments/wb_vs_fews_compare/missing_and_lag.py
```

## Results (release 2026-04-27)

All correlations are computed in HTG, monthly, on the 9 markets that exist in
both sources.

### Commodity overlap

10 WB commodities, all mappable to a FEWS product. The pick is the
longest-running FEWS series whose unit matches the WB column hint (e.g. WB
`rice (1 Marmite)` → FEWS `Rice (Milled)` Local, 6_lb).

| WB commodity | FEWS mapping                          | Median r (levels) | Median r (Δlog) | Ratio WB/FEWS |
|--------------|---------------------------------------|-------------------|-----------------|---------------|
| wheat_fao    | Wheat Grain (Local, 6_lb)             | 1.00              | 0.98            | 1.00          |
| beans_fao    | Beans (Black) (Local, 6_lb)           | 1.00              | 0.96            | 1.00          |
| sorghum_fao  | Sorghum (Local, 6_lb)                 | 1.00              | 0.95            | 1.00          |
| maize_meal   | Maize Meal (Local, 6_lb)              | 1.00              | 0.92            | 1.00          |
| wheat_flour  | Wheat Flour (Import, 6_lb)            | 1.00              | 0.87            | 1.00          |
| sugar        | Refined sugar (Import, 6_lb)          | 0.99              | 0.75            | 1.01          |
| pasta        | Spaghetti (Gourmet) (Import, 350_g)   | 0.99              | 0.72            | 0.98          |
| oil          | Refined Vegetable Oil (Import, gal)   | 0.98              | 0.48            | 1.05          |
| rice         | Rice (Milled) (Local, 6_lb)           | 0.96              | 0.33            | **0.59**      |
| rice_fao     | Rice (4% Broken) (Import, 6_lb)       | 0.95              | 0.40            | **2.00**      |

- **8 of 10 commodities are essentially identical** to FEWS — ratio ≈ 1.0 and
  r ≈ 1.0 in levels. WB clearly ingests FEWS as an input to its model for
  those commodities, so they are not an independent second opinion.
- **Both rice variants are outliers.** Levels still track (r ≈ 0.95) but the
  scale is off by ~2× and monthly returns barely correlate (r ≈ 0.3–0.4).
  This is almost certainly a unit/product-grade mismatch in our mapping, not
  bad data — see "Caveats" below.
- **Oil and pasta** have lower return correlations (0.48 / 0.72) despite
  matching scales. Worth flagging if you intend to forecast short-horizon
  movements off WB for these commodities.

### Market overlap

9 of 13 WB markets map cleanly to FEWS by name:

```text
WB 'Cap-Haitien'    -> FEWS 'Cap Haitien'
WB 'Cayes'          -> FEWS 'Cayes'
WB 'Gonaives'       -> FEWS 'Gonaives'
WB 'Hinche'         -> FEWS 'Hinche'
WB 'Jacmel'         -> FEWS 'Jacmel'
WB 'Jeremie'        -> FEWS 'Jeremie'
WB 'Ouanaminthe'    -> FEWS 'Ouanaminthe'
WB 'Port-au-Prince' -> FEWS 'Port-au-Prince, Croix-de-Bossales'
WB 'Port-de-Paix'   -> FEWS 'Port-de-Paix'
```

- **WB-only:** Marche Previle, Marche de Jeremie, Marche de Leon, Market Average.
- **FEWS-only:** Fond-des-Negres.

### Missing observations

Counted per (market, commodity) over the union span of both sources, pooled
across the 9 overlapping markets.

| Commodity   | Expected market-months | FEWS missing | WB missing | FEWS % | WB %  |
|-------------|-----------------------:|-------------:|-----------:|------:|------:|
| wheat_flour | 2,286                  | 112          | 352        |   4.9 |  15.4 |
| beans_fao   | 2,286                  | 112          | 486        |   4.9 |  21.3 |
| wheat_fao   | 1,181                  | 53           | 407        |   4.5 |  34.5 |
| oil         | 2,286                  | 113          | 1,029      |   4.9 |  45.0 |
| maize_meal  | 2,273                  | 125          | 361        |   5.5 |  15.9 |
| sugar       | 2,221                  | 166          | 1,390      |   7.5 |  62.6 |
| pasta       | 1,956                  | 169          | 1,321      |   8.6 |  67.5 |
| sorghum_fao | 2,286                  | 233          | 713        |  10.2 |  31.2 |
| rice        | 2,262                  | 243          | 924        |  10.7 |  40.8 |
| rice_fao    | 2,281                  | 334          | 999        |  14.6 |  43.8 |
| **Total**   | **21,318**             | **1,660**    | **7,982**  | **7.8** | **37.4** |

- **WB has 4–5× more missing observations than FEWS** — the opposite of
  what you might expect from a "modeled" dataset. Two drivers:
    1. WB does not publish every commodity for every market — `rice_fao` and
       `wheat_fao` are only released for 6–7 of the 9 overlapping markets.
    2. WB `pasta`, `rice`, and `sugar` all stopped updating at **2024-12**
       (see release lag section below), so the recent 14 months are blanks
       for those commodities. This drives `pasta` (68% missing) and `sugar`
       (63%) to look much worse than they are.
- FEWS missing rates are dominated by sparse markets (e.g. Cayes pre-2014)
  and seasonal absence of certain commodities; the median is ~5%.
- Net: **for a complete recent panel, FEWS is materially more reliable**.
  WB is useful as a smoothed/imputed cross-check for the commodities where it
  publishes, not as a higher-coverage replacement.

### Release lag

Today = 2026-05-19.

| Source | Latest data point | Released / synced | Data lag vs today |
|--------|-------------------|-------------------|-------------------|
| WB RTFP | 2026-02-01 (Feb)  | 2026-04-27 (file version) | 107 days |
| FEWS NET | 2026-02-28 (Feb)  | 2026-04-30 (last sync) | 80 days |

- **Release cadence and data lag are essentially the same**: both publish
  with a ~2–3 month gap from latest data point to release date, and both
  currently have Feb 2026 as the latest observation.
- WB uses month-start dates (2026-02-01) and FEWS uses month-end
  (2026-02-28), so the 27-day difference in "days vs today" is a labelling
  artifact, not a real lag difference.
- **Commodity-level exception:** WB's `pasta`, `rice`, and `sugar` series
  last updated at **2024-12-01** — those feeds have effectively been
  dropped from the model. FEWS still ships all three through 2026-02. If
  you need recent prices for pasta/rice/sugar, FEWS is the only option.

See `output/release_lag.txt` for per-commodity latest-date details.

### Coverage

- Time range: WB 2007-01 → 2026-02 (FEWS starts 2005-01).
- WB `pasta`, `rice`, `sugar` truncate at 2024-12; others run through 2026-02.
- 15,018 long-form rows in `wb_rtfp_prices` (13 markets × 10 commodities).

## Outputs

In `output/`:

- `correlation_summary.csv` — one row per (wb_market, cm_name) with n_months,
  means, ratio, and Pearson r in levels and log-returns.
- `overlap_summary.txt` — text dump of the mapping + headline correlations.
- `overlap_matrix.png`, `overlap_matrix_returns.png` — heatmaps of r by
  (commodity × market) for levels and Δlog.
- `levels_<commodity>.png` (×10) — per-market panels with WB and FEWS prices
  overlaid.
- `returns_<commodity>.png` (×10) — same, but monthly Δlog returns.
- `missing_summary.csv` — per (commodity, market) coverage: expected months,
  observed counts, missing counts and % for both sources.
- `missing_by_commodity.png` — side-by-side bar chart of FEWS vs WB %
  missing, pooled across the 9 overlapping markets.
- `release_lag.txt` — release version date, latest data date, and data lag
  for both sources, plus per-commodity latest-date breakdown.

## Caveats

- **Re-check the rice mappings before using.** The 0.5× / 2× ratios suggest
  the FEWS product/unit pick is wrong, not a WB modelling error. Candidates
  to try next: WB `rice` → FEWS `Rice (TCS)` Local; WB `rice_fao` →
  FEWS `Rice (10/10)` Import. The mapping dict is at the top of
  `compare.py`.
- **WB is not an independent second source for 8/10 commodities.** Treat the
  high correlations as a consistency check, not validation. The interesting
  signal is in the disagreements (rice, oil at the return horizon).
- Single FEWS unit per WB commodity. Some FEWS products are reported in
  multiple units (gal, 6_lb, 350_g, …); the script picks one.
