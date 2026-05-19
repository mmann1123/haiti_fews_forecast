#!/usr/bin/env Rscript
# =============================================================================
# experiments/intl_beans/correlations.R
#
# Reproducible correlation scan: USDA NASS US Dry Edible Beans (aggregate,
# excl chickpeas) price vs Haiti FEWS Black-bean retail price. Tests
# whether US producer prices LEAD Haitian retail prices at any practical
# lag (operational lag for using the signal in real-time is short --
# NASS data has only ~1-month release lag).
#
# Run from bayesian_analysis/:
#   Rscript experiments/intl_beans/correlations.R
#
# Outputs under experiments/intl_beans/out/:
#   cor_levels.csv, cor_returns.csv
#   cor_levels_deseasonalized.csv, cor_returns_deseasonalized.csv
#   plots/01_lag_scan_returns.png
#   plots/02_lag_scan_returns_deseasonalized.png
#   plots/03_overlay_levels.png  (FEWS vs USDA over time)
#   plots/04_scatter_best_lag.png
#   report.md
# =============================================================================

suppressPackageStartupMessages({
  library(dplyr)
  library(ggplot2)
})

USDA_PATH   <- "intl_beans_usda.csv"
PRICES_PATH <- "fews_price_timeseries_example.csv"
OUT_DIR     <- "experiments/intl_beans/out"
PLOT_DIR    <- file.path(OUT_DIR, "plots")
dir.create(PLOT_DIR, recursive = TRUE, showWarnings = FALSE)

LAGS <- c(-6, -3, 0, 1, 3, 6, 9, 12, 15, 18)
GO_NO_GO_THRESHOLD <- 0.30  # |r| above which we proceed to v09

# -----------------------------------------------------------------------------
# 1. Load + align
# -----------------------------------------------------------------------------

usda <- read.csv(USDA_PATH, stringsAsFactors = FALSE)
usda$month <- as.Date(usda$month)
usda <- usda[, c("month", "price_usd_kg")]
colnames(usda)[2] <- "usda"

pr <- read.csv(PRICES_PATH, stringsAsFactors = FALSE,
               na.strings = c("NA","NaN","nan",""))
pr$month <- as.Date(pr$month)
pr <- pr[, c("month", "median_price_htg")]
colnames(pr)[2] <- "fews"

df <- merge(pr, usda, by = "month", all = TRUE)
df <- df[order(df$month), ]
df$log_fews   <- log(df$fews)
df$log_usda   <- log(df$usda)
df$dlog_fews  <- c(NA, diff(df$log_fews))
df$dlog_usda  <- c(NA, diff(df$log_usda))
df$moy        <- factor(format(df$month, "%m"))

overlap <- df[!is.na(df$fews) & !is.na(df$usda), ]
cat("Overlap: ", as.character(min(overlap$month)), " to ",
    as.character(max(overlap$month)), " (n = ", nrow(overlap),
    " months)\n\n", sep = "")

# -----------------------------------------------------------------------------
# 2. Correlation tables
# -----------------------------------------------------------------------------

.p_from_r <- function(r, n) {
  if (is.na(r) || n < 4) return(NA_real_)
  t <- r * sqrt((n - 2) / (1 - r^2))
  2 * pt(-abs(t), df = n - 2)
}

resid_of <- function(y, df) {
  ok <- !is.na(y)
  if (sum(ok) < 12) return(rep(NA_real_, length(y)))
  fit <- lm(y[ok] ~ df$moy[ok])
  r <- rep(NA_real_, length(y))
  r[ok] <- residuals(fit)
  r
}

# x at lag L means x[t-L] (positive L = past USDA predicts current FEWS).
# We also test negative lags (does FEWS lead USDA?) for symmetry.
cor_table <- function(y, x, df, lags = LAGS, deseasonalize = FALSE) {
  if (deseasonalize) {
    y <- resid_of(y, df)
    x <- resid_of(x, df)
  }
  do.call(rbind, lapply(lags, function(L) {
    if (L >= 0) {
      x_lag <- dplyr::lag(x, L)
    } else {
      # negative lag: x at t-L = x[t + |L|], shift forward
      x_lag <- dplyr::lead(x, -L)
    }
    ok <- complete.cases(x_lag, y)
    n  <- sum(ok)
    r  <- if (n > 3) suppressWarnings(cor(x_lag[ok], y[ok])) else NA_real_
    data.frame(lag = L, r = r, n = n, p = .p_from_r(r, n))
  }))
}

ct_levels       <- cor_table(df$log_fews,  df$log_usda,  df)
ct_returns      <- cor_table(df$dlog_fews, df$dlog_usda, df)
ct_levels_des   <- cor_table(df$log_fews,  df$log_usda,  df, deseasonalize = TRUE)
ct_returns_des  <- cor_table(df$dlog_fews, df$dlog_usda, df, deseasonalize = TRUE)

write.csv(ct_levels,      file.path(OUT_DIR, "cor_levels.csv"),                row.names = FALSE)
write.csv(ct_returns,     file.path(OUT_DIR, "cor_returns.csv"),               row.names = FALSE)
write.csv(ct_levels_des,  file.path(OUT_DIR, "cor_levels_deseasonalized.csv"), row.names = FALSE)
write.csv(ct_returns_des, file.path(OUT_DIR, "cor_returns_deseasonalized.csv"),row.names = FALSE)

# -----------------------------------------------------------------------------
# 3. Plots
# -----------------------------------------------------------------------------

lag_scan_plot <- function(ct, title, subtitle) {
  ggplot(ct, aes(x = lag, y = r)) +
    geom_hline(yintercept = 0, color = "grey50") +
    geom_hline(yintercept = c(-GO_NO_GO_THRESHOLD, GO_NO_GO_THRESHOLD),
               linetype = "dashed", color = "#b40426") +
    geom_line(color = "#1d4f93", linewidth = 0.8) +
    geom_point(color = "#1d4f93", size = 2.4) +
    scale_x_continuous(breaks = LAGS) +
    labs(title = title, subtitle = subtitle,
         x = "Lag of USDA (months; positive = past USDA predicts current FEWS)",
         y = "Pearson r") +
    theme_minimal(base_size = 12)
}
ggsave(file.path(PLOT_DIR, "01_lag_scan_returns.png"),
       lag_scan_plot(ct_returns,
                     "USDA -> FEWS: monthly returns correlation by lag",
                     "Raw (trend not removed). Dashed = +/-0.30 go/no-go threshold."),
       width = 9, height = 5, dpi = 110)
ggsave(file.path(PLOT_DIR, "02_lag_scan_returns_deseasonalized.png"),
       lag_scan_plot(ct_returns_des,
                     "USDA -> FEWS: monthly returns (deseasonalized) by lag",
                     "Month-of-year partialled out from both sides. Dashed = +/-0.30 threshold."),
       width = 9, height = 5, dpi = 110)

# Overlay levels (with a secondary axis so both series fit despite scale gap).
overlay <- df[!is.na(df$fews) | !is.na(df$usda), c("month", "fews", "usda")]
overlay <- overlay[overlay$month >= as.Date("2019-01-01"), ]
scale_factor <- max(overlay$fews, na.rm = TRUE) / max(overlay$usda, na.rm = TRUE)
ggsave(file.path(PLOT_DIR, "03_overlay_levels.png"),
  ggplot(overlay, aes(x = month)) +
    geom_line(aes(y = fews, color = "FEWS Beans (HTG / 6-lb)"), linewidth = 0.6) +
    geom_line(aes(y = usda * scale_factor,
                  color = "USDA dry beans (USD / Kg, rescaled)"), linewidth = 0.6) +
    scale_y_continuous(
      name = "FEWS Beans (HTG / 6-lb)",
      sec.axis = sec_axis(~ . / scale_factor, name = "USDA dry beans (USD / Kg)")) +
    scale_color_manual("", values = c("FEWS Beans (HTG / 6-lb)" = "#1d4f93",
                                       "USDA dry beans (USD / Kg, rescaled)" = "#b40426")) +
    labs(title = "FEWS Haiti Beans vs USDA US dry beans (level overlay, 2019+)",
         x = NULL) +
    theme_minimal(base_size = 12) +
    theme(legend.position = "bottom"),
  width = 10, height = 5, dpi = 110)

# Scatter at the best deseasonalized-returns lag.
best_idx  <- which.max(abs(ct_returns_des$r))
best_lag  <- ct_returns_des$lag[best_idx]
best_r    <- ct_returns_des$r[best_idx]
best_n    <- ct_returns_des$n[best_idx]
y_des     <- resid_of(df$dlog_fews, df)
x_lag     <- (if (best_lag >= 0) dplyr::lag(df$dlog_usda, best_lag)
              else dplyr::lead(df$dlog_usda, -best_lag))
x_des     <- resid_of(x_lag, df)
zoom      <- data.frame(x = x_des, y = y_des)
zoom      <- zoom[complete.cases(zoom), ]
ggsave(file.path(PLOT_DIR, "04_scatter_best_lag.png"),
  ggplot(zoom, aes(x, y)) +
    geom_hline(yintercept = 0, color = "grey60") +
    geom_vline(xintercept = 0, color = "grey60") +
    geom_point(alpha = 0.7, size = 2) +
    geom_smooth(method = "lm", formula = y ~ x, se = TRUE,
                color = "#b40426", fill = "#b40426", alpha = 0.15) +
    labs(title = sprintf("USDA @ t-%d vs FEWS, deseasonalized returns", best_lag),
         subtitle = sprintf("n = %d, r = %.3f", best_n, best_r),
         x = sprintf("USDA dlog (residual after month-of-year) @ t-%d", best_lag),
         y = "FEWS dlog (residual after month-of-year)") +
    theme_minimal(base_size = 12),
  width = 8, height = 5, dpi = 110)

# -----------------------------------------------------------------------------
# 4. report.md
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

# Headline finding from deseasonalized returns
best_des_r <- best_r
verdict <- if (!is.na(best_des_r) && abs(best_des_r) >= GO_NO_GO_THRESHOLD) {
  sprintf("**GO: |r| = %.3f exceeds the %.2f threshold at lag = %d. Proceed to wire as a v09 regressor.**",
          abs(best_des_r), GO_NO_GO_THRESHOLD, best_lag)
} else {
  sprintf("**NO-GO: best |r| = %.3f at lag = %d is below the %.2f threshold. The USDA aggregate dry-bean series does not lead Haiti's FEWS beans strongly enough to justify wiring into the model. Document as a negative result.**",
          abs(best_des_r), best_lag, GO_NO_GO_THRESHOLD)
}

table_section <- function(ct, title) {
  lines <- c("", paste0("### ", title), "",
             "| lag (months) | r | n | p | sig |",
             "|---|---|---|---|---|")
  for (i in seq_len(nrow(ct))) {
    lines <- c(lines, sprintf("| %d | %s | %d | %s | %s |",
                              ct$lag[i],
                              fmt_r(ct$r[i]),
                              ct$n[i],
                              fmt_p(ct$p[i]),
                              sig_stars(ct$p[i])))
  }
  lines
}

md <- c(
  "# USDA-vs-FEWS correlation experiment",
  "",
  sprintf("**Overlap window:** %s to %s (n = %d months)",
          format(min(overlap$month)), format(max(overlap$month)), nrow(overlap)),
  "",
  "Tests whether US producer prices for dry edible beans (aggregate class,",
  "excluding chickpeas) lead Haiti's FEWS Beans (Black) retail price at any",
  "practical lag. If yes, the USDA series would be a candidate additional",
  "regressor for v09 (similar to ACLED but with a much shorter operational",
  "lag -- NASS releases monthly data with ~1-month delay vs ACLED's 12).",
  "",
  "## Verdict",
  "",
  verdict,
  "",
  "## Headline lag scan (deseasonalized monthly returns)",
  "",
  "This is the cleanest test -- removes both trend (returns) and month-of-year",
  "seasonality. Positive lag = past USDA predicts current FEWS.",
  "",
  table_section(ct_returns_des, "Deseasonalized Delta-log correlations"),
  "",
  "## Other tables",
  table_section(ct_returns,    "Raw Delta-log correlations"),
  table_section(ct_levels_des, "Deseasonalized log-level correlations"),
  table_section(ct_levels,     "Raw log-level correlations (interpret with care -- trend-shared)"),
  "",
  "## Plots",
  "",
  "- ![Lag scan, raw returns](plots/01_lag_scan_returns.png)",
  "- ![Lag scan, deseasonalized returns](plots/02_lag_scan_returns_deseasonalized.png)",
  "- ![Level overlay](plots/03_overlay_levels.png)",
  sprintf("- ![Scatter at best lag (t-%d)](plots/04_scatter_best_lag.png)", best_lag),
  "",
  "## Caveats",
  "",
  "- USDA series is the **aggregate** dry-edible-bean class (Black + Pinto +",
  "  Navy + Kidney + ...). Black beans are ~10-15% of US production by volume,",
  "  so this is a noisy proxy for what we'd ideally have (Black-specific).",
  "- USDA reports US producer prices; FEWS is Haitian retail. The supply",
  "  chain (US export -> import -> Haitian retail markup) is multi-step.",
  "- Coverage is short (since 2019). Power is limited; CIs are wide.",
  "- n ~ 70 vs r = 0.30 has p ~ 0.01, so the chosen threshold is roughly",
  "  the bar for 'significant at 1%'."
)
writeLines(md, file.path(OUT_DIR, "report.md"))

cat("\nWrote:\n  ", file.path(OUT_DIR, "report.md"), "\n",
    "  ", file.path(OUT_DIR, "cor_returns_deseasonalized.csv"), "\n",
    "  ", PLOT_DIR, "/01..04.png\n", sep = "")
cat("\nVerdict (printed for non-md consumers):\n")
cat("  ", verdict, "\n", sep = "")
