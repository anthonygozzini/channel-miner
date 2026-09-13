#!/usr/bin/env python3
"""Transcribe any single video or audio file, locally, on the GPU. Nothing leaves the machine.

    python3 transcribe.py "https://www.youtube.com/watch?v=XXXX"
    python3 transcribe.py ~/Videos/clip.mp4
    python3 transcribe.py <link-or-file> --lang en        # default: auto-detect
    python3 transcribe.py <link-or-file> --out ~/Desktop  # default: alongside the input

Writes two files: a .txt (plain text) and a .vtt (with timestamps).

Standalone - the pipeline in miner/ does this in bulk; this is the one-off tool. It handles the
JS challenge YouTube now puts in front of media URLs, which returns HTTP 403 without a runtime.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

VENV_SITE = os.path.expanduser(
    os.getenv("WHISPER_VENV_SITE", "~/whisper-venv/lib/python3.12/site-packages"))
YTDLP = os.path.expanduser("~/.local/bin/yt-dlp")
DENO = os.path.expanduser("~/.local/bin/deno")
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4a", ".mp3", ".wav", ".flac", ".ogg",
             ".opus", ".aac", ".wma", ".m4v", ".mpg", ".mpeg", ".ts", ".3gp"}


def _cuda_paths() -> None:
    libs = [p for p in (os.path.join(VENV_SITE, x)
                        for x in ("nvidia/cublas/lib", "nvidia/cudnn/lib")) if os.path.isdir(p)]
    if libs:
        os.environ["LD_LIBRARY_PATH"] = ":".join(libs + [os.environ.get("LD_LIBRARY_PATH", "")])


def _ts(s: float) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}.{int((s % 1) * 1000):03d}"


def fetch(src: str, workdir: str) -> tuple[str, str] | None:
    """(audio path, base name) for a URL or a local file; None on failure."""
    if os.path.exists(src):
        ext = os.path.splitext(src)[1].lower()
        if ext not in MEDIA_EXT:
            print(f"stt: '{os.path.basename(src)}' is not a video/audio file "
                  f"({ext or 'no extension'}). Supported: "
                  f"{', '.join(sorted(e.lstrip('.') for e in MEDIA_EXT))}", file=sys.stderr)
            return None
        return src, os.path.splitext(os.path.basename(src))[0]
    if not src.lower().startswith(("http://", "https://")):
        print(f"stt: '{src}' is neither an existing file nor a http(s) link", file=sys.stderr)
        return None
    if not os.path.exists(YTDLP):
        print(f"stt: yt-dlp not found at {YTDLP}", file=sys.stderr)
        return None
    # name the JS runtime explicitly: yt-dlp enables only deno, and finds it via PATH
    js = ["--js-runtimes", f"deno:{DENO}"] if os.path.exists(DENO) else []
    out = os.path.join(workdir, "audio.%(ext)s")
    print("stt: downloading audio…", flush=True)
    r = subprocess.run([YTDLP, *js, "-f", "bestaudio[ext=m4a]/bestaudio",
                        "--print", "after_move:%(title)s", "-o", out, src],
                       capture_output=True, text=True)
    got = [os.path.join(workdir, f) for f in os.listdir(workdir)]
    if r.returncode != 0 or not got:
        err = (r.stderr or "").strip()
        if "403" in err:
            err = ("HTTP 403 — YouTube's JS challenge. Update yt-dlp "
                   "(python3 -m pip install --user --upgrade --break-system-packages yt-dlp) "
                   "and make sure ~/.local/bin/deno exists.")
        print(f"stt: download failed. {err[-300:]}", file=sys.stderr)
        return None
    title = (r.stdout or "").strip().splitlines()
    name = title[-1] if title else "transcript"
    return got[0], "".join(c for c in name if c not in '/\\:*?"<>|').strip()[:120] or "transcript"


def main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe any video/audio locally on the GPU.")
    ap.add_argument("source", help="a http(s) link or a path to a local media file")
    ap.add_argument("--lang", default=None, help="language code (it, en, …); default: auto-detect")
    ap.add_argument("--out", default=None, help="output directory (default: next to the input)")
    ap.add_argument("--model", default=os.getenv("STT_MODEL", "large-v3-turbo"))
    a = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="stt-") as tmp:
        got = fetch(a.source, tmp)
        if got is None:
            return 1
        audio, base = got
        outdir = a.out or (os.path.dirname(os.path.abspath(audio))
                           if os.path.exists(a.source) else os.getcwd())
        os.makedirs(outdir, exist_ok=True)

        _cuda_paths()
        sys.path.insert(0, VENV_SITE)
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            print(f"stt: the speech-to-text engine is not installed.\n"
                  f"     Expected it at: {VENV_SITE}\n"
                  f"     Run ./setup.sh to create it, or point WHISPER_VENV_SITE at an existing\n"
                  f"     virtualenv that has faster-whisper.", file=sys.stderr)
            return 1
        try:
            model = WhisperModel(a.model, device="cuda", compute_type="int8_float16",
                                 download_root=os.path.expanduser("~/.cache/whisper"))
            where = "GPU"
        except Exception as exc:  # noqa: BLE001 — no GPU/driver: CPU still works, just slower
            print(f"stt: GPU unavailable ({str(exc)[:80]}) — falling back to CPU", flush=True)
            model = WhisperModel(a.model, device="cpu", compute_type="int8",
                                 download_root=os.path.expanduser("~/.cache/whisper"))
            where = "CPU"
        print(f"stt: {a.model} on {where}, transcribing…", flush=True)

        try:
            segments, info = model.transcribe(audio, language=a.lang, vad_filter=True,
                                              beam_size=1)
            lang = getattr(info, "language", a.lang or "?")
            _first = list(segments)          # force the decode here so failures land in THIS try
            segments = _first
        except Exception as exc:  # noqa: BLE001 — a plain sentence beats a stack trace
            print(f"stt: could not read any audio from this file ({type(exc).__name__}). "
                  f"Is it really a video or audio recording?", file=sys.stderr)
            return 1
        vtt = ["WEBVTT", "Kind: captions", f"Language: {lang}", ""]
        text: list[str] = []
        for seg in segments:
            s = seg.text.strip()
            if not s:
                continue
            vtt += [f"{_ts(seg.start)} --> {_ts(seg.end)}", s, ""]
            text.append(s)
        if not text:
            print("stt: no speech found — nothing written", file=sys.stderr)
            return 1

        p_txt = os.path.join(outdir, base + ".txt")
        p_vtt = os.path.join(outdir, base + ".vtt")
        open(p_txt, "w", encoding="utf-8").write("\n".join(text) + "\n")
        open(p_vtt, "w", encoding="utf-8").write("\n".join(vtt))
        mins = (getattr(info, "duration", 0) or 0) / 60
        print(f"stt: done — {len(text)} segments, {mins:.0f} min, language '{lang}'")
        print(f"  {p_txt}")
        print(f"  {p_vtt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
