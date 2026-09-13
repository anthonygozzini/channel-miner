"""Step 5 - have a local model read each new transcript against what you already know.

Counting is blind to an idea expressed in familiar words: something genuinely new can hide for
months inside vocabulary the indexer already tags. So a local model reads each new transcript in
chunks, is given your rubric - the numbered list of what you already know - and proposes only
what that rubric does not cover.

Bounded per run, resumable, and it declares its own truncation rather than trimming silently.

    python3 miner/read.py                  # read what is new, up to the configured caps
    python3 miner/read.py --ids VIDEOID    # force one video (state untouched)

Writes candidates to findings.md. It proposes; it never decides.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from settings import S  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
VTT_DIR = Path(S.data_dir) / "vtt"
LANG = S.lang_suffix
SEGMENTS = Path(S.data_dir) / "segments.jsonl"
STATE = Path(S.data_dir) / "read_state.json"
INBOX = Path(S.data_dir) / "findings.md"
COUNT = Path(S.data_dir) / "new_findings_count"
OLLAMA = S.ollama
MODEL = S.model
MAX_VIDEOS, MAX_CHUNKS, CHUNK_CHARS = 3, 8, 3500
INBOX_MAX_LINES = 400

FUNDAMENTALS = S.rubric   # from config.json

SYSTEM = f"""You screen new transcript excerpts from a video channel against a list of things
the reader already knows ({S.rubric_name}):
{FUNDAMENTALS}

REQUIREMENTS:
- new_concepts: a rule/method he TEACHES that NONE of the 24 covers. Method only.
- refinements: a quote that deepens HOW to apply one of the 24 (name its number).
- market_calls: dated views on specific assets/levels (asset + his view, short).
- Every item carries a SHORT VERBATIM Italian quote from the excerpt.

NOT WANTED: summaries, praise, generic advice, anything not quotable from the text,
channel talk/greetings, duplicates of a rubric item's own one-liner. Empty lists are the
normal answer for most excerpts.

CHECKLIST before answering: quote appears verbatim? concept truly absent from all 24? refinement
names a number 1-24? Answer ONLY the JSON."""

SCHEMA = {"type": "object",
          "required": ["new_concepts", "refinements", "market_calls"],
          "properties": {
              "new_concepts": {"type": "array", "items": {"type": "object",
                  "required": ["concept", "quote"],
                  "properties": {"concept": {"type": "string"}, "quote": {"type": "string"}}}},
              "refinements": {"type": "array", "items": {"type": "object",
                  "required": ["fundamental", "insight", "quote"],
                  "properties": {"fundamental": {"type": "integer"},
                                 "insight": {"type": "string"}, "quote": {"type": "string"}}}},
              "market_calls": {"type": "array", "items": {"type": "object",
                  "required": ["asset", "view"],
                  "properties": {"asset": {"type": "string"}, "view": {"type": "string"}}}}}}


def ask(user: str, timeout=300):
    body = json.dumps({"model": MODEL, "stream": False, "think": False, "format": SCHEMA,
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": user}],
                       "options": {"temperature": 0.0, "num_ctx": 4096,
                                   "num_predict": 500}}).encode()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
            return json.loads(r.get("message", {}).get("content") or "{}")
        except Exception as exc:  # noqa: BLE001 — ollama busy/reloading: wait and retry
            last = exc
            time.sleep(10 * (attempt + 1))
    raise last


def video_text(vid: str) -> str:
    """Kept segments for the id; raw de-timestamped vtt when the index kept almost nothing."""
    segs = []
    if SEGMENTS.exists():
        with open(SEGMENTS) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    if r.get("id") == vid:
                        segs.append(r.get("text") or "")
                except Exception:  # noqa: BLE001
                    continue
    if len(segs) >= 3:
        return "\n".join(segs)
    vtt = VTT_DIR / f"{vid}.{LANG}.vtt"
    if not vtt.exists():
        return "\n".join(segs)
    out = []
    for ln in vtt.read_text(errors="ignore").splitlines():
        if ln.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or "-->" in ln or not ln.strip():
            continue
        out.append(re.sub(r"<[^>]+>", "", ln).strip())
    return "\n".join(out)


def meta_of(vid: str) -> dict:
    try:
        return json.loads((VTT_DIR / f"{vid}.meta.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*", default=None, help="force these ids (state untouched)")
    a = ap.parse_args()
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except Exception:  # noqa: BLE001
            state = {}
    all_ids = sorted({p.name[: -len(f".{LANG}.vtt")] for p in VTT_DIR.glob(f"*.{LANG}.vtt")})
    if a.ids:
        todo = a.ids
    else:
        todo = [v for v in all_ids if v not in state][:MAX_VIDEOS]
    def _pending() -> int:
        """PENDING = candidate lines still in the file, which you delete as you review them —
        NOT this run's additions. Counting only the latest run would hide every unreviewed
        candidate the moment one quiet run followed."""
        try:
            return sum(1 for ln in INBOX.read_text().splitlines()
                       if ln.startswith("- ") and not ln.startswith("- nothing flagged"))
        except Exception:  # noqa: BLE001
            return 0

    if not todo:
        print("read: nothing new")
        COUNT.write_text(str(_pending()))
        return
    findings_total = 0
    blocks = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    for vid in todo:
        meta = meta_of(vid)
        title = (meta.get("title") or vid)[:90]
        text = video_text(vid)
        chunks = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]
        truncated = len(chunks) > MAX_CHUNKS
        chunks = chunks[:MAX_CHUNKS]
        nc, rf, mc, errs = [], [], [], 0
        for ch in chunks:
            if len(ch.split()) < 30:
                continue
            try:
                out = ask("EXCERPT:\n" + ch)
            except Exception:  # noqa: BLE001
                errs += 1
                continue
            nc += [x for x in (out.get("new_concepts") or []) if x.get("quote")]
            rf += [x for x in (out.get("refinements") or []) if x.get("quote")]
            mc += [x for x in (out.get("market_calls") or []) if x.get("view")]
        n = len(nc) + len(rf) + len(mc)
        findings_total += n
        b = [f"## {now} · {vid} · {title}" + (" · TRUNCATED (long live, first "
             f"{MAX_CHUNKS} chunks)" if truncated else "") + (f" · {errs} chunk errors" if errs else "")]
        if not n:
            b.append("- nothing flagged (normal for news/chatter content)")
        for x in nc:
            b.append(f"- **NEW-CONCEPT?** {x['concept']} — \"{x['quote'][:160]}\"")
        for x in rf:
            b.append(f"- refine #{x.get('fundamental', '?')}: {x['insight'][:120]} — \"{x['quote'][:160]}\"")
        for x in mc:
            b.append(f"- market: {x['asset']} — {x['view'][:140]}")
        blocks.append("\n".join(b))
        if not a.ids:
            state[vid] = now
        print(f"read: {vid} -> {n} findings ({len(chunks)} chunks, {errs} errors)", flush=True)
    header = ("# Findings — candidates from the automated reading pass\n\n"
              "Proposed by the reading pass, judged by the adjudication pass. You may overrule "
              "any entry. Newest first; the file is capped, so old candidates fall off.\n")
    old = ""
    if INBOX.exists():
        old = "\n".join(INBOX.read_text().split("\n")[3:])   # drop old header
    body = header + "\n" + "\n\n".join(blocks) + "\n" + old
    INBOX.write_text("\n".join(body.split("\n")[:INBOX_MAX_LINES]) + "\n")
    if not a.ids:
        STATE.write_text(json.dumps(state, indent=0))
        COUNT.write_text(str(_pending()))
    print(f"read: DONE — {findings_total} candidate findings -> {INBOX.name}")


if __name__ == "__main__":
    main()
