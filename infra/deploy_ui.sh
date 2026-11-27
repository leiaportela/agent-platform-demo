#!/usr/bin/env bash
# Build and deploy the chat UI to Cloud Run behind IAP (same pattern as the pipelines dashboard):
# no unauthenticated access, run.invoker only for the IAP service agent, IAP access for the presenter.
# One instance max (the log poller is per process; Cloud Logging read quota is per project).
# Usage: ./deploy_ui.sh [PRESENTER_EMAIL]
set -euo pipefail
cd "$(dirname "$0")" && source env.sh
PRESENTER=${1:-$PRESENTER}
[ -n "$ENGINE" ] || { echo "ENGINE is not set in env.sh (agent/deploy.py prints it)"; exit 1; }
TAG=$(date +%Y%m%d-%H%M%S)
IMG=$REGION-docker.pkg.dev/$PROJ_ID/agent-demo/ui:$TAG
$GCLOUD builds submit ../ui --tag=$IMG --region=$REGION --project=$PROJ_ID \
  --service-account=projects/$PROJ_ID/serviceAccounts/agent-demo-build@$PROJ_ID.iam.gserviceaccount.com \
  --default-buckets-behavior=regional-user-owned-bucket --quiet | tail -3
$GCLOUD run deploy agent-demo-ui --image=$IMG --region=$REGION --project=$PROJ_ID \
  --service-account=agent-demo-ui@$PROJ_ID.iam.gserviceaccount.com \
  --no-allow-unauthenticated --iap --ingress=all \
  --min-instances=1 --max-instances=1 --timeout=600 --cpu=1 --memory=1Gi \
  --set-env-vars=ENGINE=$ENGINE,PROJECT=$PROJ_ID,REGION=$REGION,AGENT_ORG=$ORG_ID \
  --labels=app=agent-demo --quiet | tail -3
$GCLOUD run services add-iam-policy-binding agent-demo-ui --region=$REGION --project=$PROJ_ID \
  --member=serviceAccount:service-$PROJ_NO@gcp-sa-iap.iam.gserviceaccount.com --role=roles/run.invoker --quiet >/dev/null
$GCLOUD iap web add-iam-policy-binding --resource-type=cloud-run --service=agent-demo-ui --region=$REGION \
  --project=$PROJ_ID --member=user:$PRESENTER --role=roles/iap.httpsResourceAccessor --quiet >/dev/null
echo "UI: https://agent-demo-ui-$PROJ_NO.$REGION.run.app (IAP; $PRESENTER)"
