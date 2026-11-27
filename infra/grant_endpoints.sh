#!/usr/bin/env bash
# roles/iap.egressor for this project's Agent Runtime identities (AGENT_PSET) on every Google API
# endpoint in the registry (IAP policy V1). Never on the registry root. The MCP server gets its own
# tool-conditioned policy in grant_mcp.sh. Usage: ./grant_endpoints.sh
set -uo pipefail
cd "$(dirname "$0")" && source env.sh
cat > /tmp/agent-demo-ep-policy.json <<JSON
{"bindings": [{"role": "roles/iap.egressor", "members": ["${AGENT_PSET}"]}]}
JSON
$GCLOUD agent-registry services list --location=$REGION --project=$PROJ_ID \
  --format='value(displayName,registryResource)' |
while IFS=$'\t' read -r name rres; do
  rid=${rres##*/}
  case "$rres" in */endpoints/*) ;; *) continue ;; esac
  $GCLOUD -q beta iap web set-iam-policy /tmp/agent-demo-ep-policy.json --resource-type=agent-registry \
    --endpoint="$rid" --region=$REGION --project=$PROJ_ID >/dev/null 2>grant.err \
    && echo "ok   $name" || echo "FAIL $name: $(tail -2 grant.err)"
done
