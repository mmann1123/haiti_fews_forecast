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

```
python experiments/wb_vs_fews_compare/compare.py
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

```
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
