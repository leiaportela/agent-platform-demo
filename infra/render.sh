#!/usr/bin/env bash
# Render the gateway / authz YAML templates in infra/templates/ with the values from env.sh, into
# infra/rendered/ (gitignored), ready for `gcloud ... import --source=infra/rendered/<name>.yaml`.
# Only ${PROJ_ID} and ${REGION} are substituted. Usage: ./render.sh
set -euo pipefail
cd "$(dirname "$0")" && source env.sh
mkdir -p rendered
for t in templates/*.yaml; do
  envsubst '${PROJ_ID} ${REGION}' < "$t" > "rendered/$(basename "$t")"
done
if grep -l -E 'your-project-id|\$\{' rendered/*.yaml; then echo "unrendered values left (above)" >&2; exit 1; fi
ls rendered/
