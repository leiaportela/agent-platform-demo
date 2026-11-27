#!/usr/bin/env bash
# Registers the agent's destinations in the gateway's regional Agent Registry (idempotent):
# every host in google-api-hosts.txt (+ its .mtls. variant) as an endpoint, and the MCP server
# with its toolspec. Usage: ./register.sh
set -uo pipefail
cd "$(dirname "$0")" && source env.sh
existing=$($GCLOUD agent-registry services list --location=$REGION --project=$PROJ_ID --format='value(name.basename())' 2>/dev/null)
create_endpoint() {  # host
  local host=$1 id; id="${REGION}-$(echo "${host%.googleapis.com}" | tr '.' '-')"
  [ ${#id} -gt 63 ] && id=${id:0:63}
  if grep -qx "$id" <<<"$existing"; then echo "skip $host ($id)"; return; fi
  $GCLOUD agent-registry services create "$id" --location=$REGION --project=$PROJ_ID \
    --display-name="$host" --endpoint-spec-type=no-spec \
    --interfaces="url=https://${host},protocolBinding=JSONRPC" --quiet >/dev/null 2>reg.err \
    && echo "ok   $host ($id)" || { echo "FAIL $host: $(tail -2 reg.err)"; }
}
grep -v -E '^\s*(#|$)' google-api-hosts.txt | while read -r h; do
  create_endpoint "$h"
  create_endpoint "${h%.googleapis.com}.mtls.googleapis.com"
done
id=${REGION}-agent-demo-mcp
if grep -qx "$id" <<<"$existing"; then echo "skip mcp ($id)"; else
  $GCLOUD agent-registry services create "$id" --location=$REGION --project=$PROJ_ID \
    --display-name="agent-demo-mcp" --description="Pipelines demo predictions (read-only) + customer contact (PII)" \
    --mcp-server-spec-type=tool-spec --mcp-server-spec-content=toolspec.json \
    --interfaces="url=${MCP_BASE_URL}/mcp,protocolBinding=JSONRPC" --quiet 2>reg.err >/dev/null \
    && echo "ok   mcp ($id)" || echo "FAIL mcp: $(tail -3 reg.err)"
fi
