#!/usr/bin/env python3
"""Step 4 - find the concepts you have no word for yet.

The indexer can only find topics whose vocabulary you already wrote down, which is circular: an
idea explained in words you never listed is invisible to it. This step looks the other way
round. It counts phrase families across the whole corpus and reports those that recur often
enough to be a real, repeated idea while matching none of your topics.

The bar is relative, not absolute: a family must clear a fraction of your own smallest topic, so
it scales with the corpus instead of resting on a magic number.

    python3 miner/novelty.py            # full table to stdout
    python3 miner/novelty.py --watch    # one line for a log; only new candidates

Promotion stays yours: a candidate becomes a topic when you add it to config.json.
"""
from __future__ import annotations

import collections
import importlib.util
import glob
import json
import os
import re
import sys
import unicodedata

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from settings import S  # noqa: E402

OUT = S.data_dir
LANG = S.lang_suffix
STATE = f"{OUT}/novelty_state.json"
NOVELTY_BAR_FRAC = 0.25       # of your smallest topic's segment count - a relative bar
VERBATIM_FRAC = 0.6           # >60% of an n-gram's occurrences share one context window = boilerplate
TOP_N = 25

_here = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_here, f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Italian function words + the stream's own generic chatter. Chatter words are FILLER, not concept
# vocabulary — "vediamo", "ragazzi", "praticamente" appear in every window about everything, so
# they can only ever produce false families.
_DEFAULT_STOP = """
the a an and or but if then than that this these those there here it its is are was were
be been being have has had do does did will would can could should may might must
i you he she we they me him her us them my your his our their what which who whom
when where why how all any both each few more most other some such no nor not only own
same so too very just about into over after before again once out up down off on in
yeah okay ok right like really actually basically literally obviously anyway well
thing things stuff kind sort lot lots bit guys everyone today now going get got go
know think want see look say said tell make made take even still back way time
"""

# Filler words are not concept vocabulary: they appear in every window about everything, so
# they can only ever produce false families. The list lives in config.json (`stopwords`) —
# it is language-specific, and the default set below is only a fallback for English.
STOP = S.stopwords(_DEFAULT_STOP)
# Two dilution classes are worth stopping. (1) ELISION STUBS: the tokenizer splits on the
# apostrophe, so an elided preposition leaves a stub that seeds a fake family for every word it
# precedes. (2) FILLER: speech mechanics ("I mean", "you know", "let's see") appear in every
# window about everything, so they can only ever produce false families.
# Domain words are deliberately NOT stopped — junk is removed, signal never is.

WORD = re.compile(r"[a-zàèéìòù]{3,}", re.I)


def _norm(w: str) -> str:
    return unicodedata.normalize("NFC", w.lower())


def mine(idx, arch):
    tag_pats = list(idx.TAGS.values()) + [idx.NOISE]
    grams: dict[tuple, dict] = {}
    videos = sorted(v for v in glob.glob(f"{OUT}/vtt/*.{LANG}.vtt")
                    if not os.path.basename(v).replace(f".{LANG}.vtt", "").endswith(".yt"))
    n_seg = 0
    for vtt in videos:
        vid = os.path.basename(vtt).replace(f".{LANG}.vtt", "")
        for _t, text in idx.segment(arch.parse_vtt(vtt)):
            n_seg += 1
            text = re.sub(r"\[[^\]]{1,20}\]", " ", text)   # ASR markers: [musica], [applausi]
            toks = [_norm(w) for w in WORD.findall(text)]
            keep = [w for w in toks if w not in STOP
                    and not any(p.search(w) for p in tag_pats)]
            seen_here = set()
            for n in (1, 2):
                for i in range(len(keep) - n + 1):
                    g = tuple(keep[i:i + n])
                    if n == 2 and g[0] == g[1]:
                        continue
                    if g in seen_here:
                        continue
                    seen_here.add(g)
                    e = grams.setdefault(g, {"freq": 0, "vids": set(), "ctx": collections.Counter(),
                                             "ex": []})
                    e["freq"] += 1
                    e["vids"].add(vid)
                    j = text.lower().find(g[0])
                    e["ctx"][text.lower()[max(0, j - 20):j + 40]] += 1
                    if len(e["ex"]) < 3:
                        e["ex"].append(text[:180])
    return grams, n_seg


def families(grams, bar: int, n_videos: int):
    """A CONCEPT is bursty and partial: taught repeatedly WITHIN the streams that teach it
    (freq/containing-video >= BURST) while absent from most others (document-frequency <= DF_MAX) —
    ubiquitous words ('mercato', 'oggi') fail the ceiling, one-off episodes fail the floor, and
    channel boilerplate fails the verbatim test. All three cuts are in the corpus's own units."""
    DF_MAX, BURST = 0.3, 2.5
    rows = []
    for g, e in grams.items():
        spread = len(e["vids"])
        if spread < 5 or spread > DF_MAX * n_videos:
            continue
        if e["freq"] / spread < BURST:
            continue
        top_ctx = e["ctx"].most_common(1)[0][1] if e["ctx"] else 0
        if e["freq"] >= 5 and top_ctx / e["freq"] > VERBATIM_FRAC:
            continue                                    # verbatim-repeat = boilerplate, not teaching
        rows.append({"gram": " ".join(g), "freq": e["freq"], "videos": spread,
                     "candidate": e["freq"] >= bar, "examples": e["ex"]})
    rows.sort(key=lambda r: (-r["freq"], -r["videos"]))
    # a bigram's words each also count as unigrams; keep the bigram and drop its parts when the
    # bigram carries most of the parent's mass (the family is the PHRASE, not the word)
    bigs = {r["gram"] for r in rows if " " in r["gram"]}
    parts = {w for b in bigs for w in b.split()}
    out, dropped_parts = [], set()
    for r in rows:
        if " " not in r["gram"] and r["gram"] in parts:
            parent = next((b for b in bigs if r["gram"] in b.split()), None)
            pf = next((x["freq"] for x in rows if x["gram"] == parent), 0)
            if parent and pf >= 0.6 * r["freq"]:
                dropped_parts.add(r["gram"])
                continue
        out.append(r)
    return out


def smallest_topic() -> tuple[str, int]:
    """The least-represented topic in the corpus — the unit the novelty bar is expressed in.

    An unindexed or untagged corpus has no smallest topic; the fallback keeps the bar at the
    floor so the first run reports honestly instead of dividing by nothing.
    """
    counts = collections.Counter()
    try:
        for line in open(f"{OUT}/segments.jsonl"):
            for t in json.loads(line).get("tags", []):
                counts[t] += 1
    except FileNotFoundError:
        return ("none yet", 0)
    if not counts:
        return ("none yet", 0)
    name, n = min(counts.items(), key=lambda kv: kv[1])
    return name, n


def main() -> int:
    watch = "--watch" in sys.argv
    idx, arch = _load("index"), _load("archive")
    small_name, small_n = smallest_topic()
    bar = max(10, int(small_n * NOVELTY_BAR_FRAC))
    grams, n_seg = mine(idx, arch)
    n_videos = len([v for v in glob.glob(f"{OUT}/vtt/*.{LANG}.vtt")      # .yt is a caption-check temp,
                    if not os.path.basename(v)[:-7].endswith(".yt")])  # never a real video
    rows = families(grams, bar, n_videos)
    cands = [r for r in rows if r["candidate"]]
    if watch:
        st = {"known": []}
        if os.path.exists(STATE):
            st = json.load(open(STATE))
        fresh = [r for r in cands if r["gram"] not in st["known"]]
        print(f"novelty: {len(cands)} families over the bar ({bar} seg = "
              f"{NOVELTY_BAR_FRAC:.0%} of smallest topic '{small_name}' {small_n}), "
              f"{len(fresh)} NEW: " + ", ".join(r["gram"] for r in fresh[:5]), flush=True)
        st["known"] = sorted({*st["known"], *[r["gram"] for r in cands]})
        tmp = STATE + ".tmp"
        json.dump(st, open(tmp, "w"))
        os.replace(tmp, STATE)
        return 0
    print(f"mined {n_seg} segments · bar = {bar} segments "
          f"({NOVELTY_BAR_FRAC:.0%} of smallest topic '{small_name}' = {small_n})")
    print(f"{'PHRASE':28s} {'FREQ':>5s} {'VIDS':>4s}  CANDIDATE")
    for r in rows[:TOP_N]:
        print(f"{r['gram']:28s} {r['freq']:5d} {r['videos']:4d}  {'<< OVER BAR' if r['candidate'] else ''}")
        if r["candidate"]:
            for ex in r["examples"][:2]:
                print(f"    e.g. {ex}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
