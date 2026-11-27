"""Send prompts straight to Model Armor sanitizeUserPrompt on a template; print the verdict per filter.
Usage: python3 -I evals/armor_probe.py TEMPLATE_RESOURCE < prompts.txt  (one prompt per line; needs TOKEN env)"""
import json, os, sys, urllib.request

tpl = sys.argv[1]
region = tpl.split("/")[3]
url = f"https://modelarmor.{region}.rep.googleapis.com/v1/{tpl}:sanitizeUserPrompt"
hdr = {"Authorization": f"Bearer {os.environ['TOKEN']}", "Content-Type": "application/json",
       "x-goog-user-project": tpl.split("/")[1]}
for prompt in (l.rstrip("\n") for l in sys.stdin if l.strip()):
    body = json.dumps({"userPromptData": {"text": prompt}}).encode()
    res = json.load(urllib.request.urlopen(urllib.request.Request(url, data=body, headers=hdr)))
    r = res["sanitizationResult"]
    hits = []
    for name, fr in r.get("filterResults", {}).items():
        inner = next(iter(fr.values())) if isinstance(fr, dict) else {}
        state = inner.get("matchState") or inner.get("inspectResult", {}).get("matchState")
        if state == "MATCH_FOUND":
            hits.append(name)
    print(f"{r['filterMatchState']:15s} {','.join(hits) or '-':28s} {prompt[:80]}")
