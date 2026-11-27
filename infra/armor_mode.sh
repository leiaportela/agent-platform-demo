#!/usr/bin/env bash
# Switch the inbound gateway's Model Armor extension between the blocking template and the
# INSPECT_ONLY twin (before/after check 1). Usage: ./armor_mode.sh block|inspect
set -euo pipefail
cd "$(dirname "$0")" && source env.sh
case "${1:-}" in
  block) TPL=agent-demo-armor ;;
  inspect) TPL=agent-demo-armor-inspect ;;
  *) echo "usage: $0 block|inspect"; exit 2 ;;
esac
T=projects/${PROJ_ID}/locations/${REGION}/templates/${TPL}
envsubst '${PROJ_ID} ${REGION}' < templates/${IGW}-authz-modelarmor.yaml \
  | sed -E "s#templates/agent-demo-armor(-inspect)?\"#templates/${TPL}\"#g" > /tmp/igw-armor.yaml
grep -c "$TPL\"" /tmp/igw-armor.yaml >/dev/null
$GCLOUD service-extensions authz-extensions import ${IGW}-authz-modelarmor --source=/tmp/igw-armor.yaml \
  --location=$REGION --project=$PROJ_ID --quiet 2>&1 | grep -E 'model_armor_settings|updateTime'
echo "inbound Model Armor now uses $TPL ($(date -u +%FT%TZ))"
