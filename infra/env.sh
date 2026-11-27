# Shared settings for the agent demo (source me).
export PROJ_ID=your-project-id
export PROJ_NO=123456789012
export ORG_ID=111111111111
export REGION=us-central1
export EGW=agent-demo-egress-gw
export IGW=agent-demo-ingress-gw
export EGW_URI=projects/${PROJ_ID}/locations/${REGION}/agentGateways/${EGW}
export IGW_URI=projects/${PROJ_ID}/locations/${REGION}/agentGateways/${IGW}
export STAGING_BUCKET=agent-demo-staging-${PROJ_NO}
export GCLOUD=/snap/bin/gcloud
export MCP_BASE_URL=https://agent-demo-mcp-123456789012.us-central1.run.app
export AGENT_PSET="principalSet://agents.global.org-${ORG_ID}.system.id.goog/attribute.platformContainer/aiplatform/projects/${PROJ_NO}"
export ENGINE=projects/123456789012/locations/us-central1/reasoningEngines/1234567890123456789
export AGENT_PRINCIPAL=principal://agents.global.org-111111111111.system.id.goog/resources/aiplatform/projects/123456789012/locations/us-central1/reasoningEngines/1234567890123456789
