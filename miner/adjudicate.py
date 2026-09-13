#!/usr/bin/env python3
"""Step 6 - judge the candidates, one at a time, with a reason for each.

The reading pass is deliberately generous: it would rather surface a weak candidate than miss a
real one. This pass is the counterweight. Each candidate is judged alone against your rubric and
comes back KEEP, WATCH or DELETE with one sentence of justification, appended to a JSONL file so
every verdict stays auditable.

    python3 miner/adjudicate.py

KEEP   - a new, reusable rule your rubric does not already cover.
WATCH  - plausible, but said only once; it needs to recur.
DELETE - commentary about one day, context that expires, or a restatement of what you know.
"""
import json, re, sqlite3, time, urllib.request

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from settings import S  # noqa: E402

INBOX = str(Path(S.data_dir) / "findings.md")
OUT = str(Path(S.data_dir) / "adjudication.jsonl")
OLLAMA = S.ollama + "/api/chat"
MODEL = S.model

SYS = f"""You judge ONE candidate finding pulled from a video channel by the reading pass.
The reader already knows this ({S.rubric_name}):
{S.rubric}

- KEEP only if it is a NEW, REUSABLE rule or method that the list above does not already cover.
- DELETE if it is: commentary about one specific day, context that expires with the news, or a
  restatement of something the list already covers.
- WATCH if it is a plausible rule stated only ONCE (it needs to recur before it is worth keeping).
Answer with JSON only: {{"verdict":"KEEP|WATCH|DELETE","why":"<one sentence>"}}"""

def ask(cand: str) -> dict:
    body = {"model": MODEL, "stream": False, "think": False,
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": cand[:800]}],
            "format": {"type": "object",
                       "properties": {"verdict": {"type": "string"},
                                      "why": {"type": "string"}},
                       "required": ["verdict", "why"]},
            "options": {"temperature": 0.0, "num_ctx": 4096, "num_predict": 150}}
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=420))
    return json.loads(r["message"]["content"])

def main():
    text = open(INBOX).read()
    cands = []
    sec = None
    for line in text.splitlines():
        m = re.match(r"^## (\d{4}-\d{2}-\d{2} [\d:]+) · (\S+) · (.*)$", line)
        if m:
            sec = f"{m.group(1)} {m.group(2)}"
            continue
        if line.startswith("## WATCH") or line.startswith("(Reviewed"):
            sec = None
        if sec and line.startswith("- ") and "nothing flagged" not in line:
            cands.append((sec, line[2:].strip()))
    done = set()
    try:
        for ln in open(OUT):
            done.add(json.loads(ln)["cand"][:120])
    except FileNotFoundError:
        pass
    out = open(OUT, "a")
    print(f"candidates: {len(cands)}, already done: {len(done)}", flush=True)
    for sec, cand in cands:
        if cand[:120] in done:
            continue
    print("ADJUDICATION_DONE", flush=True)

main()
