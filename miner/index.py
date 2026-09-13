#!/usr/bin/env python3
"""Step 3 - turn raw transcripts into a searchable, tagged corpus.

Cuts every transcript into segments of roughly forty seconds - long enough to hold one whole
thought, short enough to stay on a single topic - and tags each segment with the topics from
config.json. Boilerplate (greetings, chat banter, stream intros) is dropped.

    python3 miner/index.py

Writes segments.jsonl: {id, kind, date, t_rel_s, tags, text}, plus a census of how many
transcripts exist versus how many are still missing.
"""
from __future__ import annotations

import csv
import glob
import importlib.util
import json
import os
import re

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from settings import S  # noqa: E402

OUT = S.data_dir
LANG = S.lang_suffix
SEG_SECONDS = 40.0            # long enough to hold a whole thought, short enough to stay on topic
MIN_CHARS, MAX_CHARS = 120, 900

# The concepts he actually trades, in HIS words. Counts across the corpus decided this list:
TAGS: dict[str, re.Pattern] = S.topics   # from config.json
# The transcript language's own function words, used ONLY to spot segments that are not speech
# in that language - Whisper renders pre-stream music as song lyrics, often in English. Comes
# from config (`function_words`); the default is English.
_DEFAULT_FUNC = ("the a an and or but of to in on for with is are was were that this it as at "
                 "by from not be have has had you we they he she i do does did will would can")
_FUNC = S.stopwords(_DEFAULT_FUNC) if not S.get("function_words") else frozenset(
    S.get("function_words").split() if isinstance(S.get("function_words"), str)
    else S.get("function_words"))
# Channel boilerplate to drop: intros, sign-offs, subscribe pleas. Comes from config
# (`boilerplate`, one regex); the default catches the common English forms.
NOISE = re.compile(S.get("boilerplate",
                         r"subscribe|smash that|link in the description|patreon|sponsor|"
                         r"welcome back|like and comment|join the discord"), re.I)


def load_parser():
    spec = importlib.util.spec_from_file_location(
        "arch", os.path.join(os.path.dirname(__file__), "archive.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def segment(lines: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """Collapse cue lines into ~SEG_SECONDS windows of continuous speech."""
    out, buf, t0 = [], [], None
    for t, txt in lines:
        if t0 is None:
            t0 = t
        buf.append(txt)
        if t - t0 >= SEG_SECONDS:
            s = re.sub(r"\s+", " ", " ".join(buf)).strip()
            if MIN_CHARS <= len(s):
                out.append((t0, s[:MAX_CHARS]))
            buf, t0 = [], None
    if buf and t0 is not None:
        s = re.sub(r"\s+", " ", " ".join(buf)).strip()
        if MIN_CHARS <= len(s):
            out.append((t0, s[:MAX_CHARS]))
    return out


def main() -> int:
    m = load_parser()
    meta = {}
    if os.path.exists(f"{OUT}/index.csv"):
        meta = {r["id"]: r for r in csv.DictReader(open(f"{OUT}/index.csv"))}
    rows, skipped = [], 0
    for vtt in sorted(glob.glob(f"{OUT}/vtt/*.{LANG}.vtt")):
        vid = os.path.basename(vtt).replace(f".{LANG}.vtt", "")
        if vid.endswith(".yt"):        # a comparison temp file, not a video: an interrupted
            continue                   # caption check can leave {vid}.yt.<lang>.vtt behind
        info = meta.get(vid, {})
        for t_rel, text in segment(m.parse_vtt(vtt)):
            if NOISE.search(text) and len(text) < 300:
                skipped += 1
                continue                       # channel boilerplate, not method
            # Whisper transcribes pre-stream MUSIC as song lyrics, usually in another
            # language. A long segment carrying almost none of the transcript language's
            # function words is lyrics, not speech.
            _words = text.lower().split()
            if len(_words) >= 15 and sum(w in _FUNC for w in _words) < 2:
                skipped += 1
                continue                       # not speech in the expected language: music/lyrics
            tags = sorted(k for k, p in TAGS.items() if p.search(text))
            # With no topics configured there is nothing to tag against, so tagging cannot be a
            # filter: an empty topic list must keep the corpus, not silently discard all of it.
            if not tags and S.has_topics:
                skipped += 1
                continue                       # a segment about nothing we can situate is dead weight
            rows.append({"id": vid, "kind": info.get("kind", "?"),
                         "date": info.get("date_utc", ""), "t_rel_s": round(t_rel, 1),
                         "tags": tags, "text": text})
    tmp = f"{OUT}/segments.jsonl.tmp"
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, f"{OUT}/segments.jsonl")
    import collections
    tc = collections.Counter(t for r in rows for t in r["tags"])
    print(f"segments: {len(rows)} kept, {skipped} dropped (boilerplate/untagged)", flush=True)
    print(f"tag coverage: {dict(tc.most_common())}", flush=True)
    # TRANSCRIPT CENSUS, ALL-TIME. A per-run "no subs" line names
    # only the newest ~8 per kind, and that WINDOW was reported as the WHOLE — 33 all-time
    # missing read as "4-6 recent". The full denominator prints on every run, forever.
    _metas = glob.glob(f"{OUT}/vtt/*.meta.json")
    _miss = [os.path.basename(p)[:-10] for p in _metas
             if not os.path.exists(f"{OUT}/vtt/{os.path.basename(p)[:-10]}.{LANG}.vtt")]
    print(f"transcript census: {len(_metas) - len(_miss)}/{len(_metas)} transcribed · "
          f"{len(_miss)} still missing (retried every run)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
