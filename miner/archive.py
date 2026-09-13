#!/usr/bin/env python3
"""Step 1 - walk the channel and keep what is new.

Reads the channel's streams / videos / shorts tabs, and for every video not seen before stores
its metadata, YouTube's own captions when they exist, and the live chat when there is one.
Nothing is fetched twice: the index is merged, never overwritten.

    python3 miner/archive.py [--limit N]     # N is per tab, so a catch-up run reaches shorts too

Outputs, under the configured data directory:
  index.csv          one row per video: id, kind, date, title, whether it has captions
  vtt/<id>.meta.json per-video metadata
  vtt/<id>.*.vtt     captions, when YouTube had them
  chat/<id>.jsonl    live chat, when there was one
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import time

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from settings import S  # noqa: E402

OUT = S.data_dir
LANG = S.lang_suffix
YTDLP = os.path.expanduser("~/.local/bin/yt-dlp")
CALL_PAT = re.compile(r"\b(long|short|entro|entrata|entriamo|stop ?loss|take ?profit|target|"
                      r"leva|liquidit|breakout|reclaim|swing|scalp)\b", re.I)
# A per-video count of which topics were mentioned, using the same topic list as the indexer.
IND = dict(S.topics)


def sh(args, timeout=300):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def parse_vtt(path):
    """Yield (rel_seconds, text) deduped (rolling captions repeat lines)."""
    seen, out, t = set(), [], 0.0
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        m = re.match(r"^(\d+):(\d+):(\d+)\.(\d+) --> ", line)
        if m:
            t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000
            continue
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        txt = re.sub(r"<[^>]+>", "", line).strip()
        if len(txt) < 4 or txt in seen:
            continue
        seen.add(txt)
        out.append((t, txt))
    return out



def parse_live_chat(path: str, start_ts: int = 0) -> list[dict]:
    """YouTube's live_chat.json -> flat messages, same shape as calls/ so both read alike.

    The raw file is one replay envelope per line with the message split into `runs` (text fragments
    interleaved with emoji objects) — joining the runs is what turns it back into a sentence.
    Timestamps are absolute usec; t_rel_s is offset from the stream start so a chat line can be
    lined up against the transcript line that provoked it.
    """
    out: list[dict] = []
    try:
        fh = open(path, errors="ignore")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            for a in d.get("replayChatItemAction", {}).get("actions", []):
                it = (a.get("addChatItemAction", {}).get("item", {})
                       .get("liveChatTextMessageRenderer"))
                if not it:
                    continue
                txt = "".join(
                    r.get("text", "") or (":" + r.get("emoji", {}).get("emojiId", "") + ":"
                                          if r.get("emoji") else "")
                    for r in it.get("message", {}).get("runs", []))
                if not txt.strip():
                    continue
                ts = int(it.get("timestampUsec", 0) or 0) // 1_000_000
                out.append({"t_rel_s": round(ts - start_ts, 1) if (start_ts and ts) else None,
                            "t_utc": ts or None,
                            "author": it.get("authorName", {}).get("simpleText", "?"),
                            "text": txt})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    for d in ("vtt", "calls", "chat"):
        os.makedirs(f"{OUT}/{d}", exist_ok=True)

    ids, kind = [], {}
    for url, k in S.tabs:
        r = sh([YTDLP, "--flat-playlist", "-J", url], timeout=600)
        ent = json.loads(r.stdout).get("entries", []) if r.returncode == 0 else []
        fresh = []
        for e in ent:
            vid = e.get("id")
            if vid and vid not in kind:      # a live also appears under /videos: streams win
                kind[vid] = k
                fresh.append(vid)
        # --limit is PER TAB, not over the concatenated list: the tabs are walked in a fixed
        # order, so a shared budget would always be spent before reaching the last one. Per-tab
        # means a bounded catch-up run tops up all three.
        ids += fresh[: a.limit] if a.limit else fresh
        print(f"{k}s listed: {len(ent)} (taking {len(fresh[: a.limit] if a.limit else fresh)})",
              flush=True)

    index, indicators = [], {}
    for k, vid in enumerate(ids):
        vtt = f"{OUT}/vtt/{vid}.{LANG}.vtt"
        meta_f = f"{OUT}/vtt/{vid}.meta.json"
        if not os.path.exists(meta_f):
            m = sh([YTDLP, "--skip-download", "-j", f"https://www.youtube.com/watch?v={vid}"])
            if m.returncode != 0:
                print(f"{vid}: meta FAILED (declared)", flush=True)
                continue
            try:
                md = json.loads(m.stdout)
            except ValueError:                   # a truncated payload skips THIS video only:
                print(f"{vid}: yt-dlp meta unparseable — skipped this pass", flush=True)
                continue                         # aborting the run would desync index and calls
            keep = {x: md.get(x) for x in ("id", "title", "duration", "timestamp",
                                           "release_timestamp", "upload_date", "was_live")}
            _mt = f"{meta_f}.tmp.{os.getpid()}"          # atomic
            json.dump(keep, open(_mt, "w"))
            os.replace(_mt, meta_f)
        try:
            md = json.load(open(meta_f))
        except Exception:                        # a torn meta must not poison every
            print(f"{vid}: meta unreadable — refetching (declared)", flush=True)
            try:
                os.remove(meta_f)
            except OSError:
                pass
            continue
        # CHAT BEFORE THE SUBS GATE: the chat pull sat AFTER the vtt `continue`,
        # so a live whose auto-captions YouTube hadn't produced yet — or never produces — got its
        # chat pulled NEVER. Four lives (Aug 1/4/5/6) sat sub-less with unpulled chats; the chat
        # replay is available the moment a stream ends and can expire, so it must be captured on
        # the FIRST pass, independent of whether subtitles ever arrive.
        # Gate on the CHANNEL TAB, not on the metadata flag. YouTube sets `was_live` only after it
        # finishes processing a stream, so a live archived soon after it ends reads was_live=False —
        # and because the meta file is then cached forever, its chat is never pulled and never
        # retried, losing that stream's chat permanently. The streams tab already says what
        # this video is, so trust the tab.
        if kind.get(vid) == "live" or md.get("was_live"):
            chat = f"{OUT}/chat/{vid}.live_chat.json"
            os.makedirs(f"{OUT}/chat", exist_ok=True)
            if not os.path.exists(chat):
                sh([YTDLP, "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                    "-o", f"{OUT}/chat/{vid}.%(ext)s",
                    f"https://www.youtube.com/watch?v={vid}"], timeout=900)
            parsed = f"{OUT}/chat/{vid}.jsonl"
            if os.path.exists(chat) and not os.path.exists(parsed):
                msgs = parse_live_chat(chat, start_ts=md.get("release_timestamp")
                                       or md.get("timestamp") or 0)
                with open(parsed, "w") as f:
                    for msg in msgs:
                        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        if not os.path.exists(vtt):
            s = sh([YTDLP, "--skip-download", "--write-auto-subs", "--sub-langs", LANG,
                    "--sub-format", "vtt", "-o", f"{OUT}/vtt/{vid}.%(ext)s",
                    f"https://www.youtube.com/watch?v={vid}"])
            if not os.path.exists(vtt):
                print(f"{vid}: no subs (declared)", flush=True)
                continue
            time.sleep(1.0)
        elif md.get("stt") and not md.get("stt_check"):
            # CROSS-CHECK: a locally-transcribed video gets YouTube's caption
            # compared against ours if/when it appears — an AGREEMENT tripwire, not ground truth
            # (YT's auto-captions are ASR too). Ours stays canonical either way; a low score is a
            # flag to look, never a silent swap. YT captions that haven't appeared within ~7 days
            # of the attempt never do (33/405 measured) — stamped and stopped, not retried forever.
            _ytv = f"{OUT}/vtt/{vid}.yt.vtt"
            sh([YTDLP, "--skip-download", "--write-auto-subs", "--sub-langs", LANG,
                "--sub-format", "vtt", "-o", f"{OUT}/vtt/{vid}.yt.%(ext)s",
                f"https://www.youtube.com/watch?v={vid}"])
            _ytraw = f"{OUT}/vtt/{vid}.yt.{LANG}.vtt"
            if os.path.exists(_ytraw):
                os.replace(_ytraw, _ytv)
            if os.path.exists(_ytv):
                def _bag(p):
                    import collections as _c
                    return _c.Counter(w for _t, tx in parse_vtt(p)
                                      for w in re.findall(r"[a-zàèéìòù]{4,}", tx.lower()))
                a, b = _bag(vtt), _bag(_ytv)
                inter = sum((a & b).values())
                union = sum((a | b).values())
                agree = inter / union if union else 0.0
                md["stt_check"] = {"agreement": round(agree, 3),
                                   "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                _mt = f"{meta_f}.tmp.{os.getpid()}"      # atomic, like every
                json.dump(md, open(_mt, "w"))            # other cross-run store in this lane
                os.replace(_mt, meta_f)
                print(f"{vid}: stt-check agreement {agree:.0%}"
                      + ("" if agree >= 0.55 else " ⚠ LOW — review the local transcript"),
                      flush=True)
            else:
                _age_d = (time.time() - float(md.get("timestamp") or time.time())) / 86400
                if _age_d > 7:
                    md["stt_check"] = {"agreement": None, "note": "yt-never-captioned"}
                    _mt = f"{meta_f}.tmp.{os.getpid()}"
                    json.dump(md, open(_mt, "w"))
                    os.replace(_mt, meta_f)
        lines = parse_vtt(vtt)
        start = md.get("release_timestamp") or md.get("timestamp") or 0
        calls, ind_hits = [], {k: 0 for k in IND}
        for t_rel, txt in lines:
            for name, pat in IND.items():
                if pat.search(txt):
                    ind_hits[name] += 1
            if CALL_PAT.search(txt):
                calls.append({"t_rel_s": round(t_rel, 1),
                              "t_utc": int(start + t_rel) if start else None, "text": txt})
        with open(f"{OUT}/calls/{vid}.jsonl", "w") as f:
            for c in calls:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        indicators[vid] = ind_hits
        n_chat = n_auth = 0
        _cp = f"{OUT}/chat/{vid}.jsonl"
        if os.path.exists(_cp):
            _cm = [json.loads(x) for x in open(_cp, errors="ignore") if x.strip()]
            n_chat, n_auth = len(_cm), len({c.get("author") for c in _cm})
        index.append({"id": vid, "kind": kind.get(vid, "live"),
                      "n_chat_msgs": n_chat, "n_chat_authors": n_auth,
                      "date_utc": md.get("upload_date"), "start_ts": start,
                      "duration_s": md.get("duration"), "title": (md.get("title") or "")[:80],
                      "n_call_lines": len(calls),
                      "indicator_hits": sum(ind_hits.values())})
        if k % 10 == 9:
            print(f"{k+1}/{len(ids)} archived", flush=True)

    # MERGE, never overwrite. Writing only the rows processed in THIS run would destroy the
    # index for every video a bounded run did not touch. The transcripts on disk are never at
    # risk, but the index IS what every downstream reader greps, so losing it
    # loses the archive in every practical sense. Now the run's rows are merged over what exists.
    COLS = ["id", "kind", "date_utc", "start_ts", "duration_s", "title",
            "n_call_lines", "indicator_hits", "n_chat_msgs", "n_chat_authors"]
    merged: dict[str, dict] = {}
    if os.path.exists(f"{OUT}/index.csv"):
        with open(f"{OUT}/index.csv", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("id"):
                    merged[row["id"]] = {c: row.get(c, "") for c in COLS}
    for row in index:
        merged[row["id"]] = {c: row.get(c, "") for c in COLS}
    with open(f"{OUT}/index.csv.tmp", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows([merged[k] for k in sorted(merged, key=lambda i: str(merged[i]["date_utc"]))])
    os.replace(f"{OUT}/index.csv.tmp", f"{OUT}/index.csv")     # atomic
    prev = {}
    if os.path.exists(f"{OUT}/indicators.json"):
        try:
            prev = (json.load(open(f"{OUT}/indicators.json")) or {}).get("per_video", {})
        except ValueError:
            prev = {}
    prev.update(indicators)
    indicators = prev
    tot = {k: sum(v.get(k, 0) for v in indicators.values()) for k in IND} if indicators else {}
    json.dump({"per_video": indicators, "total": tot}, open(f"{OUT}/indicators.json", "w"))
    print(f"DONE: {len(index)} lives archived · call-lines {sum(i['n_call_lines'] for i in index)}"
          f" · indicator totals {tot}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
