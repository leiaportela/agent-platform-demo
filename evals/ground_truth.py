"""Expected facts for grading, computed with direct read-only SQL against BigQuery.

Written independently of mcp_server/main.py (it does not import it): if the MCP server and this
file disagree, the eval shows it. Output: a JSON object of facts (see FACT KEYS in README.md).

Usage: .venv/bin/python ground_truth.py [--run-id RUN] [--out ground_truth.json]
"""

from __future__ import annotations

import argparse
import datetime
import decimal
import json
import os
import sys

PROJECT = os.environ.get("BQ_PROJECT", "your-project-id")
DATASET = os.environ.get("BQ_DATASET", "vertex_demo_dashboard")
HORIZON_DAYS = 14


def _t(name: str) -> str:
    return f"`{PROJECT}.{DATASET}.{name}`"


def _plain(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return v


class BQ:
    def __init__(self) -> None:
        from google.cloud import bigquery

        self._bq = bigquery
        self.client = bigquery.Client(project=PROJECT)

    def rows(self, sql: str, **params) -> list[dict]:
        bq = self._bq
        kinds = {int: "INT64", float: "FLOAT64", str: "STRING"}
        cfg = bq.QueryJobConfig(
            labels={"app": "agent-demo-evals"},
            maximum_bytes_billed=200 * 1024 * 1024,
            query_parameters=[bq.ScalarQueryParameter(k, kinds[type(v)], v) for k, v in params.items()],
        )
        return [{k: _plain(v) for k, v in dict(r).items()} for r in self.client.query(sql, job_config=cfg).result()]


def compute(db: BQ, run_id: str | None = None) -> dict:
    if run_id:
        runs = db.rows(f"SELECT run_id, status, started_at, data_as_of FROM {_t('runs')} WHERE run_id = @r", r=run_id)
    else:
        runs = db.rows(
            f"SELECT run_id, status, started_at, data_as_of FROM {_t('runs')} "
            f"WHERE status = 'SUCCEEDED' ORDER BY started_at DESC LIMIT 1"
        )
    if not runs:
        raise SystemExit("no SUCCEEDED run found")
    run = runs[0]
    rid = run["run_id"]

    metrics: dict[str, dict[str, float]] = {}
    for r in db.rows(f"SELECT model, metric, value FROM {_t('model_metrics')} WHERE run_id = @r", r=rid):
        metrics.setdefault(r["model"], {})[r["metric"]] = r["value"]

    models = {
        r["model"]: {"training_method": r["training_method"], "headline_metric_name": r["headline_metric_name"],
                     "headline_metric_value": r["headline_metric_value"]}
        for r in db.rows(
            f"SELECT model, training_method, headline_metric_name, headline_metric_value "
            f"FROM {_t('models')} WHERE run_id = @r", r=rid)
    }

    top = db.rows(
        f"SELECT rank, user_id, churn_score FROM {_t('churn_top_customers')} WHERE run_id = @r ORDER BY rank LIMIT 30",
        r=rid,
    )
    drivers = db.rows(
        f"SELECT feature, importance FROM {_t('churn_feature_importance')} WHERE run_id = @r ORDER BY rank LIMIT 5",
        r=rid,
    )

    first = db.rows(
        f"SELECT MIN(date) AS d FROM {_t('demand_forecast')} WHERE run_id = @r AND kind = 'forecast'", r=rid
    )[0]["d"]
    cats = db.rows(
        f"SELECT category, SUM(forecast_units) AS units, SUM(lower_bound) AS low, SUM(upper_bound) AS high "
        f"FROM {_t('demand_forecast')} WHERE run_id = @r AND kind = 'forecast' "
        f"AND date >= @d AND date < DATE_ADD(@d, INTERVAL {HORIZON_DAYS} DAY) "
        f"GROUP BY category ORDER BY units DESC LIMIT 10",
        r=rid, d=first,
    ) if first else []
    holdout = {
        r["category"]: r
        for r in db.rows(f"SELECT category, mape, coverage FROM {_t('demand_holdout_metrics')} WHERE run_id = @r", r=rid)
    }
    top5 = cats[:5]

    churn, clv, demand = metrics.get("churn", {}), metrics.get("clv", {}), metrics.get("demand", {})
    scores = [c["churn_score"] for c in top]
    return {
        "computed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": f"{PROJECT}.{DATASET}",
        "latest_run": {"run_id": rid, "started_at": run["started_at"], "data_as_of": run["data_as_of"]},
        "churn": {
            "auc_pr": churn.get("auc_pr"), "positive_rate": churn.get("positive_rate"),
            "auc_roc": churn.get("auc_roc"), "precision": churn.get("precision"), "recall": churn.get("recall"),
            "threshold": churn.get("threshold"),
            "top5_user_ids": [str(c["user_id"]) for c in top[:5]],
            "top5_scores": scores[:5],
            "top30_score_min": min(scores) if scores else None,
            "top30_score_max": max(scores) if scores else None,
            "top30_score_spread": (max(scores) - min(scores)) if scores else None,
            "top_drivers": [d["feature"] for d in drivers[:3]],
        },
        "clv": {"rmse": clv.get("rmse"), "baseline_rmse": clv.get("baseline_rmse"), "r2": clv.get("r2"),
                "mae": clv.get("mae")},
        "demand": {
            "mape_mean": demand.get("mape_mean"), "coverage_mean": demand.get("coverage_mean"),
            "forecast_start": first, "horizon_days": HORIZON_DAYS,
            "top5_categories_14d": [c["category"] for c in top5],
            "top5_units_14d": [round(c["units"], 1) for c in top5],
            "top5_coverage": [holdout.get(c["category"], {}).get("coverage") for c in top5],
            "top5_mape": [holdout.get(c["category"], {}).get("mape") for c in top5],
            "top5_max_coverage": max((holdout.get(c["category"], {}).get("coverage") or 0) for c in top5) if top5 else None,
        },
        "models": models,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", help="grade against this run instead of the newest SUCCEEDED one")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "ground_truth.json"))
    args = ap.parse_args(argv)
    facts = compute(BQ(), args.run_id)
    with open(args.out, "w") as f:
        json.dump(facts, f, indent=2)
        f.write("\n")
    json.dump(facts, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
