# Project-specific settings for the agent demo. This is the only file to edit.
# `source infra/env.sh` before any script in infra/, agent/deploy.py, the evals, or running the UI/MCP locally.
# A value already set in your shell wins over the default here.

# --- set these for your project ---
export PROJ_ID=${PROJ_ID:-your-project-id}            # gcloud config get-value project
export PROJ_NO=${PROJ_NO:-123456789012}               # gcloud projects describe $PROJ_ID --format='value(projectNumber)'
export ORG_ID=${ORG_ID:-111111111111}                 # gcloud projects get-ancestors $PROJ_ID (type organization)
export REGION=${REGION:-us-central1}
export PRESENTER=${PRESENTER:-presenter@example.com}  # who may open the chat UI through IAP
# Printed by agent/deploy.py: projects/<number>/locations/<region>/reasoningEngines/<id>
export ENGINE=${ENGINE:-}

# --- derived from the above; normally left alone ---
export GCLOUD=${GCLOUD:-gcloud}
export EGW=${EGW:-agent-demo-egress-gw}
export IGW=${IGW:-agent-demo-ingress-gw}
export EGW_URI=projects/${PROJ_ID}/locations/${REGION}/agentGateways/${EGW}
export IGW_URI=projects/${PROJ_ID}/locations/${REGION}/agentGateways/${IGW}
export STAGING_BUCKET=${STAGING_BUCKET:-agent-demo-staging-${PROJ_NO}}
export MCP_BASE_URL=${MCP_BASE_URL:-https://agent-demo-mcp-${PROJ_NO}.${REGION}.run.app}
export AGENT_PSET="principalSet://agents.global.org-${ORG_ID}.system.id.goog/attribute.platformContainer/aiplatform/projects/${PROJ_NO}"
export AGENT_PRINCIPAL=${ENGINE:+principal://agents.global.org-${ORG_ID}.system.id.goog/resources/aiplatform/${ENGINE}}
# The names the Python code reads.
export PROJECT=$PROJ_ID BQ_PROJECT=$PROJ_ID AGENT_ORG=$ORG_ID

if [ "$PROJ_ID" = your-project-id ]; then
  echo "infra/env.sh: PROJ_ID is still the placeholder; set your project's values at the top of this file" >&2
fi
