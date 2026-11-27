#!/usr/bin/env bash
# Tool-level egress policy on the MCP server's registry entry (IAP policy V1): the agent may call the
# six analytics tools; get_customer_contact is implicitly denied. The empty tool name lets the MCP
# handshake (initialize, notifications/initialized, tools/list) through.
# Usage: ./grant_mcp.sh AGENT_PRINCIPAL      (principal://agents.global.org-.../reasoningEngines/ID)
set -euo pipefail
cd "$(dirname "$0")" && source env.sh
AGENT_PRINCIPAL=$1
MCP_RID=$($GCLOUD agent-registry services describe ${REGION}-agent-demo-mcp --location=$REGION --project=$PROJ_ID --format='value(registryResource)')
MCP_RID=${MCP_RID##*/}
ETAG=$($GCLOUD beta iap web get-iam-policy --resource-type=agent-registry --mcp-server=$MCP_RID --region=$REGION --project=$PROJ_ID --format='value(etag)')
cat > mcp-policy.json <<JSON
{
  "bindings": [{
    "role": "roles/iap.egressor",
    "members": ["${AGENT_PRINCIPAL}"],
    "condition": {
      "title": "pipelines analyst: analytics tools only",
      "description": "Allows the six read-only analytics tools; get_customer_contact (PII) is not listed, so it is denied.",
      "expression": "api.getAttribute('iap.googleapis.com/mcp.toolName', '') in ['get_latest_run', 'get_model_summary', 'list_top_churn_risk', 'get_churn_drivers', 'get_clv_segments', 'get_demand_outlook', '']"
    }
  }],
  "etag": "${ETAG}",
  "version": 3
}
JSON
$GCLOUD -q beta iap web set-iam-policy mcp-policy.json --resource-type=agent-registry --mcp-server=$MCP_RID --region=$REGION --project=$PROJ_ID >/dev/null
$GCLOUD beta iap web get-iam-policy --resource-type=agent-registry --mcp-server=$MCP_RID --region=$REGION --project=$PROJ_ID
