#!/usr/bin/env Rscript
# =============================================================================
# experiments/intl_beans/correlations.R
#
# Multi-pair lag-scan: USDA NASS US producer prices vs Haiti FEWS retail
# prices, for every FEWS product that has a plausible USDA counterpart.
#
# For each pair the script computes Pearson r between log-levels and Δlog
# (monthly returns), both raw and after partialling out month-of-year, at
# lags spanning -6 to +18 months. Positive lag = past USDA predicts current
# FEWS (the direction we'd actually use the signal in a forecast).
#
# Pipeline:
#   1. Run prep_intl_prices.py    -> intl_prices_usda.csv  (long form)
#   2. Run prep_fews_export.py    -> fews_haiti_prices.csv (long form)
#   3. Rscript correlations.R     -> out/...
#
# Outputs under experiments/intl_beans/out/:
#   cor_all_pairs.csv         -- one row per (fews_commodity, usda_slug, basis, lag)
#   best_lags.csv             -- one row per pair, best |r| across lags for each basis
#   plots/lag_scan_<pair>.png -- 2x2 lag-scan grid per pair (one panel per basis)
#   plots/overlay_<pair>.png  -- level overlay since 2019
#   plots/summary_heatmap.png -- |r| at best lag, heatmap of pairs
#   report.md                 -- writeup with per-pair verdict
# =============================================================================

suppressPackageStartupMessages({
  library(dplyr)
  library(tidyr)
  library(ggplot2)
})

# Working directory: repo root is the canonical place. Fall back to the
# script's own directory so direct `Rscript correlations.R` from within
# experiments/intl_beans/ still works.
script_dir <- (function() {
  args <- commandArgs(trailingOnly = FALSE)
  m <- regmatches(args, regexpr("(?<=--file=).*", args, perl = TRUE))
  if (length(m) > 0) normalizePath(dirname(m[1])) else normalizePath(".")
})()
if (file.exists("experiments/intl_beans/intl_prices_usda.csv")) {
  EXP_DIR <- "experiments/intl_beans"
} else {
  EXP_DIR <- script_dir
}
OUT_DIR  <- file.path(EXP_DIR, "out")
PLOT_DIR <- file.path(OUT_DIR, "plots")
dir.create(PLOT_DIR, recursive = TRUE, showWarnings = FALSE)

USDA_CSV <- file.path(EXP_DIR, "intl_prices_usda.csv")
FEWS_CSV <- file.path(EXP_DIR, "fews_haiti_prices.csv")
LEGACY_BEANS_CSV <- file.path(EXP_DIR, "intl_beans_usda.csv")

LAGS <- c(-6, -3, 0, 1, 3, 6, 9, 12, 15, 18)
GO_THRESHOLD <- 0.30  # |r| above which we'd consider wiring as a v09 regressor

# -----------------------------------------------------------------------------
# 1. FEWS <-> USDA mapping
# -----------------------------------------------------------------------------
# Each row maps one FEWS (commodity, source) series to a USDA slug from
# prep_intl_prices.py. Multi-variant FEWS items (e.g. several brands of
# Rice) all join against the same USDA aggregate.

PAIRS <- read.table(
  text = "
fews_commodity|product_source|usda_slug
Beans (Black)|Local|beans_dry_edible
Beans (Red)|Local|beans_dry_edible
Beans (Lima)|Local|beans_dry_edible
Beans (Pinto)|Import|beans_dry_edible
Maize Grain (Yellow)|Local|corn
Maize Meal|Local|corn
Maize Meal (Gradoro)|Import|corn
Sorghum|Local|sorghum
Rice (Milled)|Local|rice
Rice (4% Broken)|Import|rice
Rice (10/10)|Import|rice
Wheat Grain|Local|wheat
Wheat Flour|Import|wheat
Spaghetti (Gourmet)|Import|wheat
Refined Vegetable Oil|Import|soybeans
Milk (Vita)|Import|milk
Milk (Bongu)|Import|milk
",
  header = TRUE, sep = "|", stringsAsFactors = FALSE, strip.white = TRUE
)

# -----------------------------------------------------------------------------
# 2. Load inputs
# -----------------------------------------------------------------------------

if (!file.exists(FEWS_CSV)) {
  stop(sprintf("FEWS CSV missing: %s\nRun: python3 %s/prep_fews_export.py",
               FEWS_CSV, EXP_DIR))
}
fews <- read.csv(FEWS_CSV, stringsAsFactors = FALSE,
                 na.strings = c("NA", "NaN", "nan", ""))
fews$month <- as.Date(fews$month)

if (!file.exists(USDA_CSV)) {
  # Fall back to the legacy beans-only file so the script remains useful
  # before someone re-runs prep_intl_prices.py.
  if (file.exists(LEGACY_BEANS_CSV)) {
    message(sprintf("Multi-commodity USDA CSV missing; falling back to %s (beans only)",
                    LEGACY_BEANS_CSV))
    legacy <- read.csv(LEGACY_BEANS_CSV, stringsAsFactors = FALSE)
    legacy$month <- as.Date(legacy$month)
    usda <- data.frame(
      month        = legacy$month,
      usda_slug    = "beans_dry_edible",
      price_usd_kg = legacy$price_usd_kg,
      stringsAsFactors = FALSE
    )
  } else {
    stop(sprintf("USDA CSV missing: %s\nRun: NASS_API_KEY=... python3 %s/prep_intl_prices.py",
                 USDA_CSV, EXP_DIR))
  }
} else {
  usda <- read.csv(USDA_CSV, stringsAsFactors = FALSE)
  usda$month <- as.Date(usda$month)
  usda <- usda[, c("month", "usda_slug", "price_usd_kg")]
}

# Keep only pairs we actually have USDA data for.
PAIRS <- PAIRS[PAIRS$usda_slug %in% unique(usda$usda_slug), , drop = FALSE]
cat(sprintf("Running %d FEWS<->USDA pairs across %d USDA series\n",
            nrow(PAIRS), length(unique(PAIRS$usda_slug))))

# -----------------------------------------------------------------------------
# 3. Helpers
# -----------------------------------------------------------------------------

p_from_r <- function(r, n) {
  if (is.na(r) || n < 4 || abs(r) >= 1) return(NA_real_)
  t <- r * sqrt((n - 2) / (1 - r^2))
  2 * pt(-abs(t), df = n - 2)
}

resid_of <- function(y, moy) {
  ok <- !is.na(y)
  if (sum(ok) < 12) return(rep(NA_real_, length(y)))
  fit <- lm(y[ok] ~ moy[ok])
  r <- rep(NA_real_, length(y))
  r[ok] <- residuals(fit)
  r
}

# Returns a data.frame with one row per lag in `lags` for the given y/x pair.
cor_at_lags <- function(y, x, lags = LAGS) {
  out <- do.call(rbind, lapply(lags, function(L) {
    x_lag <- if (L >= 0) dplyr::lag(x, L) else dplyr::lead(x, -L)
    ok <- complete.cases(x_lag, y)
    n  <- sum(ok)
    r  <- if (n > 3) suppressWarnings(cor(x_lag[ok], y[ok])) else NA_real_
    data.frame(lag = L, r = r, n = n, p = p_from_r(r, n))
  }))
  out
}

# Slugify a pair name for filenames.
pair_slug <- function(fews_commodity, product_source, usda_slug) {
  s <- paste(fews_commodity, product_source, "vs", usda_slug, sep = "_")
  s <- gsub("[^A-Za-z0-9]+", "_", s)
  s <- gsub("_+", "_", s)
  s <- gsub("^_|_$", "", s)
  s
}

# -----------------------------------------------------------------------------
# 4. Per-pair lag scan
# -----------------------------------------------------------------------------

run_one_pair <- function(fews_commodity, product_source, usda_slug) {
  pair_label <- sprintf("%s (%s) vs USDA %s",
                       fews_commodity, product_source, usda_slug)
  message("- ", pair_label)

  fs <- fews[fews$fews_commodity == fews_commodity
             & fews$product_source == product_source, ]
  us <- usda[usda$usda_slug == usda_slug, ]

  if (nrow(fs) == 0 || nrow(us) == 0) {
    message("    (no data on one side; skipping)")
    return(NULL)
  }

  # Both sides on a common monthly grid. FEWS month is month-end (date_trunc
  # back to first-of-month is done in the SQL), USDA is first-of-month already.
  fs$month_key <- as.Date(format(fs$month, "%Y-%m-01"))
  us$month_key <- as.Date(format(us$month, "%Y-%m-01"))

  df <- merge(
    fs[, c("month_key", "price_htg", "price_usd")],
    us[, c("month_key", "price_usd_kg")],
    by = "month_key", all = TRUE
  )
  df <- df[order(df$month_key), ]
  colnames(df)[1] <- "month"

  # IMPORTANT: use FEWS's USD-converted price (`price_usd`), NOT the raw HTG
  # price. Haiti has had heavy HTG inflation/depreciation over the window,
  # which produces a spurious shared trend with any nominal USD series.
  # FEWS reports `common_currency_price` as the same HTG observation divided
  # by the prevailing FX rate at the time of measurement -- this strips out
  # the currency-depreciation component and leaves a real-terms commodity
  # price comparable to USDA's nominal USD producer price. (US CPI moves
  # over the window are small enough that the residual nominal-USD drift
  # on the USDA side doesn't materially affect monthly-returns r.)
  df$log_fews    <- suppressWarnings(log(df$price_usd))
  df$log_usda    <- suppressWarnings(log(df$price_usd_kg))
  df$dlog_fews   <- c(NA, diff(df$log_fews))
  df$dlog_usda   <- c(NA, diff(df$log_usda))
  df$moy         <- factor(format(df$month, "%m"))

  overlap <- df[!is.na(df$price_usd) & !is.na(df$price_usd_kg), ]
  if (nrow(overlap) < 12) {
    message("    (overlap < 12 months; skipping)")
    return(NULL)
  }

  ct_lvl     <- cor_at_lags(df$log_fews,  df$log_usda)
  ct_ret     <- cor_at_lags(df$dlog_fews, df$dlog_usda)
  ct_lvl_des <- cor_at_lags(resid_of(df$log_fews,  df$moy),
                            resid_of(df$log_usda,  df$moy))
  ct_ret_des <- cor_at_lags(resid_of(df$dlog_fews, df$moy),
                            resid_of(df$dlog_usda, df$moy))

  add_meta <- function(ct, basis) {
    ct$basis           <- basis
    ct$fews_commodity  <- fews_commodity
    ct$product_source  <- product_source
    ct$usda_slug       <- usda_slug
    ct[, c("fews_commodity", "product_source", "usda_slug",
           "basis", "lag", "r", "n", "p")]
  }
  combined <- rbind(
    add_meta(ct_lvl,     "levels"),
    add_meta(ct_ret,     "returns"),
    add_meta(ct_lvl_des, "levels_des"),
    add_meta(ct_ret_des, "returns_des")
  )

  # --- 2x2 lag-scan plot ----------------------------------------------------
  plot_df <- combined
  plot_df$basis <- factor(plot_df$basis,
                          levels = c("levels", "returns",
                                     "levels_des", "returns_des"),
                          labels = c("log levels (raw)",
                                     "Δlog returns (raw)",
                                     "log levels (deseasonalized)",
                                     "Δlog returns (deseasonalized)"))
  p <- ggplot(plot_df, aes(lag, r)) +
    geom_hline(yintercept = 0, color = "grey50") +
    geom_hline(yintercept = c(-GO_THRESHOLD, GO_THRESHOLD),
               linetype = "dashed", color = "#b40426") +
    geom_line(color = "#1d4f93", linewidth = 0.7) +
    geom_point(color = "#1d4f93", size = 2) +
    facet_wrap(~ basis, ncol = 2) +
    scale_x_continuous(breaks = LAGS) +
    coord_cartesian(ylim = c(-1, 1)) +
    labs(
      title = pair_label,
      subtitle = sprintf("Overlap: %s -- %s (n=%d months). Dashed = +/-%.2f threshold.",
                         format(min(overlap$month)),
                         format(max(overlap$month)),
                         nrow(overlap), GO_THRESHOLD),
      x = "Lag of USDA (months; positive = past USDA predicts current FEWS)",
      y = "Pearson r"
    ) +
    theme_minimal(base_size = 11)

  slug <- pair_slug(fews_commodity, product_source, usda_slug)
  ggsave(file.path(PLOT_DIR, sprintf("lag_scan_%s.png", slug)),
         p, width = 10, height = 6, dpi = 110)

  # --- level overlay since 2019 ---------------------------------------------
  # Both series are USD-denominated (FEWS via prevailing FX rate, USDA in
  # nominal USD/kg). We still need a per-axis rescale because they're in
  # different unit packs (e.g. FEWS is USD per 6-lb bag while USDA is USD/kg),
  # but the depreciation-driven trend that confounded the HTG vs USD overlay
  # is now removed.
  ov <- df[df$month >= as.Date("2019-01-01") & !is.na(df$month),
           c("month", "price_usd", "price_usd_kg")]
  if (nrow(ov) > 6
      && any(!is.na(ov$price_usd)) && any(!is.na(ov$price_usd_kg))) {
    scl <- max(ov$price_usd, na.rm = TRUE) / max(ov$price_usd_kg, na.rm = TRUE)
    fews_lbl <- "FEWS retail (USD/unit, FX-adjusted)"
    usda_lbl <- sprintf("USDA %s (USD/kg, rescaled)", usda_slug)
    po <- ggplot(ov, aes(month)) +
      geom_line(aes(y = price_usd, color = fews_lbl),
                linewidth = 0.6, na.rm = TRUE) +
      geom_line(aes(y = price_usd_kg * scl, color = usda_lbl),
                linewidth = 0.6, na.rm = TRUE) +
      scale_y_continuous(
        name = "FEWS retail (USD/unit)",
        sec.axis = sec_axis(~ . / scl, name = "USDA (USD/kg)")
      ) +
      scale_color_manual("", values = setNames(c("#1d4f93", "#b40426"),
                                                c(fews_lbl, usda_lbl))) +
      labs(title = pair_label, x = NULL) +
      theme_minimal(base_size = 11) +
      theme(legend.position = "bottom")
    ggsave(file.path(PLOT_DIR, sprintf("overlay_%s.png", slug)),
           po, width = 10, height = 5, dpi = 110)
  }

  combined
}

# -----------------------------------------------------------------------------
# 5. Run all pairs + aggregate
# -----------------------------------------------------------------------------

all_results <- do.call(rbind, lapply(seq_len(nrow(PAIRS)), function(i) {
  run_one_pair(PAIRS$fews_commodity[i],
               PAIRS$product_source[i],
               PAIRS$usda_slug[i])
}))

if (is.null(all_results) || nrow(all_results) == 0) {
  stop("No pairs produced results. Did the prep scripts run?")
}

write.csv(all_results,
          file.path(OUT_DIR, "cor_all_pairs.csv"), row.names = FALSE)

# Best |r| per (pair, basis) -- the headline number for each combo.
best <- all_results %>%
  group_by(fews_commodity, product_source, usda_slug, basis) %>%
  filter(!is.na(r)) %>%
  slice_max(order_by = abs(r), n = 1, with_ties = FALSE) %>%
  ungroup() %>%
  arrange(basis, desc(abs(r)))
write.csv(best, file.path(OUT_DIR, "best_lags.csv"), row.names = FALSE)

# -----------------------------------------------------------------------------
# 6. Summary heatmap (deseasonalized returns -- the cleanest signal)
# -----------------------------------------------------------------------------

heat <- all_results %>% filter(basis == "returns_des")
if (nrow(heat) > 0) {
  pair_label <- function(c, s) sprintf("%s (%s)", c, s)
  heat$pair  <- pair_label(heat$fews_commodity, heat$product_source)
  # Order pairs by their best |r| so the strongest pair sits at the top.
  best_per_pair <- heat %>%
    group_by(pair) %>%
    summarise(max_abs_r = max(abs(r), na.rm = TRUE)) %>%
    arrange(desc(max_abs_r))
  heat$pair <- factor(heat$pair, levels = best_per_pair$pair)

  ph <- ggplot(heat, aes(x = factor(lag), y = pair, fill = r)) +
    geom_tile(color = "white") +
    geom_text(aes(label = sprintf("%+.2f", r)), size = 2.8, color = "black") +
    scale_fill_gradient2(low = "#3a4cc0", mid = "white", high = "#b40426",
                         midpoint = 0, limits = c(-1, 1)) +
    labs(title = "FEWS retail vs USDA producer -- deseasonalized monthly returns",
         subtitle = "Pearson r at each lag. Positive lag = USDA leads FEWS.",
         x = "USDA lag (months)", y = NULL, fill = "r") +
    theme_minimal(base_size = 10) +
    theme(panel.grid = element_blank(),
          axis.text.y = element_text(family = "mono"))
  ggsave(file.path(PLOT_DIR, "summary_heatmap.png"),
         ph,
         width  = 9,
         height = max(4, 0.35 * length(unique(heat$pair)) + 2),
         dpi    = 110)
}

# -----------------------------------------------------------------------------
# 7. report.md
# -----------------------------------------------------------------------------

fmt_r <- function(r) if (is.na(r)) "NA" else sprintf("%+.3f", r)
fmt_p <- function(p) if (is.na(p)) "NA" else formatC(p, format = "g", digits = 2)
sig_stars <- function(p) {
  if (is.na(p)) ""
  else if (p < 0.001) "***"
  else if (p < 0.01)  "**"
  else if (p < 0.05)  "*"
  else                ""
}

best_ret_des <- best %>% filter(basis == "returns_des")
verdict_for_row <- function(r) {
  if (is.na(r$r)) {
    sprintf("- %s (%s) ↔ %s: insufficient data", r$fews_commodity, r$product_source, r$usda_slug)
  } else if (abs(r$r) >= GO_THRESHOLD) {
    sprintf("- **GO** %s (%s) ↔ %s: |r| = %.3f at lag = %+d (n = %d) %s",
            r$fews_commodity, r$product_source, r$usda_slug,
            abs(r$r), r$lag, r$n, sig_stars(r$p))
  } else {
    sprintf("- no-go %s (%s) ↔ %s: best |r| = %.3f at lag = %+d (n = %d)",
            r$fews_commodity, r$product_source, r$usda_slug,
            abs(r$r), r$lag, r$n)
  }
}

best_table_section <- function(basis_label) {
  rows <- best %>% filter(basis == basis_label) %>% arrange(desc(abs(r)))
  if (nrow(rows) == 0) return(c("(no rows)", ""))
  out <- c("",
           "| FEWS commodity | source | USDA | best lag | r | n | p | sig |",
           "|---|---|---|---:|---:|---:|---:|---|")
  for (i in seq_len(nrow(rows))) {
    out <- c(out, sprintf("| %s | %s | %s | %+d | %s | %d | %s | %s |",
                          rows$fews_commodity[i], rows$product_source[i],
                          rows$usda_slug[i], rows$lag[i],
                          fmt_r(rows$r[i]), rows$n[i],
                          fmt_p(rows$p[i]), sig_stars(rows$p[i])))
  }
  out
}

md <- c(
  "# USDA-vs-FEWS multi-pair correlation experiment",
  "",
  sprintf("Pairs tested: %d. USDA series fetched: %d.",
          nrow(PAIRS), length(unique(usda$usda_slug))),
  sprintf("FEWS data through: %s.", format(max(fews$month, na.rm = TRUE))),
  sprintf("USDA data through: %s.", format(max(usda$month, na.rm = TRUE))),
  "",
  "## Verdict per pair (deseasonalized monthly returns)",
  "",
  "Headline test: does USDA US-producer price *change* lead the FEWS Haiti",
  "retail price *change* at any lag from −6 to +18 months, after removing",
  "month-of-year seasonality from both sides?",
  "",
  "**FX control:** FEWS retail prices are taken in USD (FEWS's own conversion",
  "using the prevailing HTG/USD rate at observation time). This strips out",
  "HTG depreciation / domestic inflation, which would otherwise produce a",
  "shared trend with any nominal USD series.",
  "",
  if (nrow(best_ret_des) > 0)
    paste(vapply(seq_len(nrow(best_ret_des)),
                 function(i) verdict_for_row(best_ret_des[i, , drop = FALSE]),
                 character(1)),
          collapse = "\n")
  else "(no pairs)",
  "",
  sprintf("Threshold for GO: |r| >= %.2f at the best lag (n ~ 70 → p ~ 0.01).",
          GO_THRESHOLD),
  "",
  "## Best lag per (pair, basis)",
  "",
  "### Deseasonalized monthly returns (headline)",
  best_table_section("returns_des"),
  "",
  "### Raw monthly returns",
  best_table_section("returns"),
  "",
  "### Deseasonalized log levels",
  best_table_section("levels_des"),
  "",
  "### Raw log levels (interpret with care — trend-shared)",
  best_table_section("levels"),
  "",
  "## Plots",
  "",
  "- ![Summary heatmap (deseasonalized returns)](plots/summary_heatmap.png)",
  "- One `lag_scan_<pair>.png` per pair (4-panel: levels/returns × raw/deseasonalized)",
  "- One `overlay_<pair>.png` per pair (level overlay since 2019, dual axis)",
  "",
  "## Caveats",
  "",
  "- USDA `BEANS, DRY EDIBLE, (EXCL CHICKPEAS)` is the aggregate dry-bean",
  "  class (Black + Pinto + Navy + Kidney + ...). Black-specific FEWS prices",
  "  are paired against this aggregate. Multiple FEWS bean variants share",
  "  the same USDA series.",
  "- Sugar uses sugarcane producer-price (the dominant raw-cane series).",
  "  FEWS measures refined retail; the supply chain is multi-step.",
  "- Vegetable oil pairs against SOYBEAN producer price (US oils are mostly",
  "  soybean by volume) rather than a direct refined-oil price.",
  "- All USDA series are US-producer prices; FEWS is Haitian retail. Each",
  "  pair therefore measures a global producer-price signal against a",
  "  country-level retail signal with import + markup steps in between.",
  "- Coverage windows differ by USDA series. Check `n` in best_lags.csv before",
  "  acting on a marginal pair.",
  "- Reported p-values assume independence — they ignore autocorrelation in",
  "  the residuals so the true p is somewhat larger. Treat as a sanity",
  "  signal, not a statistical test."
)
writeLines(md, file.path(OUT_DIR, "report.md"))

cat(sprintf("\nWrote:\n  %s\n  %s\n  %s/lag_scan_*.png + overlay_*.png + summary_heatmap.png\n  %s\n",
            file.path(OUT_DIR, "cor_all_pairs.csv"),
            file.path(OUT_DIR, "best_lags.csv"),
            PLOT_DIR,
            file.path(OUT_DIR, "report.md")))

cat("\nTop 5 pairs by |r| on deseasonalized returns:\n")
top5 <- best_ret_des %>% arrange(desc(abs(r))) %>% head(5)
for (i in seq_len(nrow(top5))) {
  cat(sprintf("  %s (%s) <-> %s: r=%+.3f at lag=%+d (n=%d)\n",
              top5$fews_commodity[i], top5$product_source[i],
              top5$usda_slug[i], top5$r[i], top5$lag[i], top5$n[i]))
}
