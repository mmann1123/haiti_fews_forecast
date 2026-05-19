#!/usr/bin/env python3
"""
Compare World Bank RTFP (catalog 4494) vs FEWS NET prices for Haiti.

Both feeds live in fews_haiti.duckdb. We map commodities and markets,
align both to month-end, and compare in levels and log-returns.

Outputs (next to this script, under ./output/):
    correlation_summary.csv          per (market, commodity) Pearson r
    overlap_summary.txt              text dump of coverage + correlation tables
    overlap_matrix.png               heatmap: market x commodity, level correlation
    overlap_matrix_returns.png       heatmap: market x commodity, log-return correlation
    levels_<commodity>.png           one panel per WB commodity, all overlapping markets
    returns_<commodity>.png          same, but month-over-month log returns
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
DB_PATH = REPO / "FEWS_Price_data" / "database" / "fews_haiti.duckdb"
OUT = Path(__file__).resolve().parent / "output"
OUT.mkdir(exist_ok=True)

# WB commodity slug -> (FEWS product name, FEWS product_source, FEWS unit).
# Picks the longest-running FEWS series whose unit matches the WB column header
# (see `components` text in the WB CSV — e.g. "rice (1 Marmite)").
COMMODITY_MAP = {
    "beans_fao":    ("Beans (Black)",          "Local",  "6_lb"),
    "maize_meal":   ("Maize Meal",             "Local",  "6_lb"),
    "oil":          ("Refined Vegetable Oil",  "Import", "gal"),
    "pasta":        ("Spaghetti (Gourmet)",    "Import", "350_g"),
    "rice":         ("Rice (Milled)",          "Local",  "6_lb"),
    "rice_fao":     ("Rice (4% Broken)",       "Import", "6_lb"),
    "sorghum_fao":  ("Sorghum",                "Local",  "6_lb"),
    "sugar":        ("Refined sugar",          "Import", "6_lb"),
    "wheat_fao":    ("Wheat Grain",            "Local",  "6_lb"),
    "wheat_flour":  ("Wheat Flour",            "Import", "6_lb"),
}

# WB market name -> FEWS market name. Only the 9 markets that overlap.
MARKET_MAP = {
    "Cap-Haitien":    "Cap Haitien",
    "Cayes":          "Cayes",
    "Gonaives":       "Gonaives",
    "Hinche":         "Hinche",
    "Jacmel":         "Jacmel",
    "Jeremie":        "Jeremie",
    "Ouanaminthe":    "Ouanaminthe",
    "Port-au-Prince": "Port-au-Prince, Croix-de-Bossales",
    "Port-de-Paix":   "Port-de-Paix",
}


def load_fews(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute(
        """
        SELECT m.name AS fews_market,
               p.name AS product, p.product_source,
               u.name AS unit,
               po.period_date,
               po.value AS price_htg
        FROM price_observations po
        JOIN markets  m ON po.market_id  = m.id
        JOIN products p ON po.product_id = p.id
        JOIN units    u ON po.unit_id    = u.id
        WHERE po.currency = 'HTG'
        """
    ).df()
    df["period_date"] = pd.to_datetime(df["period_date"])
    return df


def load_wb(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute(
        """
        SELECT mkt_name AS wb_market,
               cm_name,
               price_date,
               price
        FROM wb_rtfp_prices
        WHERE currency = 'HTG'
        """
    ).df()
    df["price_date"] = pd.to_datetime(df["price_date"])
    return df


def build_pairs(fews: pd.DataFrame, wb: pd.DataFrame) -> pd.DataFrame:
    """Return long DF with [wb_market, fews_market, cm_name, month, wb_price, fews_price]."""
    rows = []
    for cm, (prod, src, unit) in COMMODITY_MAP.items():
        f = fews[
            (fews["product"] == prod)
            & (fews["product_source"] == src)
            & (fews["unit"] == unit)
        ].copy()
        if f.empty:
            print(f"[WARN] FEWS empty for {prod} ({src}, {unit}) -> WB {cm}")
            continue
        # FEWS dates are month-end; normalize to month-start so we can join WB.
        f["month"] = f["period_date"].dt.to_period("M").dt.to_timestamp()

        w = wb[wb["cm_name"] == cm].copy()
        w["month"] = w["price_date"].dt.to_period("M").dt.to_timestamp()

        for wb_mkt, fews_mkt in MARKET_MAP.items():
            ff = f[f["fews_market"] == fews_mkt][["month", "price_htg"]]
            ww = w[w["wb_market"] == wb_mkt][["month", "price"]]
            if ff.empty or ww.empty:
                continue
            merged = ff.merge(ww, on="month", how="inner")
            if merged.empty:
                continue
            merged["wb_market"] = wb_mkt
            merged["fews_market"] = fews_mkt
            merged["cm_name"] = cm
            merged = merged.rename(columns={"price_htg": "fews_price", "price": "wb_price"})
            rows.append(merged)

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def correlation_table(pairs: pd.DataFrame) -> pd.DataFrame:
    """Per (market, commodity) Pearson r on levels and log-returns."""
    out = []
    for (mkt, cm), sub in pairs.groupby(["wb_market", "cm_name"]):
        # FEWS has gaps (po.value NULL); drop those before any stats so the
        # levels correlation doesn't get poisoned by NaNs.
        sub = (
            sub.sort_values("month")
            .dropna(subset=["wb_price", "fews_price"])
            .reset_index(drop=True)
        )
        if len(sub) < 12:
            continue

        x = sub["wb_price"].astype(float).values
        y = sub["fews_price"].astype(float).values
        r_lvl = np.corrcoef(x, y)[0, 1] if np.std(x) and np.std(y) else np.nan

        # Log returns: drop non-positive and NaN diffs.
        lx = np.log(np.where(x > 0, x, np.nan))
        ly = np.log(np.where(y > 0, y, np.nan))
        dx = np.diff(lx)
        dy = np.diff(ly)
        mask = np.isfinite(dx) & np.isfinite(dy)
        if mask.sum() >= 12 and np.std(dx[mask]) and np.std(dy[mask]):
            r_ret = np.corrcoef(dx[mask], dy[mask])[0, 1]
        else:
            r_ret = np.nan

        y_mean = float(np.mean(y))
        x_mean = float(np.mean(x))
        out.append(
            {
                "wb_market": mkt,
                "cm_name": cm,
                "n_months": len(sub),
                "fews_mean": y_mean,
                "wb_mean": x_mean,
                "ratio_wb_over_fews": x_mean / y_mean if y_mean else np.nan,
                "r_levels": r_lvl,
                "r_logreturns": r_ret,
            }
        )
    return pd.DataFrame(out).sort_values(["cm_name", "wb_market"])


def plot_heatmap(corr_df: pd.DataFrame, value_col: str, title: str, path: Path) -> None:
    pivot = corr_df.pivot(index="cm_name", columns="wb_market", values=value_col)
    fig, ax = plt.subplots(figsize=(max(7, 0.7 * len(pivot.columns) + 3), 0.5 * len(pivot.index) + 3))
    im = ax.imshow(pivot.values, vmin=-1, vmax=1, cmap="RdBu_r", aspect="auto")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns, rotation=45, ha="right")
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)
    for i in range(pivot.shape[0]):
        for j in range(pivot.shape[1]):
            v = pivot.values[i, j]
            if np.isfinite(v):
                ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                        color="white" if abs(v) > 0.5 else "black", fontsize=8)
    fig.colorbar(im, ax=ax, shrink=0.7, label="Pearson r")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_commodity(pairs: pd.DataFrame, cm: str, kind: str, path: Path) -> None:
    """kind = 'levels' or 'returns'."""
    sub_all = pairs[pairs["cm_name"] == cm].copy()
    if sub_all.empty:
        return
    mkts = sorted(sub_all["wb_market"].unique())
    ncols = 3
    nrows = int(np.ceil(len(mkts) / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.5 * ncols, 2.6 * nrows), sharex=False)
    axes = np.array(axes).reshape(-1)
    for ax in axes:
        ax.set_visible(False)

    for ax, mkt in zip(axes, mkts):
        ax.set_visible(True)
        s = sub_all[sub_all["wb_market"] == mkt].sort_values("month")
        if kind == "levels":
            ax.plot(s["month"], s["fews_price"], label="FEWS", lw=1.5, color="#1f77b4")
            ax.plot(s["month"], s["wb_price"], label="WB",  lw=1.5, color="#d62728", alpha=0.85)
            ax.set_ylabel("HTG")
        else:
            s = s.assign(
                fews_ret=np.log(s["fews_price"]).diff(),
                wb_ret=np.log(s["wb_price"]).diff(),
            )
            ax.plot(s["month"], s["fews_ret"], label="FEWS", lw=1.0, color="#1f77b4")
            ax.plot(s["month"], s["wb_ret"], label="WB",  lw=1.0, color="#d62728", alpha=0.85)
            ax.axhline(0, color="k", lw=0.5)
            ax.set_ylabel("Δ log price")
        ax.set_title(f"{mkt} ({len(s)} months)", fontsize=10)
        ax.tick_params(axis="x", labelsize=8)
        ax.tick_params(axis="y", labelsize=8)

    # Single legend on first visible axis.
    axes[0].legend(loc="best", fontsize=9)
    prod, src, unit = COMMODITY_MAP[cm]
    fig.suptitle(
        f"WB {cm}  vs  FEWS {prod} ({src}, {unit}) — {kind}",
        fontsize=12,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main():
    if not DB_PATH.exists():
        print(f"[ERROR] DB not found: {DB_PATH}")
        sys.exit(1)

    con = duckdb.connect(str(DB_PATH), read_only=True)
    fews = load_fews(con)
    wb = load_wb(con)
    con.close()

    print(f"FEWS rows: {len(fews):,}  WB rows: {len(wb):,}")
    print(
        f"FEWS markets: {fews['fews_market'].nunique()}  "
        f"WB markets: {wb['wb_market'].nunique()}"
    )

    # Coverage summary written to disk
    coverage_lines = []
    coverage_lines.append("== Commodity mapping ==")
    for cm, (prod, src, unit) in COMMODITY_MAP.items():
        f_match = fews[
            (fews["product"] == prod)
            & (fews["product_source"] == src)
            & (fews["unit"] == unit)
        ]
        w_match = wb[wb["cm_name"] == cm]
        coverage_lines.append(
            f"  WB {cm:12s} -> FEWS {prod} ({src}, {unit}): "
            f"FEWS n={len(f_match):5d}  WB n={len(w_match):5d}"
        )

    coverage_lines.append("")
    coverage_lines.append("== Market mapping (9 of 13 WB markets in FEWS) ==")
    for wb_mkt, fews_mkt in MARKET_MAP.items():
        coverage_lines.append(f"  WB {wb_mkt!r:24s} -> FEWS {fews_mkt!r}")
    coverage_lines.append("")
    wb_only = sorted(set(wb["wb_market"]) - set(MARKET_MAP.keys()))
    fews_only = sorted(set(fews["fews_market"]) - set(MARKET_MAP.values()))
    coverage_lines.append(f"WB-only markets:   {wb_only}")
    coverage_lines.append(f"FEWS-only markets: {fews_only}")
    coverage_lines.append("")

    pairs = build_pairs(fews, wb)
    print(f"Joined month-overlap pairs: {len(pairs):,}")
    corr = correlation_table(pairs)
    corr.to_csv(OUT / "correlation_summary.csv", index=False)

    # Print headline stats
    coverage_lines.append("== Median correlation by commodity (across overlapping markets) ==")
    headline = (
        corr.groupby("cm_name")
        .agg(
            n_pairs=("wb_market", "size"),
            median_r_levels=("r_levels", "median"),
            median_r_returns=("r_logreturns", "median"),
            median_n_months=("n_months", "median"),
            mean_ratio=("ratio_wb_over_fews", "mean"),
        )
        .sort_values("median_r_levels", ascending=False)
    )
    coverage_lines.append(headline.to_string())
    coverage_lines.append("")
    coverage_lines.append("== Per-pair correlation (n_months >= 24) ==")
    coverage_lines.append(corr[corr["n_months"] >= 24].to_string(index=False))

    (OUT / "overlap_summary.txt").write_text("\n".join(coverage_lines))
    print()
    print("\n".join(coverage_lines[-25:]))

    # Heatmaps
    plot_heatmap(
        corr, "r_levels",
        "WB vs FEWS — Pearson r (levels, HTG)",
        OUT / "overlap_matrix.png",
    )
    plot_heatmap(
        corr, "r_logreturns",
        "WB vs FEWS — Pearson r (log returns, monthly)",
        OUT / "overlap_matrix_returns.png",
    )

    # Per-commodity time-series panels
    for cm in COMMODITY_MAP:
        plot_commodity(pairs, cm, "levels", OUT / f"levels_{cm}.png")
        plot_commodity(pairs, cm, "returns", OUT / f"returns_{cm}.png")

    print(f"\nWrote {len(list(OUT.iterdir()))} files to {OUT}")


if __name__ == "__main__":
    main()
