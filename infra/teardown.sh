#!/usr/bin/env bash
# Remove everything the agent demo created in your-project-id (run after the demo, on purpose).
# Asks for confirmation. Leaves the enabled APIs and the pipelines demo untouched.
set -uo pipefail
cd "$(dirname "$0")" && source env.sh
read -r -p "Delete all agent-demo resources in $PROJ_ID? Type 'delete agent-demo': " ok
[ "$ok" = "delete agent-demo" ] || { echo "aborted"; exit 1; }
run() { echo "+ $*"; "$@" --quiet 2>&1 | grep -v -i warn | tail -2; }

TOK=$($GCLOUD auth print-access-token)
# 1. Agent Runtime engine (force: deletes its sessions too)
/usr/bin/curl -s -X DELETE -H "Authorization: Bearer $TOK" \
  "https://$REGION-aiplatform.googleapis.com/v1beta1/$ENGINE?force=true" | head -c 300; echo
# 2. Gateways: policies, extensions, gateways
run $GCLOUD beta network-security authz-policies delete ${EGW}-authz-policy-iap --location=$REGION --project=$PROJ_ID
run $GCLOUD beta network-security authz-policies delete ${IGW}-authz-policy-modelarmor --location=$REGION --project=$PROJ_ID
for x in ${EGW}-authz-iap-dryrun ${EGW}-authz-iap-enforced ${IGW}-authz-modelarmor; do
  run $GCLOUD service-extensions authz-extensions delete $x --location=$REGION --project=$PROJ_ID
done
run $GCLOUD network-services agent-gateways delete $EGW --location=$REGION --project=$PROJ_ID
run $GCLOUD network-services agent-gateways delete $IGW --location=$REGION --project=$PROJ_ID
# 3. Agent Registry entries created by register.sh (ids start with the region prefix)
for id in $($GCLOUD agent-registry services list --location=$REGION --project=$PROJ_ID --format='value(name.basename())'); do
  case "$id" in ${REGION}-*) run $GCLOUD agent-registry services delete "$id" --location=$REGION --project=$PROJ_ID ;; esac
done
# 4. Cloud Run services, image repo, staging bucket
run $GCLOUD run services delete agent-demo-ui --region=$REGION --project=$PROJ_ID
run $GCLOUD run services delete agent-demo-mcp --region=$REGION --project=$PROJ_ID
run $GCLOUD artifacts repositories delete agent-demo --location=$REGION --project=$PROJ_ID
run $GCLOUD storage rm -r gs://$STAGING_BUCKET
# 5. Model Armor templates and the DLP inspect template
for t in agent-demo-armor agent-demo-armor-inspect; do
  /usr/bin/curl -s -X DELETE -H "Authorization: Bearer $TOK" -H "x-goog-user-project: $PROJ_ID" \
    "https://modelarmor.$REGION.rep.googleapis.com/v1/projects/$PROJ_ID/locations/$REGION/templates/$t"; echo
done
/usr/bin/curl -s -X DELETE -H "Authorization: Bearer $TOK" -H "x-goog-user-project: $PROJ_ID" \
  "https://dlp.googleapis.com/v2/projects/$PROJ_ID/locations/$REGION/inspectTemplates/agent-demo-ids"; echo
# 6. Project IAM bindings this demo added, then the service accounts
for b in "serviceAccount:agent-demo-build@$PROJ_ID.iam.gserviceaccount.com roles/cloudbuild.builds.builder" \
         "serviceAccount:agent-demo-mcp@$PROJ_ID.iam.gserviceaccount.com roles/bigquery.jobUser" \
         "serviceAccount:agent-demo-ui@$PROJ_ID.iam.gserviceaccount.com roles/aiplatform.user" \
         "serviceAccount:agent-demo-ui@$PROJ_ID.iam.gserviceaccount.com roles/logging.viewer" \
         "serviceAccount:agent-demo-ui@$PROJ_ID.iam.gserviceaccount.com roles/cloudtrace.user" \
         "serviceAccount:agent-demo-ui@$PROJ_ID.iam.gserviceaccount.com roles/logging.privateLogViewer"; do
  set -- $b; run $GCLOUD projects remove-iam-policy-binding $PROJ_ID --member=$1 --role=$2 --condition=None
done
echo "NOTE: Model Armor roles granted to service-$PROJ_NO@gcp-sa-aiplatform-re and service-<DEP_SERVICE_AGENT_NUMBER>@gcp-sa-dep,"
echo "      the IAP audit config, and the vertex_demo_dashboard READER entry for agent-demo-mcp are left in place;"
echo "      remove them by hand if wanted (they grant nothing once the resources above are gone)."
for sa in agent-demo-build agent-demo-mcp agent-demo-ui; do
  run $GCLOUD iam service-accounts delete $sa@$PROJ_ID.iam.gserviceaccount.com --project=$PROJ_ID
done
echo done
