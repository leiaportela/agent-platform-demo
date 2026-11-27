"""Add a READER entry for a member to a BigQuery dataset's access list (idempotent).
`bq add-iam-policy-binding --dataset` needs allowlisting in this project, so this edits the
access list through the REST API. Usage: python3 ds_grant.py PROJECT DATASET SA_EMAIL"""
import json, subprocess, sys, urllib.request

project, ds, member = sys.argv[1:]
tok = subprocess.check_output(["/snap/bin/gcloud", "auth", "print-access-token"], text=True).strip()
url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{ds}"
hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
cur = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=hdr)))
entry = {"role": "READER", "userByEmail": member}
if any(a.get("userByEmail") == member and a.get("role") == "READER" for a in cur.get("access", [])):
    print("already granted"); sys.exit(0)
body = json.dumps({"access": cur["access"] + [entry], "etag": cur["etag"]}).encode()
req = urllib.request.Request(url, data=body, headers=hdr, method="PATCH")
print("granted", json.load(urllib.request.urlopen(req))["datasetReference"])
