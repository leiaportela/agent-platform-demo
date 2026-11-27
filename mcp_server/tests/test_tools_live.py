"""Live smoke tests: every tool against real BigQuery (read-only). Run with ADC that can read the
dataset: `.venv/bin/pytest -q tests/test_tools_live.py`."""
import asyncio
import json

import pytest
from fastmcp import Client

import main


def call(name, **args):
    async def go():
        async with Client(main.mcp) as c:
            res = await c.call_tool(name, args)
            return res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    return asyncio.run(go())


def test_tools_listed():
    async def go():
        async with Client(main.mcp) as c:
            return {t.name for t in await c.list_tools()}
    assert asyncio.run(go()) == {
        "get_latest_run", "get_model_summary", "list_top_churn_risk", "get_churn_drivers",
        "get_clv_segments", "get_demand_outlook", "get_customer_contact"}


def test_latest_run():
    r = call("get_latest_run")
    assert r["status"] == "SUCCEEDED" and r["run_id"].startswith("vertex-demo-")
    assert r["steps"] and all(not s["step"].startswith("vertex") for s in r["steps"])


@pytest.mark.parametrize("model", ["churn", "clv", "demand"])
def test_model_summary(model):
    r = call("get_model_summary", model=model)
    assert r["model"] == model and r["metrics"]


def test_top_churn():
    r = call("list_top_churn_risk", limit=5)
    assert len(r["customers"]) == 5 and r["customers"][0]["rank"] == 1


def test_drivers_clv_demand():
    assert call("get_churn_drivers")["drivers"]
    assert call("get_clv_segments")["deciles"]
    d = call("get_demand_outlook", days=14)
    assert d["outlook"] and d["holdout_accuracy"]


def test_contact_returns_pii():
    r = call("get_customer_contact", user_id=9044)
    assert "email" in r
