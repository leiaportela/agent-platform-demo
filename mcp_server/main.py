"""MCP server for the agent demo: read-only tools over the Vertex AI Pipelines demo results.

Every tool reads the run-versioned tables the pipeline publishes to
`<project>.vertex_demo_dashboard`, except `get_customer_contact`, which reads real
personal data (name, email, address) from `bigquery-public-data.thelook_ecommerce.users`. That one
is the tool the outbound Agent Gateway denies for the agent; it exists so the deny is real.

Served as stateless JSON at /mcp (no SSE), so Agent Gateway can inspect every tools/call.
"""

import datetime
import decimal
import os

from fastmcp import FastMCP
from google.cloud import bigquery
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

# The project that holds vertex_demo_dashboard: BQ_PROJECT (infra/env.sh / the Cloud Run service), else the
# project of the runtime's credentials (on Cloud Run, the service's own project).
PROJECT = os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or None
DATASET = "vertex_demo_dashboard"
TABLES = frozenset({
    "runs", "step_timings", "models", "model_metrics", "churn_feature_importance",
    "churn_top_customers", "churn_confusion", "clv_error_by_segment", "clv_deciles",
    "demand_forecast", "demand_holdout_metrics",
})
MODELS = ("churn", "clv", "demand")

_client: bigquery.Client | None = None


def _bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=PROJECT)  # None: inferred from the credentials
    return _client


def _t(name: str) -> str:
    if name not in TABLES:
        raise ValueError(f"table {name!r} is not readable by this server")
    return f"`{_bq().project}.{DATASET}.{name}`"


def _plain(value):
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, decimal.Decimal):
        return round(float(value), 4)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _query(sql: str, **params) -> list[dict]:
    types = {int: "INT64", float: "FLOAT64", str: "STRING"}
    config = bigquery.QueryJobConfig(
        labels={"app": "agent-demo"},
        query_parameters=[bigquery.ScalarQueryParameter(k, types[type(v)], v) for k, v in params.items()],
    )
    return [_plain(dict(r)) for r in _bq().query(sql, job_config=config).result()]


def _latest_run() -> dict:
    rows = _query(
        f"SELECT run_id, status, started_at, ended_at, data_as_of, train_automl, console_url "
        f"FROM {_t('runs')} WHERE status = 'SUCCEEDED' ORDER BY started_at DESC LIMIT 1"
    )
    if not rows:
        raise ValueError("no successful pipeline run has been published yet")
    return rows[0]


mcp = FastMCP(name="agent-demo-predictions")


@mcp.tool(annotations={"readOnlyHint": True})
def get_latest_run() -> dict:
    """Latest successful Vertex AI Pipelines run: id, start/end time, data date, console link, and
    the duration of each pipeline step."""
    run = _latest_run()
    run["steps"] = _query(
        f"SELECT step, branch, status, duration_seconds FROM {_t('step_timings')} "
        f"WHERE run_id = @run_id AND NOT STARTS_WITH(step, 'vertex_demo') "
        f"AND NOT STARTS_WITH(step, 'vertex-demo') ORDER BY started_at LIMIT 50",
        run_id=run["run_id"],
    )
    return run


@mcp.tool(annotations={"readOnlyHint": True})
def get_model_summary(model: str) -> dict:
    """Training method, headline metric and all evaluation metrics of one model from the latest run.

    Args:
        model: one of "churn" (AutoML Tabular classifier), "clv" (custom XGBoost regressor with
            hyperparameter tuning, 180-day customer lifetime value) or "demand" (BigQuery ML
            ARIMA_PLUS daily units forecast per product category).

    Notes for interpreting churn: the headline metric is AUC-PR for the *reorders* class, and
    `positive_rate` is the share of test customers who reordered. Compare AUC-PR to that base rate.
    """
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}")
    run_id = _latest_run()["run_id"]
    rows = _query(
        f"SELECT model, training_method, model_resource_name, model_version, trained_at, "
        f"trained_in_run, headline_metric_name, headline_metric_value, console_url "
        f"FROM {_t('models')} WHERE run_id = @run_id AND model = @model LIMIT 1",
        run_id=run_id, model=model,
    )
    if not rows:
        raise ValueError(f"model {model!r} not found in run {run_id}")
    summary = rows[0]
    summary["run_id"] = run_id
    summary["metrics"] = {
        r["metric"]: r["value"]
        for r in _query(
            f"SELECT metric, value FROM {_t('model_metrics')} "
            f"WHERE run_id = @run_id AND model = @model ORDER BY metric",
            run_id=run_id, model=model,
        )
    }
    return summary


@mcp.tool(annotations={"readOnlyHint": True})
def list_top_churn_risk(limit: int = 10) -> dict:
    """Customers with the highest predicted churn probability in the latest run (at most 30), with
    the three features that drove each score.

    churn_score is the predicted probability that the customer has churned (no order in the last
    90 days). Customers are identified by user_id only.
    """
    limit = max(1, min(int(limit), 30))
    run_id = _latest_run()["run_id"]
    customers = _query(
        f"SELECT rank, user_id, churn_score, reason_1_feature, reason_1_value, reason_2_feature, "
        f"reason_2_value, reason_3_feature, reason_3_value FROM {_t('churn_top_customers')} "
        f"WHERE run_id = @run_id ORDER BY rank LIMIT @limit",
        run_id=run_id, limit=limit,
    )
    return {"run_id": run_id, "customers": customers}


@mcp.tool(annotations={"readOnlyHint": True})
def get_churn_drivers(threshold: float = 0.5) -> dict:
    """Global feature importance of the churn model, plus precision/recall at the decision threshold
    closest to `threshold` (from the test-set confusion matrix)."""
    run_id = _latest_run()["run_id"]
    drivers = _query(
        f"SELECT rank, feature, importance FROM {_t('churn_feature_importance')} "
        f"WHERE run_id = @run_id ORDER BY rank LIMIT 10",
        run_id=run_id,
    )
    confusion = _query(
        f"SELECT threshold, tp, fp, tn, fn, SAFE_DIVIDE(tp, tp + fp) AS precision, "
        f"SAFE_DIVIDE(tp, tp + fn) AS recall FROM {_t('churn_confusion')} "
        f"WHERE run_id = @run_id ORDER BY ABS(threshold - @threshold) LIMIT 1",
        run_id=run_id, threshold=float(threshold),
    )
    return {"run_id": run_id, "drivers": drivers, "at_threshold": confusion[0] if confusion else None}


@mcp.tool(annotations={"readOnlyHint": True})
def get_clv_segments() -> dict:
    """CLV (180-day lifetime value) model quality by customer segment, and predicted vs actual
    revenue by predicted-value decile (decile 1 = highest predicted value) with lift."""
    run_id = _latest_run()["run_id"]
    segments = _query(
        f"SELECT segment, customers, mae, rmse, bias FROM {_t('clv_error_by_segment')} "
        f"WHERE run_id = @run_id ORDER BY customers DESC LIMIT 10",
        run_id=run_id,
    )
    deciles = _query(
        f"SELECT decile, customers, predicted_revenue, actual_revenue, lift FROM {_t('clv_deciles')} "
        f"WHERE run_id = @run_id ORDER BY decile LIMIT 10",
        run_id=run_id,
    )
    return {"run_id": run_id, "segments": segments, "deciles": deciles}


@mcp.tool(annotations={"readOnlyHint": True})
def get_demand_outlook(days: int = 14, category: str = "") -> dict:
    """Forecast units per product category for the next `days` days (max 30) from the latest run,
    with the 90% band, plus each category's holdout accuracy (MAPE and band coverage).

    Args:
        days: forecast horizon in days, starting at the run's data date.
        category: optional exact category name (e.g. "Swim"); empty = top 10 categories by units.
    """
    days = max(1, min(int(days), 30))
    run = _latest_run()
    run_id = run["run_id"]
    where_cat = "AND category = @category" if category else ""
    params = {"run_id": run_id, "days": days}
    if category:
        params["category"] = category
    outlook = _query(
        f"SELECT category, ROUND(SUM(forecast_units), 1) AS units, ROUND(SUM(lower_bound), 1) AS low, "
        f"ROUND(SUM(upper_bound), 1) AS high, MIN(date) AS start_date, MAX(date) AS end_date "
        f"FROM {_t('demand_forecast')} WHERE run_id = @run_id AND kind = 'forecast' {where_cat} "
        f"AND date < DATE_ADD((SELECT MIN(date) FROM {_t('demand_forecast')} "
        f"WHERE run_id = @run_id AND kind = 'forecast'), INTERVAL @days DAY) "
        f"GROUP BY category ORDER BY units DESC LIMIT 10",
        **params,
    )
    cats = [r["category"] for r in outlook]
    accuracy = [
        r for r in _query(
            f"SELECT category, mape, coverage, holdout_days FROM {_t('demand_holdout_metrics')} "
            f"WHERE run_id = @run_id ORDER BY mape LIMIT 40",
            run_id=run_id,
        )
        if r["category"] in cats
    ]
    return {"run_id": run_id, "data_as_of": run["data_as_of"], "days": days,
            "outlook": outlook, "holdout_accuracy": accuracy}


@mcp.tool(annotations={"readOnlyHint": True})
def get_customer_contact(user_id: int) -> dict:
    """Name, email and postal address of one customer, for outreach.

    Reads directly identifying personal data from the source system
    (bigquery-public-data.thelook_ecommerce.users).
    """
    rows = _query(
        "SELECT id AS user_id, first_name, last_name, email, street_address, city, state, "
        "postal_code, country FROM `bigquery-public-data.thelook_ecommerce.users` "
        "WHERE id = @user_id LIMIT 1",
        user_id=int(user_id),
    )
    return rows[0] if rows else {"error": f"no customer {user_id}"}


async def health(request):
    return JSONResponse({"status": "ok"})


_mcp_app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

app = Starlette(
    routes=[Route("/health", health), Mount("/", app=_mcp_app)],
    lifespan=_mcp_app.lifespan,
)
