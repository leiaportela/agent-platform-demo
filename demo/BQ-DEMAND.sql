-- Demand model (BigQuery ML ARIMA_PLUS): paste into BigQuery Studio, select one block, Run selection.
-- All blocks were tested on 2026-11-27 against run vertex-demo-e2e-4-20261126. Only block 3 writes anything,
-- and it writes a scratch model, never the pipeline's.

-- 1. The training data: one row per category per day, two years, zero-filled.
SELECT COUNT(DISTINCT category) AS series, COUNT(*) AS days_x_series,
       MIN(sale_date) AS first_day, MAX(sale_date) AS last_day, SUM(units) AS units
FROM `your-project-id.vertex_demo.demand_daily`;
-- → 26 series, 18,980 rows, 2024-11-26 .. 2026-11-25


-- 2. Why the last 6 days are left out: the synthetic dataset ramps up at its end (Chart tab: line).
SELECT sale_date, SUM(units) AS units_all_categories
FROM `your-project-id.vertex_demo.demand_daily`
WHERE sale_date >= DATE '2026-10-27'
GROUP BY sale_date ORDER BY sale_date;
-- → ~230-320 a day, then 348, 499, 610, 778, 1083, 1585 from Nov 20. The pipeline detects that day
--   (first day > 1.5x the median of the weeks before) and trains only on data before it.


-- 3. The model, exactly as the pipeline trains it, written to a scratch name (takes ~15 s).
CREATE OR REPLACE MODEL `your-project-id.vertex_demo.demand_model_live`
OPTIONS (
  model_type = 'ARIMA_PLUS',
  time_series_timestamp_col = 'sale_date',
  time_series_data_col = 'units',
  time_series_id_col = 'category',   -- one model per category, trained in one statement
  data_frequency = 'DAILY',
  holiday_region = 'US'
) AS
SELECT category, sale_date, units
FROM `your-project-id.vertex_demo.demand_daily`
WHERE sale_date < DATE '2026-11-20';   -- the detected ramp start


-- 4. What ARIMA_PLUS chose for each series (on the pipeline's model).
SELECT category,
       non_seasonal_p AS p, non_seasonal_d AS d, non_seasonal_q AS q, has_drift,
       ARRAY_TO_STRING(seasonal_periods, ', ') AS seasonality,
       has_holiday_effect, has_spikes_and_dips, has_step_changes, ROUND(AIC, 1) AS aic
FROM ML.ARIMA_EVALUATE(MODEL `your-project-id.vertex_demo.demand_model`)
ORDER BY category;
-- → mostly ARIMA(1,1,1) or (0,1,1) with drift, weekly seasonality, US holiday effects; chosen per series.


-- 5. The forecast for one category, with its 90% interval.
SELECT DATE(forecast_timestamp) AS day,
       ROUND(GREATEST(0, forecast_value), 1) AS forecast_units,
       ROUND(GREATEST(0, prediction_interval_lower_bound), 1) AS low_90,
       ROUND(prediction_interval_upper_bound, 1) AS high_90
FROM ML.FORECAST(MODEL `your-project-id.vertex_demo.demand_model`,
                 STRUCT(44 AS horizon, 0.9 AS confidence_level))
WHERE category = 'Accessories' AND DATE(forecast_timestamp) >= DATE '2026-11-26'
ORDER BY day;


-- 6. Where the forecast comes from: trend + weekly seasonality + holidays (Chart tab: lines).
SELECT DATE(time_series_timestamp) AS day, time_series_type,
       ROUND(time_series_data, 1) AS units, ROUND(trend, 2) AS trend,
       ROUND(seasonal_period_weekly, 2) AS weekly, ROUND(holiday_effect, 2) AS holiday
FROM ML.EXPLAIN_FORECAST(MODEL `your-project-id.vertex_demo.demand_model`,
                         STRUCT(30 AS horizon, 0.9 AS confidence_level))
WHERE category = 'Accessories' AND time_series_timestamp >= TIMESTAMP '2026-10-01'
ORDER BY day;


-- 7. How good it is: a 28-day backtest (a second model trained up to Oct 22, scored on Oct 23 - Nov 19).
SELECT category, ROUND(100 * mape, 1) AS mape_pct, ROUND(100 * coverage) AS in_90_band_pct, holdout_days
FROM `your-project-id.vertex_demo.demand_holdout_metrics`
ORDER BY category = 'ALL' DESC, mape;
-- → ALL: MAPE 39.2%, only 50% of days inside the "90%" band. The band is too narrow.


-- After the demo: DROP MODEL `your-project-id.vertex_demo.demand_model_live`;
