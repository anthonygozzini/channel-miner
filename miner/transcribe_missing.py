#!/usr/bin/env python3
"""Step 2 - transcribe the videos that have no captions.

Some videos never get auto-captioned and never will. This transcribes them locally with Whisper
on the GPU, one at a time, and writes a normal .vtt the rest of the pipeline reads unchanged.
The result is stamped in the video's metadata so a local transcript is never mistaken for a
YouTube caption.

Deliberately bounded: one video at a time, a lock file forbids two instances, and the process
exits when it is done. It is never a daemon and never starts at boot.

    python3 miner/transcribe_missing.py --limit 2      # a scheduled run
    python3 miner/transcribe_missing.py --limit 40     # a full backfill (one long night)

Needs faster-whisper in a virtualenv; set `whisper_venv_site` in config.json.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import subprocess
import sys
import time

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from settings import S  # noqa: E402

OUT = S.data_dir
LANG = S.lang_suffix
VENV_SITE = os.path.expanduser(
    S.get("whisper_venv_site", "~/whisper-venv/lib/python3.12/site-packages"))
LOCK = f"{OUT}/stt.lock"
MODEL = S.stt_model
YTDLP = os.path.expanduser("~/.local/bin/yt-dlp")
# YouTube serves media URLs behind a JS ("nsig") challenge — without a JS runtime
# every fetch returns HTTP 403 and the reading pass goes silently blind (5 lives missed). yt-dlp
# enables ONLY deno by default and discovers it via PATH, which cron does not carry. Name the
# binary explicitly so the job never depends on the caller's environment.
DENO = os.path.expanduser("~/.local/bin/deno")
_JS_ARGS = ["--js-runtimes", f"deno:{DENO}"] if os.path.exists(DENO) else []


def _cuda_paths() -> None:
    """ctranslate2's pip wheel needs the pip-installed CUDA libs on the loader path."""
    libs = []
    for pkg in ("nvidia/cublas/lib", "nvidia/cudnn/lib"):
        p = os.path.join(VENV_SITE, pkg)
        if os.path.isdir(p):
            libs.append(p)
    if libs:
        os.environ["LD_LIBRARY_PATH"] = ":".join(libs + [os.environ.get("LD_LIBRARY_PATH", "")])


def missing_videos() -> list[tuple[str, str]]:
    """(vid, upload_date) for every attempted video with no transcript, newest first."""
    out = []
    for p in glob.glob(f"{OUT}/vtt/*.meta.json"):
        vid = os.path.basename(p)[:-10]
        if os.path.exists(f"{OUT}/vtt/{vid}.{LANG}.vtt"):
            continue
        try:
            d = str(json.load(open(p)).get("upload_date") or "")
        except Exception:  # noqa: BLE001
            d = ""
        out.append((vid, d))
    return sorted(out, key=lambda x: x[1], reverse=True)


def _sec_ts(s: float) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}.{int((s % 1) * 1000):03d}"


def transcribe(vid: str, model) -> bool:
    audio = f"{OUT}/vtt/{vid}.stt.m4a"
    vtt = f"{OUT}/vtt/{vid}.{LANG}.vtt"
    r = subprocess.run([YTDLP, *_JS_ARGS, "-f", "bestaudio[ext=m4a]/bestaudio", "-o", audio,
                        f"https://www.youtube.com/watch?v={vid}"],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not os.path.exists(audio):
        # the 403 face of a stale yt-dlp / missing JS runtime is silent starvation — say which
        _err = (r.stderr or "")
        _why = ("403: yt-dlp too old or no JS runtime — update yt-dlp and install deno"
                if "403" in _err else _err[-120:])
        print(f"{vid}: audio download FAILED ({_why})", flush=True)
        return False
    try:
        t0 = time.time()
        segments, info = model.transcribe(audio, language=LANG, vad_filter=True, beam_size=1)
        lines = ["WEBVTT", "Kind: captions", "Language: it",
                 f"NOTE source=local-whisper-{MODEL} (YouTube never captioned this)", ""]
        n = 0
        for seg in segments:
            txt = seg.text.strip()
            if not txt:
                continue
            lines.append(f"{_sec_ts(seg.start)} --> {_sec_ts(seg.end)}")
            lines.append(txt)
            lines.append("")
            n += 1
        if n < 5:
            print(f"{vid}: transcription produced only {n} segments — NOT saved", flush=True)
            return False
        tmp = vtt + ".tmp"
        open(tmp, "w", encoding="utf-8").write("\n".join(lines))
        os.replace(tmp, vtt)
        meta_f = f"{OUT}/vtt/{vid}.meta.json"
        try:
            md = json.load(open(meta_f))
            md["stt"] = f"local-whisper-{MODEL}"
            # truncate-in-place here + the watcher's SIGTERM (which the R3
            # handler converts to KeyboardInterrupt, sailing PAST `except Exception`) = a torn
            # meta; the archive's recovery then deletes it and refetches WITHOUT the stt stamp —
            # provenance lost forever, stt_check never runs. Atomic temp+replace.
            _mtmp = f"{meta_f}.tmp.{os.getpid()}"
            json.dump(md, open(_mtmp, "w"))
            os.replace(_mtmp, meta_f)
        except Exception:  # noqa: BLE001
            pass
        dur = getattr(info, "duration", 0) or 0
        print(f"{vid}: transcribed {dur/60:.0f}min audio in {(time.time()-t0)/60:.1f}min "
              f"({n} segments)", flush=True)
        return True
    finally:
        try:
            os.remove(audio)
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=2)
    a = ap.parse_args()
    # SIGTERM (a scheduler timeout) skips every finally — that left a stale lock for
    # 12h + orphaned 100-200MB audio files. Convert it to an exception so finally runs.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    todo = missing_videos()
    if not todo:
        print("stt: nothing missing — every attempted video has a transcript")
        return 0

    def _acquire() -> bool:
        try:
            _fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(_fd, str(os.getpid()).encode())
            os.close(_fd)
            return True
        except FileExistsError:
            return False
    # Atomic acquire. Checking existence and then creating is a race: a scheduled run and a
    # manual one can both pass the check and put two Whisper instances on one GPU. Taking over a
    # stale lock has the same hazard, so a takeover must RE-ACQUIRE atomically rather than just
    # moving the old lock out of the way.
    if not _acquire():
        if time.time() - os.path.getmtime(LOCK) < 12 * 3600:
            print("stt: another instance holds the lock — exiting (never two at once)")
            return 0
        try:
            os.replace(LOCK, LOCK + ".stale")   # stale lock: keep the evidence...
        except FileNotFoundError:                # another taker won this rename
            print("stt: lost the stale-lock takeover race — exiting (never two at once)")
            return 0
        if not _acquire():                       # ...and take over only by WINNING the race
            print("stt: lost the stale-lock takeover race — exiting (never two at once)")
            return 0
    try:
        # The orphan sweep runs INSIDE the lock: outside it, a second invocation would delete
        # the holder's in-flight audio. Only the lock holder may clean up.
        for _orph in glob.glob(f"{OUT}/vtt/*.stt.m4a") + glob.glob(f"{OUT}/vtt/*.stt.m4a.part"):
            try:
                os.remove(_orph)                # previous runs' orphaned audio (+ yt-dlp .part)
            except OSError:
                pass
        _cuda_paths()
        sys.path.insert(0, VENV_SITE)
        from faster_whisper import WhisperModel
        try:
            model = WhisperModel(MODEL, device="cuda", compute_type="int8_float16",
                                 cpu_threads=2)
            print(f"stt: {MODEL} on GPU", flush=True)
        except Exception as e:  # noqa: BLE001 — GPU busy/absent -> honest small-CPU fallback
            print(f"stt: GPU unavailable ({e}) — small model on 4 CPU threads", flush=True)
            model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=4)
        done = 0
        # newest-first + limit 2 + no failure memory = two permanently
        # untranscribable videos (members-only, geo-blocked) starve every older missing one
        # FOREVER. Rotate the start index by the 4h run slot: every missing video gets
        # attempts within a day, and transient failures still retry soon.
        if len(todo) > a.limit:
            _off = int(time.time() // 14400) % len(todo)
            todo = todo[_off:] + todo[:_off]
        for vid, d in todo[:a.limit]:
            try:
                os.utime(LOCK, None)            # a --limit 40 backfill runs
            except OSError:                     # 10-13h and crossed its own 12h staleness bar —
                pass                            # touch per video: a LIVE run never reads stale
            if transcribe(vid, model):
                done += 1
        print(f"stt: {done}/{min(a.limit, len(todo))} transcribed · "
              f"{len(todo) - done} still missing", flush=True)
        return 0
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
