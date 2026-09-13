"""Where every channel-specific choice lives: one JSON file, no dependencies.

Each step of the pipeline imports `S` from here instead of hard-coding a channel, a topic list
or a rubric. Copy config.example.json to config.json and edit that; nothing else needs touching.

Resolution order: $CHANNEL_MINER_CONFIG, then ./config.json, then config.example.json.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent


def _load() -> dict:
    for cand in (os.getenv("CHANNEL_MINER_CONFIG"),
                 _HERE / "config.json",
                 _HERE / "config.example.json"):
        if cand and Path(cand).exists():
            with open(cand, encoding="utf-8") as f:
                return json.load(f)
    raise SystemExit("channel-miner: no config found — copy config.example.json to config.json")


class _Settings:
    def __init__(self, d: dict):
        self._d = d
        self.channel: str = d["channel"].rstrip("/")
        self.language: str | None = d.get("language") or None
        self.data_dir: str = str(_HERE / d.get("data_dir", "data"))
        # A first run has no data directory yet and every step writes into it, so make it here
        # rather than making each step remember to.
        for sub in ("", "vtt", "chat"):
            os.makedirs(os.path.join(self.data_dir, sub), exist_ok=True)
        self.ollama: str = d.get("ollama_url", "http://localhost:11434").rstrip("/")
        self.model: str = d.get("model", "qwen3.5:9b")
        self.stt_model: str = d.get("stt_model", "large-v3-turbo")
        self.rubric: str = (d.get("rubric") or "").strip()
        self.rubric_name: str = d.get("rubric_name", "the rules")
        self.max_videos_per_run: int = int(d.get("max_videos_per_run", 8))
        self.max_reads_per_run: int = int(d.get("max_reads_per_run", 3))
        # topics: {name: regex} — what the indexer tags, and what novelty is measured against.
        self.topics: dict[str, re.Pattern] = {
            k: re.compile(v, re.I) for k, v in (d.get("topics") or {}).items()
        }

    @property
    def lang_suffix(self) -> str:
        """Caption language: from the config, else detected from the channel itself.

        Detection asks yt-dlp what captions the newest video actually carries, and the answer is
        cached in the data directory — a channel does not change language between runs, and a
        network round trip per module import would be absurd.
        """
        if self.language:
            return self.language.split("-")[0]
        cache = os.path.join(self.data_dir, ".detected-language")
        if os.path.exists(cache):
            got = open(cache, encoding="utf-8").read().strip()
            if got:
                return got
        got = self._detect_language()
        with open(cache, "w", encoding="utf-8") as fh:
            fh.write(got)
        return got

    def _detect_language(self) -> str:
        """Newest video's caption language, or 'en' when nothing can be learned."""
        import subprocess
        ytdlp = os.path.expanduser("~/.local/bin/yt-dlp")
        if not os.path.exists(ytdlp):
            ytdlp = "yt-dlp"
        for tab in ("videos", "streams"):
            try:
                r = subprocess.run([ytdlp, "--flat-playlist", "--print", "id", "--playlist-end", "1",
                                    f"{self.channel}/{tab}"], capture_output=True, text=True, timeout=180)
                vid = (r.stdout or "").strip().splitlines()
                if not vid:
                    continue
                r = subprocess.run([ytdlp, "--skip-download", "-J",
                                    f"https://www.youtube.com/watch?v={vid[0]}"],
                                   capture_output=True, text=True, timeout=180)
                meta = json.loads(r.stdout or "{}")
                for key in ("language",):
                    if meta.get(key):
                        return str(meta[key]).split("-")[0]
                for src in (meta.get("automatic_captions") or {}, meta.get("subtitles") or {}):
                    codes = [c for c in src if c and "-" not in c and c != "live_chat"]
                    if codes:
                        return sorted(codes)[0]
            except Exception:
                continue
        return "en"

    @property
    def has_topics(self) -> bool:
        return bool(self.topics)

    @property
    def has_rubric(self) -> bool:
        return bool(self.rubric)

    @property
    def tabs(self) -> list[tuple[str, str]]:
        """(url, kind) for each channel tab worth walking."""
        return [(f"{self.channel}/streams", "live"),
                (f"{self.channel}/videos", "upload"),
                (f"{self.channel}/shorts", "short")]

    def stopwords(self, fallback: str) -> frozenset[str]:
        """Filler words to ignore when hunting new concepts.

        Tolerant on purpose: the key may be absent, null, a space-separated string, or a list.
        Anything empty means "use the fallback the caller ships".
        """
        raw = self._d.get("stopwords") or fallback
        return frozenset(raw.split() if isinstance(raw, str) else raw)

    def get(self, key, default=None):
        """Like dict.get, but a key present-but-null also yields the default."""
        value = self._d.get(key)
        return default if value is None else value


S = _Settings(_load())
