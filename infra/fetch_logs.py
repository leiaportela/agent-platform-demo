"""Fetch the platform log records for a time window (gateway requests + Model Armor) and save raw.
Usage: source infra/env.sh && python3 -I infra/fetch_logs.py LABEL SINCE_UTC [UNTIL_UTC]"""
import json, os, subprocess, sys, urllib.request

label, since = sys.argv[1], sys.argv[2]
until = sys.argv[3] if len(sys.argv) > 3 else None
P = os.environ.get("PROJ_ID") or sys.exit("PROJ_ID is not set: run `source infra/env.sh` first")
tok = subprocess.check_output([os.environ.get("GCLOUD", "gcloud"), "auth", "print-access-token"], text=True).strip()
win = f'timestamp>="{since}"' + (f' AND timestamp<="{until}"' if until else "")
flt = (f'({win}) AND (resource.type="networkservices.googleapis.com/Gateway" OR '
       f'logName="projects/{P}/logs/modelarmor.googleapis.com%2Fsanitize_operations")')
body = json.dumps({"resourceNames": [f"projects/{P}"], "filter": flt, "orderBy": "timestamp asc", "pageSize": 500}).encode()
req = urllib.request.Request("https://logging.googleapis.com/v2/entries:list", data=body,
                             headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
entries = json.load(urllib.request.urlopen(req)).get("entries", [])
out = os.path.join(os.path.dirname(__file__), "..", "demo", "evidence", "raw-run", f"{label}.logs.json")
json.dump(entries, open(out, "w"), indent=1)
for e in entries:
    jp, hr = e.get("jsonPayload", {}), e.get("httpRequest", {})
    if e.get("resource", {}).get("type") == "networkservices.googleapis.com/Gateway":
        gw = e["resource"].get("labels", {}).get("gateway_name", "?")
        mcp = jp.get("agentGatewayInfo", {}).get("mcpInfo", {})
        print(f'GW  {gw:22s} {hr.get("status","-")!s:4s} {jp.get("authzPolicyInfo",{}).get("result","-"):8s} '
              f'{mcp.get("method","")} {mcp.get("parameter","")} {hr.get("requestUrl","")[:110]}')
    else:
        sr = jp.get("sanitizationResult", {})
        print(f'MA  {jp.get("operationType","?"):24s} {sr.get("filterMatchState","?")} '
              f'{e.get("labels",{}).get("modelarmor.googleapis.com/client_name","")}')
print(len(entries), "entries saved to", out)
