"""Spike: one prompt to the deployed engine through the inbound gateway; save everything raw.

Creates a session (:query async_create_session), streams :streamQuery?alt=sse, writes the raw SSE
lines and any HTTP error body to demo/evidence/raw-run/<label>.*, and prints a short summary.
Usage: python3 -I infra/spike_query.py ENGINE LABEL "prompt"
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

engine, label, prompt = sys.argv[1:4]
region = engine.split("/")[3]
base = f"https://{region}-aiplatform.googleapis.com/v1beta1/{engine}"
tok = subprocess.check_output([os.environ.get("GCLOUD", "gcloud"), "auth", "print-access-token"], text=True).strip()
hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
out = os.path.join(os.path.dirname(__file__), "..", "demo", "evidence", "raw-run")
os.makedirs(out, exist_ok=True)


def post(url, body, stream=False):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdr, method="POST")
    return urllib.request.urlopen(req, timeout=300)


t0 = time.time()
meta = {"label": label, "prompt": prompt, "engine": engine, "t0_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0))}
try:
    s = json.load(post(f"{base}:query", {"class_method": "async_create_session", "input": {"user_id": "spike"}}))
    session_id = s["output"]["id"]
    meta["session_id"] = session_id
    body = {"class_method": "async_stream_query",
            "input": {"user_id": "spike", "session_id": session_id, "message": prompt}}
    lines, first = [], None
    with post(f"{base}:streamQuery?alt=sse", body) as resp:
        meta["status"] = resp.status
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.strip():
                first = first or time.time()
                lines.append(line)
    open(os.path.join(out, f"{label}.sse"), "w").write("\n".join(lines) + "\n")
    meta["events"] = len(lines)
    meta["first_event_s"] = round(first - t0, 2) if first else None
except urllib.error.HTTPError as e:
    meta["status"] = e.code
    meta["error_body"] = e.read().decode("utf-8", "replace")
    meta["error_headers"] = dict(e.headers)
meta["total_s"] = round(time.time() - t0, 2)
json.dump(meta, open(os.path.join(out, f"{label}.meta.json"), "w"), indent=1)
print(json.dumps({k: v for k, v in meta.items() if k != "error_headers"}, indent=1)[:3000])
