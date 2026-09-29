#!/usr/bin/env python3
"""Turn storyboard images into narrated stories (CLI + Tkinter GUI).

Pipeline per storyboard image:
    1. Writer (OpenRouter vision chat model) reads the storyboard panels in
       order (left -> right, top -> bottom), writes one scene per panel with
       transitions between scenes, a short title, and picks the most fitting
       narrator among up to 5 Fish Audio voices (voice ids).
    2. Narrator (Fish Audio TTS via OpenRouter /audio/speech) reads every
       scene with the chosen voice; the
       scene clips are joined into one WAV with a short pause between scenes,
       so the player knows where each scene starts.
    3. Output: <output-dir>/<Short title>/ with storyboard.<ext>, roteiro.md,
       audio.wav and story.json (metadata + scene timestamps). A CSV log
       (log_story_generate.csv) in the output dir records every story.

CLI examples:
    python story_generate.py --input-dir ./storyboards --output-dir ./stories \\
        --voice 802e3bc2b27e49c2995d23ef70e6ac89=narradora --voice <id2>
    python story_generate.py --input-dir ./storyboards --dry-run   # offline, no cost
    python story_generate.py --check-voices --voice <id>
    python story_generate.py --list --output-dir ./stories
    python story_generate.py --play "./stories/Um dia comum"
    python story_generate.py --gui

GUI (stdlib tkinter): tabs Generate / Model / Voices / Player. The Player tab
shows the storyboard with the script beside it and play/pause, seek (±10 s),
previous/next scene and a seek bar; the current scene is highlighted.

Key: one OpenRouter key for writer AND narrator, shared with image_generate.py
(vault slot "openrouter", env OPENROUTER_API_KEY). Never printed (key_hash
only). Voice metadata (name/tags/languages) comes from Fish Audio's public
model page API, no key needed.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import http.client
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import wave
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import image_generate as ig  # noqa: E402  (shared HTTP, vault, helpers)

WRITER_DEFAULT_MODEL = "google/gemini-3.7-flash"
SPEECH_URL = "https://openrouter.ai/api/v1/audio/speech"
GENERATION_URL = "https://openrouter.ai/api/v1/generation?id={id}"
FISH_MODEL_URL = "https://api.fish.audio/model/{id}"  # public voice metadata
# Fish Audio TTS models served by OpenRouter (voice = Fish voice id).
TTS_MODELS = ["fish-audio/s2.1-pro-free:free", "fish-audio/s2.1-pro",
              "fish-audio/s2-pro", "fish-audio/s1"]
DEFAULT_TTS_MODEL = "fish-audio/s2.1-pro-free:free"
LANGUAGES = ["pt-BR", "en-US", "es-ES"]
DEFAULT_LANGUAGE = "pt-BR"
MAX_VOICES = 5
OPENROUTER_PROVIDER = "openrouter"

STORY_JSON = "story.json"
SCRIPT_MD = "roteiro.md"
AUDIO_WAV = "audio.wav"
PREVIEW_PNG = "preview.png"
STORYBOARD_STEM = "storyboard"
LOG_FILENAME = "log_story_generate.csv"
CONFIG_FILENAME = "story_config.json"

TTS_SAMPLE_RATE = 44100
SCENE_GAP_S = 0.6
MAX_SCENES = 24
MAX_TITLE_CHARS = 60
WRITER_TIMEOUT_S = 180
TTS_TIMEOUT_S = 300
VOICE_INFO_TIMEOUT_S = 15
COST_LOOKUP_TIMEOUT_S = 15
WRITER_MAX_TOKENS = 6000
MODELS_URL = "https://openrouter.ai/api/v1/models"  # public catalog (no key)
RATE_LIMIT_WAITS_S = (5, 15, 30)  # retries on HTTP 429 (":free" models share a pool)
COST_FILL_DEADLINE_S = 60  # background TTS cost lookup per story
# OpenRouter reasoning effort scale, highest -> lowest ("none" = no thinking).
EFFORT_ORDER = ["max", "xhigh", "high", "medium", "low", "minimal", "none"]

# Narration speed of Fish voices in pt-BR, measured on real stories: 15.5-19.5
# characters/s depending on the voice. Per-voice rates are recalibrated from
# the stories already in the output dir (speech_rates).
DEFAULT_CHARS_PER_S = 17.4
CHARS_PER_WORD = 5.6
MIN_DURATION_S, MAX_DURATION_S = 10, 1800

# Writer behaviours (Generate tab dropdown / --style).
WRITER_STYLES = {
    "descriptive": {
        "label": "Descritivo (fiel às imagens)",
        "instruction": (
            "Write, in {language}, a story that tells what happens in each panel: one "
            "scene per panel, in panel order. Describe what is happening in each scene "
            "and connect each scene to the next one (cause, time passing, emotion, "
            "place) so the whole story flows with continuity. Keep characters, "
            "objects and places consistent across scenes. Only narrate what the "
            "panels show or clearly imply; do not invent extra scenes."),
        "sentences": "2 to 5 sentences per scene",
    },
    "connective": {
        "label": "Narrativo (conecta as cenas)",
        "instruction": (
            "Write, in {language}, a story that uses the panels as key moments of one "
            "continuous plot: still one scene per panel, in panel order, but the focus "
            "is the story between the pictures, not the pictures themselves. Start "
            "every scene (except the first) by bridging the gap from the previous "
            "panel: explain why and how the character got from there to here - the "
            "motivation, what happened in between, time passing, a small plausible "
            "event - so each jump makes sense (e.g. if a girl is cleaning her shoes "
            "and next she is walking down the street, tell where she is going and "
            "why). Then tell what happens in this panel, keeping only the visual "
            "details that matter to the plot. Invented connecting events must be "
            "plausible, consistent with every panel and with each other; never "
            "contradict a panel and do not add new main characters. The last scene "
            "closes the story."),
        "sentences": "3 to 6 sentences per scene",
    },
}
DEFAULT_STYLE = "descriptive"
MODELS_TIMEOUT_S = 20

VOICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,64}")

LOG_FIELDS = [
    "date",
    "status",
    "title",
    "folder",
    "source_image",
    "scenes",
    "voice_id",
    "voice_label",
    "language",
    "style",
    "writer_model",
    "writer_effort",
    "tts_model",
    "target_seconds",
    "narration_chars",
    "audio_seconds",
    "writer_cost_usd",
    "tts_cost_usd",
    "total_seconds",
    "key_hash",
    "error",
]


def default_output_dir() -> str:
    """<pictures>/StoryGenerate, next to image_generate's default folder."""
    return str(Path(ig.default_output_dir()).parent / "StoryGenerate")


# ---------------------------------------------------------------------------
# Key (one OpenRouter key for writer + narrator, shared with image_generate)
# ---------------------------------------------------------------------------

def resolve_openrouter_key(cli_key: str | None = None) -> tuple[str | None, str]:
    return ig.resolve_api_key(OPENROUTER_PROVIDER, cli_key)


# ---------------------------------------------------------------------------
# Config (GUI settings; same vault dir as image_generate)
# ---------------------------------------------------------------------------

CONFIG_KEYS = ("input_dir", "output_dir", "writer_model", "tts_model", "language",
               "voices", "dry_run", "force", "style", "duration")


def config_path() -> Path:
    return ig.vault_dir() / CONFIG_FILENAME


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: data[k] for k in CONFIG_KEYS if k in data} if isinstance(data, dict) else {}


def save_config(settings: dict) -> Path:
    directory = ig.vault_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: settings[k] for k in CONFIG_KEYS if k in settings},
                              indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def sanitize_config(data: dict) -> dict:
    clean: dict = {}
    for key in ("input_dir", "output_dir", "writer_model", "language"):
        if isinstance(data.get(key), str):
            clean[key] = data[key]
    tts = str(data.get("tts_model", DEFAULT_TTS_MODEL))
    if tts == "s2.1-pro-free":  # old direct-Fish names -> OpenRouter slugs
        tts = DEFAULT_TTS_MODEL
    elif f"fish-audio/{tts}" in TTS_MODELS:
        tts = f"fish-audio/{tts}"
    clean["tts_model"] = tts if tts in TTS_MODELS else DEFAULT_TTS_MODEL
    voices = []
    for item in data.get("voices", []) if isinstance(data.get("voices"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            voices.append({"id": item["id"].strip(), "label": str(item.get("label", "")).strip()})
    clean["voices"] = voices[:MAX_VOICES]
    clean["dry_run"] = bool(data.get("dry_run", False))
    clean["force"] = bool(data.get("force", False))
    style = str(data.get("style", DEFAULT_STYLE))
    clean["style"] = style if style in WRITER_STYLES else DEFAULT_STYLE
    duration = str(data.get("duration", "") or "")
    try:
        parse_duration(duration)
        clean["duration"] = duration
    except ValueError:
        clean["duration"] = ""
    return clean


def parse_duration(raw: str | int | float | None) -> float | None:
    """Target narration length: "" -> None (automatic); "90", "90s", "1:30",
    "2m", "1m30s" -> seconds. Raises ValueError outside 10 s - 30 min."""
    text = str(raw if raw is not None else "").strip().lower().replace(" ", "")
    if not text:
        return None
    match = (re.fullmatch(r"(\d+):([0-5]?\d)", text)
             or re.fullmatch(r"(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?", text))
    if not match or not any(match.groups()):
        raise ValueError(f"invalid duration: {raw!r} (use seconds like 90, or 1:30, or 2m)")
    minutes, seconds = match.groups()
    total = float(minutes or 0) * 60 + float(seconds or 0)
    if not MIN_DURATION_S <= total <= MAX_DURATION_S:
        raise ValueError(f"duration must be between {MIN_DURATION_S} s and "
                         f"{MAX_DURATION_S // 60} min (got {format_clock(total)})")
    return total


def style_label(style: str) -> str:
    return WRITER_STYLES.get(style, WRITER_STYLES[DEFAULT_STYLE])["label"]


def style_from_label(label: str) -> str:
    """Dropdown label or key -> style key (unknown -> default)."""
    for key, info in WRITER_STYLES.items():
        if label in (key, info["label"]):
            return key
    return DEFAULT_STYLE


# ---------------------------------------------------------------------------
# Voices (Fish Audio voice ids)
# ---------------------------------------------------------------------------

def parse_voice_spec(spec: str) -> dict:
    """"ID" or "ID=label" -> {"id", "label"}. Raises ValueError."""
    voice_id, _, label = spec.partition("=")
    voice_id = voice_id.strip()
    if not VOICE_ID_RE.fullmatch(voice_id):
        raise ValueError(f"invalid Fish Audio voice id: {voice_id!r} "
                         "(copy it from the voice page URL: fish.audio/m/<id>)")
    return {"id": voice_id, "label": label.strip()}


def validate_voices(voices: list[dict], allow_empty: bool = False) -> list[dict]:
    """1..MAX_VOICES unique voices with valid ids. Raises ValueError."""
    clean: list[dict] = []
    seen: set[str] = set()
    for voice in voices:
        voice_id = str(voice.get("id", "")).strip()
        if not voice_id:
            continue
        parsed = parse_voice_spec(voice_id)
        parsed["label"] = str(voice.get("label", "")).strip()
        if parsed["id"] in seen:
            raise ValueError(f"duplicate voice id: {parsed['id']}")
        seen.add(parsed["id"])
        clean.append(parsed)
    if len(clean) > MAX_VOICES:
        raise ValueError(f"at most {MAX_VOICES} voices (got {len(clean)})")
    if not clean and not allow_empty:
        raise ValueError("no voice configured: add 1 to 5 Fish Audio voice ids "
                         "(--voice ID[=label] or the Voices tab)")
    return clean


def fetch_voice_info(voice_id: str,
                     cancel_event: threading.Event | None = None) -> dict:
    """Title/description/tags/languages of a Fish Audio voice ({} on failure).

    Public Fish Audio model page API (no key); only used to help the writer
    choose, so any failure just means "no extra info".
    """
    headers = {"Accept": "application/json"}
    url = FISH_MODEL_URL.format(id=urllib.parse.quote(voice_id, safe=""))
    try:
        status, raw = ig._fetch_json(url, headers, VOICE_INFO_TIMEOUT_S, cancel_event)
    except ig.GenerationCancelled:
        raise
    except (OSError, http.client.HTTPException):
        return {}
    if status != 200:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    langs = data.get("languages") if isinstance(data.get("languages"), list) else []
    return {
        "title": str(data.get("title") or "")[:80],
        "description": " ".join(str(data.get("description") or "").split())[:300],
        "tags": [str(t) for t in tags][:10],
        "languages": [str(lang) for lang in langs][:10],
    }


def describe_voices(voices: list[dict], dry_run: bool = False,
                    cancel_event: threading.Event | None = None) -> list[dict]:
    """Voices enriched with Fish Audio metadata (skipped in dry-run)."""
    described = []
    for voice in voices:
        info = {} if dry_run else fetch_voice_info(voice["id"], cancel_event)
        described.append({**voice, **info})
    return described


def voice_display(voice: dict) -> str:
    """Short human name: label, else Fish title, else the id."""
    return voice.get("label") or voice.get("title") or voice.get("id", "")


# ---------------------------------------------------------------------------
# Writer (OpenRouter vision chat model)
# ---------------------------------------------------------------------------

def image_data_url(path: Path) -> str:
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{ig.guess_mime(path)};base64,{b64}"


def scene_word(language: str) -> str:
    lang = (language or "").lower()
    if lang.startswith("pt") or lang.startswith("es"):
        return "Cena"
    return "Scene"


def length_words(target_s: float, chars_per_s: float, scenes_guess: int = 6) -> int:
    """Words that fill target_s of narration at chars_per_s (pauses excluded)."""
    speech_s = max(5.0, target_s - SCENE_GAP_S * (scenes_guess - 1))
    return max(20, round(speech_s * chars_per_s / CHARS_PER_WORD))


def writer_instruction(voices: list[dict], language: str, style: str = DEFAULT_STYLE,
                       target_s: float | None = None,
                       rates: dict[str, float] | None = None) -> str:
    """Single user message for the writer (image goes in the same message).

    style picks the writing behaviour (WRITER_STYLES). target_s asks for a
    total narration length, converted to words per voice with the calibrated
    speech rates (voices read at different speeds).
    """
    rates = rates or {}
    info = WRITER_STYLES.get(style, WRITER_STYLES[DEFAULT_STYLE])
    lines = []
    for voice in voices:
        parts = [f'- id: "{voice["id"]}"']
        for key in ("label", "title", "description"):
            if voice.get(key):
                parts.append(f"{key}: {voice[key]}")
        for key in ("tags", "languages"):
            if voice.get(key):
                parts.append(f"{key}: {', '.join(voice[key])}")
        if target_s:
            rate = rates.get(voice["id"], rates.get("*", DEFAULT_CHARS_PER_S))
            parts.append(f"total narration if you pick this voice: about "
                         f"{length_words(target_s, rate)} words")
        lines.append("; ".join(parts))
    voice_block = "\n".join(lines) if lines else "- (no voices: use an empty voice_id)"
    if target_s:
        words = length_words(target_s, rates.get("*", DEFAULT_CHARS_PER_S))
        length_rule = (
            f"LENGTH IS A HARD REQUIREMENT: the narration read aloud must last about "
            f"{format_clock(target_s)} ({round(target_s)} s). Sum of all scene narrations: "
            f"about {words} words (the exact number for each voice is in the voice "
            "list below - use the one of the voice you choose); split it evenly-ish "
            "across the scenes. Do not go more than 10% over or under.")
    else:
        length_rule = f"Length: {info['sentences']}."
    return (
        "You are a screenwriter and narrator. The attached image is a storyboard: "
        "a grid of panels that together tell one story. Read the panels in order: "
        "left to right, top to bottom (panel 1 is the top-left one).\n\n"
        + info["instruction"].format(language=language) + "\n\n"
        "The narration of every scene will be read aloud by a text-to-speech "
        "voice: write flowing prose, no markdown, no emojis, no stage directions, "
        "no panel/scene numbers inside the narration, and no text in brackets. "
        + length_rule + "\n\n"
        "Give the story a short title (at most 5 words, no quotes).\n\n"
        "Then choose the narrator voice that best fits this story (tone, mood, "
        "the protagonist, and the story language) from this list:\n"
        f"{voice_block}\n\n"
        "Reply with ONLY one JSON object, no code fences, exactly in this shape:\n"
        '{"title": "...", "logline": "one sentence summary", '
        '"scenes": [{"heading": "short scene heading", "narration": "..."}], '
        '"voice_id": "one id from the list", "voice_reason": "why this voice"}'
    )


def extract_json_object(text: str) -> dict:
    """Parse the first JSON object in a model reply (tolerates code fences)."""
    body = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", body, re.S)
    if fence:
        body = fence.group(1)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("writer reply has no JSON object")
    data = json.loads(body[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("writer reply is not a JSON object")
    return data


def _clean_line(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text.strip(" \"'“”«»")[:limit].strip()


def normalize_story(data: dict, voices: list[dict]) -> dict:
    """Validate the writer JSON and resolve the chosen voice. Raises ValueError."""
    title = _clean_line(data.get("title"), MAX_TITLE_CHARS)
    if not title:
        raise ValueError("writer reply has no title")
    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ValueError("writer reply has no scenes")
    scenes = []
    for index, raw in enumerate(raw_scenes[:MAX_SCENES]):
        if isinstance(raw, str):
            raw = {"narration": raw}
        if not isinstance(raw, dict):
            raise ValueError(f"scene {index + 1} is not an object")
        narration = " ".join(str(raw.get("narration") or raw.get("text") or "").split())
        if not narration:
            raise ValueError(f"scene {index + 1} has no narration")
        scenes.append({"number": index + 1,
                       "heading": _clean_line(raw.get("heading"), 80),
                       "narration": narration})
    story = {"title": title,
             "logline": _clean_line(data.get("logline"), 300),
             "scenes": scenes,
             "voice_id": "",
             "voice_label": "",
             "voice_reason": _clean_line(data.get("voice_reason"), 300)}
    if voices:
        chosen = str(data.get("voice_id") or "").strip()
        match = next((v for v in voices if v["id"] == chosen), None)
        if match is None:  # model echoed a name instead of the id
            lowered = chosen.lower()
            match = next((v for v in voices if lowered and lowered in
                          (str(v.get("label", "")).lower(), str(v.get("title", "")).lower())),
                         None)
        if match is None:
            match = voices[0]
            story["voice_reason"] = (f"fallback to the first voice (writer returned "
                                     f"{chosen!r}, not in the list)")
        story["voice_id"] = match["id"]
        story["voice_label"] = voice_display(match)
    return story


def dry_run_story(image_path: Path, voices: list[dict], language: str) -> dict:
    """Offline placeholder story (no API call, no cost)."""
    word = scene_word(language)
    scenes = [{"number": i + 1, "heading": "teste",
               "narration": f"{word} {i + 1} de teste para {image_path.name}. "
                            "Nenhuma chamada paga foi feita."}
              for i in range(6)]
    story = {"title": f"Teste {image_path.stem}"[:MAX_TITLE_CHARS],
             "logline": "História de teste gerada em dry-run.",
             "scenes": scenes, "voice_id": "", "voice_label": "",
             "voice_reason": "dry-run"}
    if voices:
        story["voice_id"] = voices[0]["id"]
        story["voice_label"] = voice_display(voices[0])
    return story


StatusFn = Callable[[str], None]


def _say(status: StatusFn | None, message: str) -> None:
    if status is not None:
        status(message)


def with_rate_limit_retry(call: Callable[[], tuple], cancel_event: threading.Event | None,
                          waits: tuple[float, ...] = RATE_LIMIT_WAITS_S,
                          status: StatusFn | None = None, label: str = "") -> tuple:
    """Run call() (returns (status, ...)); on HTTP 429 wait and retry.

    Cancellable while waiting (ig.GenerationCancelled). The last 429 reply is
    returned as-is so the caller reports it.
    """
    for attempt, wait_s in enumerate((*waits, None), start=1):
        reply = call()
        if reply[0] != 429 or wait_s is None:
            return reply
        _say(status, f"{label or 'model'} busy (HTTP 429): retrying in {wait_s:g} s "
                     f"({attempt}/{len(waits)})")
        if cancel_event is not None:
            if cancel_event.wait(wait_s):
                raise ig.GenerationCancelled("generation cancelled")
        else:
            time.sleep(wait_s)
        _say(status, f"{label or 'model'}: retry {attempt}/{len(waits)} sent, "
                     "waiting for the reply...")
    return reply  # unreachable, keeps type checkers calm


_MODELS_CACHE: dict[str, dict] = {}


def openrouter_models(refresh: bool = False) -> dict[str, dict]:
    """Public OpenRouter chat-model catalog {id: model} (cached; {} on failure)."""
    if _MODELS_CACHE and not refresh:
        return _MODELS_CACHE
    try:
        status, raw = ig._fetch_json(MODELS_URL, {"Accept": "application/json"},
                                     MODELS_TIMEOUT_S)
        data = json.loads(raw).get("data", []) if status == 200 else []
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        data = []
    catalog = {m["id"]: m for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)}
    if catalog:
        _MODELS_CACHE.clear()
        _MODELS_CACHE.update(catalog)
    return catalog


def _accepts_images(model: dict) -> bool:
    arch = model.get("architecture") or {}
    return ("image" in (arch.get("input_modalities") or [])
            and "text" in (arch.get("output_modalities") or []))


def vision_model_suggestions(catalog: dict[str, dict], free_only: bool, limit: int = 5
                             ) -> list[str]:
    """Some models that read images and write text (free ones when asked)."""
    def price(mid: str) -> float:
        try:
            return float((catalog[mid].get("pricing") or {}).get("prompt") or 0)
        except (TypeError, ValueError):
            return float("inf")

    ids = [mid for mid, m in catalog.items() if _accepts_images(m)
           and not mid.startswith(("openrouter/", "~")) and "agent" not in mid
           and "inkling" not in mid  # 403: "only available on agentic harnesses"
           and (mid.endswith(":free") if free_only else ":" not in mid and price(mid) > 0)]
    preferred = [WRITER_DEFAULT_MODEL, "google/gemma-4-31b-it:free", "qwen/qwen3.8-27b:free"]
    ids.sort(key=lambda mid: (mid not in preferred, preferred.index(mid) if mid in preferred
                              else 0, price(mid), mid))
    return ids[:limit]


def check_writer_model(model: str | list[str]) -> None:
    """Fail fast (ValueError) when a writer model (or any model of an "a;b"
    fallback chain) cannot see images.

    Uses the public catalog; if it cannot be fetched, the check is skipped
    (the writer call itself will report the problem).
    """
    catalog = openrouter_models()
    if not catalog:
        return
    for one in parse_model_chain(model):
        _check_one_writer_model(one, catalog)


def _check_one_writer_model(model: str, catalog: dict[str, dict]) -> None:
    info = catalog.get(model)
    free_only = model.endswith(":free")
    hint = ", ".join(vision_model_suggestions(catalog, free_only)) or WRITER_DEFAULT_MODEL
    if info is None:
        raise ValueError(f"modelo escritor {model!r} não existe no OpenRouter. "
                         f"Modelos que leem imagem{' (grátis)' if free_only else ''}: {hint}")
    if not _accepts_images(info):
        inputs = ", ".join((info.get("architecture") or {}).get("input_modalities") or ["?"])
        raise ValueError(f"o modelo escritor {model!r} não aceita imagem (entrada: {inputs}), "
                         "então não consegue ver o storyboard. Use um modelo com visão"
                         f"{' grátis' if free_only else ''}, por exemplo: {hint}")


def parse_model_chain(text: str | list[str] | None) -> list[str]:
    """"a;b;c" -> ["a", "b", "c"]: the first model, then its fallbacks in order
    (blanks and repeats dropped). Empty -> [WRITER_DEFAULT_MODEL]."""
    items = text if isinstance(text, list) else str(text or "").split(";")
    chain: list[str] = []
    for item in items:
        model = str(item).strip()
        if model and model not in chain:
            chain.append(model)
    return chain or [WRITER_DEFAULT_MODEL]


def effort_ladder(model: str, catalog: dict[str, dict] | None = None) -> list[str]:
    """Thinking levels a model accepts, from its default down to the lowest.

    Uses the public catalog (reasoning.supported_efforts / default_effort /
    mandatory). "none" is appended when thinking can be switched off. A model
    that does not think by default yields [] (nothing to lower); an unknown
    model gets a generic ladder starting at "medium".
    """
    info = (catalog if catalog is not None else openrouter_models()).get(model)
    if info is None:
        return ["medium", "low", "minimal", "none"]
    reasoning = info.get("reasoning") if isinstance(info.get("reasoning"), dict) else None
    if not reasoning or not reasoning.get("default_enabled", True):
        return []
    supported = [e for e in EFFORT_ORDER if e in (reasoning.get("supported_efforts") or [])]
    if not supported:
        supported = ["medium", "low", "minimal"]
    if not reasoning.get("mandatory") and "none" not in supported:
        supported.append("none")
    default = reasoning.get("default_effort")
    start = supported.index(default) if default in supported else 0
    return supported[start:]


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()


def _write_story_once(
    body: dict, model: str, api_key: str, voices: list[dict], timeout_s: int,
    cancel_event: threading.Event | None, status: StatusFn | None, spent: list[float],
) -> dict:
    """One writer model: its own 429 retries, one retry on invalid JSON, and
    on an empty reply or a timeout the next attempt thinks one level less
    (effort_ladder), until the lowest level also fails.
    Returns the story with story["writer_effort"] = level used ("default"
    when the model's own default worked)."""
    body = {**body, "model": model}
    ladder = effort_ladder(model)
    level = 0  # ladder[0] = the model default: the first request does not force it
    json_retry_used = False
    last_error: Exception | None = None

    def effort_name() -> str:
        return "default" if level == 0 or not ladder else ladder[level]

    def lower_thinking(reason: str) -> bool:
        nonlocal level
        if level + 1 >= len(ladder):
            return False
        level += 1
        _say(status, f"writer {model}: {reason} - retrying with less thinking "
                     f"(effort {ladder[level - 1] if level > 1 else 'default'} -> "
                     f"{ladder[level]})")
        return True

    while True:
        request = dict(body)
        if level > 0 and ladder:
            request["reasoning"] = {"effort": ladder[level]}
        _say(status, f"sent to the writer ({model}, thinking {effort_name()}): waiting for "
                     f"the story (up to {timeout_s // 60} min)...")
        try:
            http_status, raw = with_rate_limit_retry(
                lambda: ig._post_json(ig.CHAT_URL, request, ig._openrouter_headers(api_key),
                                      timeout_s, cancel_event), cancel_event, status=status,
                label=f"writer {model}")
        except ig.GenerationCancelled:
            raise
        except OSError as exc:
            if _is_timeout(exc):
                if lower_thinking(f"no reply after {timeout_s // 60} min (timeout)"):
                    continue
                raise RuntimeError(f"writer {model} timed out after {timeout_s} s even with "
                                   f"the lowest thinking level ({effort_name()})") from exc
            raise
        if http_status != 200:
            if ig._is_content_policy_refusal(http_status, raw):
                raise ig.ContentPolicyError(
                    f"O provedor bloqueou o storyboard por filtro de conteúdo "
                    f"(OpenRouter HTTP {http_status}, model {model}). Detalhe: {raw[:400]}")
            if http_status == 429:
                raise RuntimeError(f"o modelo escritor {model!r} está com limite de uso "
                                   "(HTTP 429) mesmo após 3 novas tentativas. Modelos :free "
                                   "dividem uma fila: tente mais tarde ou use outro modelo. "
                                   f"Detalhe: {raw[:300]}")
            if http_status == 404 and "image input" in raw:
                raise RuntimeError(f"o modelo escritor {model!r} não aceita imagem "
                                   f"(OpenRouter HTTP 404). Escolha um modelo com visão.")
            raise RuntimeError(f"OpenRouter HTTP {http_status}: {raw[:2000]}")
        payload = json.loads(raw)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        try:
            spent.append(float(usage.get("cost") or 0.0))
        except (TypeError, ValueError):
            pass
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if isinstance(message.get("refusal"), str) and message["refusal"].strip():
            raise RuntimeError(f"writer refused: {message['refusal'][:300]}")
        text = ig._extract_message_text(message)
        truncated = choice.get("finish_reason") == "length"
        if not text.strip() or (truncated and "}" not in text):
            reason = ("empty reply (thinking used the whole budget)" if truncated
                      else "empty reply")
            if lower_thinking(reason):
                continue
            raise RuntimeError(f"writer {model} returned an empty reply even with the lowest "
                               f"thinking level ({effort_name()})")
        try:
            story = normalize_story(extract_json_object(text), voices)
        except ValueError as exc:
            last_error = exc
            if json_retry_used:
                raise RuntimeError(f"writer {model} returned an invalid story twice: "
                                   f"{last_error}") from exc
            json_retry_used = True
            _say(status, f"{model} reply was not valid story JSON: asking again (2/2)")
            continue
        story["writer_effort"] = effort_name()
        return story


def write_story(
    image_path: Path,
    *,
    api_key: str | None,
    model: str | list[str],
    voices: list[dict],
    language: str,
    timeout_s: int = WRITER_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
    dry_run: bool = False,
    style: str = DEFAULT_STYLE,
    target_s: float | None = None,
    rates: dict[str, float] | None = None,
    status: StatusFn | None = None,
) -> tuple[dict, float]:
    """Ask the writer for the story. Returns (story, cost_usd).

    model may be a fallback chain "a;b;c": each model gets its own 429
    retries and one retry on invalid JSON; any failure of a model moves on to
    the next one (cancellation stops at once). story["writer_model"] is the
    model that wrote it, story["writer_fallbacks"] the failures before it.
    The cost includes failed attempts that were billed.
    """
    chain = parse_model_chain(model)
    if dry_run:
        story = dry_run_story(image_path, voices, language)
        story.update(writer_model=chain[0], writer_fallbacks=[])
        return story, 0.0
    if not api_key:
        raise RuntimeError("missing OpenRouter API key (set OPENROUTER_API_KEY, use "
                           "--openrouter-key, or save it with --remember-key)")
    body = {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": writer_instruction(voices, language, style, target_s,
                                                         rates)},
            {"type": "image_url", "image_url": {"url": image_data_url(image_path)}},
        ]}],
        "max_tokens": WRITER_MAX_TOKENS,
        "temperature": 0.8,
    }
    spent: list[float] = []
    failures: list[dict] = []
    for index, current in enumerate(chain):
        if failures:
            _say(status, f"writer {failures[-1]['model']} failed: falling back to {current} "
                         f"({index + 1}/{len(chain)})")
        try:
            story = _write_story_once(body, current, api_key, voices, timeout_s,
                                      cancel_event, status, spent)
        except ig.GenerationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - try the next model of the chain
            failures.append({"model": current, "error": f"{type(exc).__name__}: {exc}"})
            if len(chain) == 1:
                raise
            continue
        story.update(writer_model=current, writer_fallbacks=failures)
        return story, sum(spent)
    raise RuntimeError(f"all {len(chain)} writer models failed: "
                       + " | ".join(f"{f['model']}: {f['error'][:300]}" for f in failures))


def script_markdown(story: dict, language: str) -> str:
    """roteiro.md: title, logline, one section per scene, narrator voice."""
    word = scene_word(language)
    lines = [f"# {story['title']}", ""]
    if story.get("logline"):
        lines += [f"_{story['logline']}_", ""]
    for scene in story["scenes"]:
        heading = f"## {word} {scene['number']}"
        if scene.get("heading"):
            heading += f" — {scene['heading']}"
        if "start" in scene:
            heading += f" ({format_clock(scene['start'])})"
        lines += [heading, "", scene["narration"], ""]
    if story.get("voice_id"):
        lines += ["---", "",
                  f"Voz: {story.get('voice_label') or story['voice_id']} "
                  f"(`{story['voice_id']}`)"
                  + (f" — {story['voice_reason']}" if story.get("voice_reason") else ""),
                  ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Narrator (Fish Audio TTS via OpenRouter) + WAV helpers
# ---------------------------------------------------------------------------

def _post_bytes(
    url: str,
    body: dict,
    headers: dict,
    timeout_s: int,
    cancel_event: threading.Event | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    """POST JSON, return (status, raw bytes, lowercased headers).

    Abortable via ig.abort_all_http() (registered like ig._post_json)."""
    parts = urllib.parse.urlsplit(url)
    conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                else http.client.HTTPConnection)
    default_port = 443 if parts.scheme == "https" else 80
    conn = conn_cls(parts.hostname or "", parts.port or default_port, timeout=timeout_s)
    with ig._HTTP_LOCK:
        ig._HTTP_CONNS.add(conn)
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise ig.GenerationCancelled("generation cancelled")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn.request("POST", path, body=json.dumps(body).encode("utf-8"), headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if cancel_event is not None and cancel_event.is_set():
            raise ig.GenerationCancelled("generation cancelled")
        return resp.status, raw, {k.lower(): v for k, v in resp.getheaders()}
    except (OSError, http.client.HTTPException) as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise ig.GenerationCancelled("generation cancelled") from exc
        raise
    finally:
        with ig._HTTP_LOCK:
            ig._HTTP_CONNS.discard(conn)
        try:
            conn.close()
        except Exception:
            pass


def parse_wav(raw: bytes, fallback_rate: int = TTS_SAMPLE_RATE) -> tuple[int, int, int, bytes]:
    """(channels, sample_width, rate, pcm) from WAV bytes.

    Streamed WAVs may carry a 0/0xFFFFFFFF data size: then everything after
    the data header is PCM. Headerless input is treated as 16-bit mono PCM.
    """
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return 1, 2, fallback_rate, raw[:len(raw) // 2 * 2]
    pos = 12
    fmt: tuple[int, int, int] | None = None
    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        size = int.from_bytes(raw[pos + 4:pos + 8], "little")
        start = pos + 8
        if chunk_id == b"fmt ":
            audio_format = int.from_bytes(raw[start:start + 2], "little")
            channels = int.from_bytes(raw[start + 2:start + 4], "little")
            rate = int.from_bytes(raw[start + 4:start + 8], "little")
            bits = int.from_bytes(raw[start + 14:start + 16], "little")
            if audio_format not in (1, 0xFFFE) or bits not in (8, 16, 24, 32):
                raise ValueError(f"unsupported WAV format {audio_format} / {bits} bits")
            fmt = (max(1, channels), bits // 8, rate or fallback_rate)
        elif chunk_id == b"data":
            if fmt is None:
                raise ValueError("WAV data before fmt chunk")
            end = len(raw) if size in (0, 0xFFFFFFFF) or start + size > len(raw) else start + size
            frame = fmt[0] * fmt[1]
            pcm = raw[start:end]
            return fmt[0], fmt[1], fmt[2], pcm[:len(pcm) // frame * frame]
        if size in (0xFFFFFFFF,):
            break
        pos = start + size + (size & 1)
    raise ValueError("WAV without data chunk")


def silence(channels: int, width: int, rate: int, seconds: float) -> bytes:
    frames = int(round(rate * seconds))
    return (b"\x80" if width == 1 else b"\x00") * (frames * channels * width)


def write_wav(path: Path, channels: int, width: int, rate: int, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(pcm)


def join_scene_audio(clips: list[bytes], gap_s: float = SCENE_GAP_S
                     ) -> tuple[tuple[int, int, int], bytes, list[tuple[float, float]]]:
    """Join per-scene WAV clips with a pause between them.

    Returns ((channels, width, rate), pcm, [(start_s, end_s) per scene]).
    """
    params: tuple[int, int, int] | None = None
    pcm_parts: list[bytes] = []
    times: list[tuple[float, float]] = []
    cursor = 0
    for index, clip in enumerate(clips):
        channels, width, rate, pcm = parse_wav(clip)
        if params is None:
            params = (channels, width, rate)
        elif params != (channels, width, rate):
            raise ValueError(f"scene {index + 1} audio format {channels}ch/{width * 8}bit/"
                             f"{rate}Hz differs from scene 1")
        if index:
            gap = silence(*params, gap_s)
            pcm_parts.append(gap)
            cursor += len(gap)
        frame_bytes = params[0] * params[1]
        start = cursor / frame_bytes / params[2]
        pcm_parts.append(pcm)
        cursor += len(pcm)
        times.append((round(start, 3), round(cursor / frame_bytes / params[2], 3)))
    if params is None:
        raise ValueError("no scene audio")
    return params, b"".join(pcm_parts), times


def dry_run_clip(text: str, rate: int = 16000) -> bytes:
    """Offline stand-in for a TTS clip: silence sized to the text length."""
    seconds = min(12.0, max(1.0, len(text) / 18.0))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(silence(1, 2, rate, seconds))
    return buffer.getvalue()


def pcm_params(content_type: str) -> tuple[int, int]:
    """(rate, channels) from "audio/pcm;rate=44100;channels=1" (16-bit LE)."""
    rate, channels = TTS_SAMPLE_RATE, 1
    for part in (content_type or "").split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.lower() == "rate" and value.strip().isdigit():
            rate = int(value)
        elif name.lower() == "channels" and value.strip().isdigit():
            channels = max(1, int(value))
    return rate, channels


def pcm_to_wav(pcm: bytes, rate: int, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    frame = channels * 2
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm[:len(pcm) // frame * frame])
    return buffer.getvalue()


def synthesize(
    text: str,
    *,
    api_key: str | None,
    voice_id: str,
    tts_model: str,
    timeout_s: int = TTS_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
    dry_run: bool = False,
    status: StatusFn | None = None,
) -> tuple[bytes, str]:
    """One TTS call (OpenRouter /audio/speech, Fish Audio model) -> (WAV, generation id).

    Asks for raw PCM (lossless, easy to join); rate/channels come from the
    Content-Type ("audio/pcm;rate=44100;channels=1"). A WAV reply is kept.
    """
    if dry_run:
        return dry_run_clip(text), ""
    if not api_key:
        raise RuntimeError("missing OpenRouter API key (set OPENROUTER_API_KEY, use "
                           "--openrouter-key, or save it with --remember-key)")
    if not voice_id:
        raise RuntimeError("no voice selected for the narration")
    body = {"model": tts_model, "input": text, "voice": voice_id, "response_format": "pcm"}
    headers = {**ig._openrouter_headers(api_key), "Accept": "audio/*"}
    http_status, raw, reply_headers = with_rate_limit_retry(
        lambda: _post_bytes(SPEECH_URL, body, headers, timeout_s, cancel_event), cancel_event,
        status=status, label="narrator")
    if http_status != 200:
        detail = raw[:800].decode("utf-8", errors="replace")
        if ig._is_content_policy_refusal(http_status, detail):
            raise ig.ContentPolicyError(
                f"O provedor bloqueou a narração por filtro de conteúdo "
                f"(OpenRouter HTTP {http_status}, model {tts_model}). Detalhe: {detail[:400]}")
        if http_status == 429:
            raise RuntimeError(f"a narração ({tts_model}) está com limite de uso (HTTP 429) "
                               "mesmo após 3 novas tentativas; tente mais tarde ou use outro "
                               f"modelo de voz. Detalhe: {detail[:300]}")
        if http_status == 402:
            raise RuntimeError(f"OpenRouter HTTP 402: saldo insuficiente para {tts_model} "
                               f"(o modelo :free não cobra). Detalhe: {detail}")
        raise RuntimeError(f"OpenRouter speech HTTP {http_status}: {detail}")
    if not raw:
        raise RuntimeError("OpenRouter speech returned empty audio")
    content_type = reply_headers.get("content-type", "")
    generation_id = reply_headers.get("x-generation-id", "")
    if raw[:4] == b"RIFF" or "wav" in content_type:
        return raw, generation_id
    if "mpeg" in content_type or "mp3" in content_type:
        raise RuntimeError(f"OpenRouter speech returned {content_type} (expected pcm)")
    rate, channels = pcm_params(content_type)
    return pcm_to_wav(raw, rate, channels), generation_id


COST_LOOKUP_DEADLINE_S = 30  # OpenRouter generation stats lag ~10-15 s


def lookup_generation_cost(generation_ids: list[str], api_key: str | None,
                           cancel_event: threading.Event | None = None,
                           deadline_s: float = COST_LOOKUP_DEADLINE_S,
                           poll_s: float = 1.5) -> float | None:
    """Sum OpenRouter total_cost of TTS generations; None if not available in time.

    Generation stats appear ~10-15 s after the audio, so ids are polled until
    deadline_s. Best effort: errors or cancellation just return None.
    """
    if not generation_ids:
        return 0.0
    if not api_key:
        return None
    pending = list(generation_ids)
    total = 0.0
    give_up = time.monotonic() + deadline_s
    while pending:
        gen_id = pending[0]
        try:
            status, raw = ig._fetch_json(
                GENERATION_URL.format(id=urllib.parse.quote(gen_id, safe="")),
                ig._openrouter_headers(api_key), COST_LOOKUP_TIMEOUT_S, cancel_event)
        except ig.GenerationCancelled:
            return None
        except (OSError, http.client.HTTPException):
            status, raw = 0, ""
        if status == 200:
            try:
                total += float((json.loads(raw).get("data") or {}).get("total_cost") or 0.0)
            except (ValueError, TypeError, AttributeError):
                return None
            pending.pop(0)
            continue
        if time.monotonic() + poll_s > give_up:
            return None
        if cancel_event is not None and cancel_event.wait(poll_s):
            return None
        if cancel_event is None:
            time.sleep(poll_s)
    return total


# ---------------------------------------------------------------------------
# Story folders + CSV log
# ---------------------------------------------------------------------------

_BAD_NAME_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')


def safe_folder_name(title: str) -> str:
    name = _BAD_NAME_CHARS.sub(" ", title or "")
    name = " ".join(name.split()).strip(" .")[:MAX_TITLE_CHARS].strip(" .")
    return name or "Historia"


def unique_folder(root: Path, name: str) -> Path:
    candidate = root / name
    counter = 2
    while candidate.exists():
        candidate = root / f"{name} ({counter})"
        counter += 1
    return candidate


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def list_storyboards(input_dir: str | Path) -> list[Path]:
    root = Path(input_dir).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"input dir not found: {input_dir}")
    return sorted((p for p in root.iterdir()
                   if p.is_file() and p.suffix.lower() in ig.IMAGE_EXTS),
                  key=lambda p: p.name.casefold())


def load_story_meta(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / STORY_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_stories(output_dir: str | Path) -> list[tuple[Path, dict]]:
    """Story folders in output_dir (with story.json), newest first."""
    root = Path(output_dir).expanduser()
    if not root.is_dir():
        return []
    stories = []
    for folder in root.iterdir():
        if folder.is_dir() and not folder.name.startswith("."):
            meta = load_story_meta(folder)
            if meta is not None:
                stories.append((folder, meta))
    return sorted(stories, key=lambda item: str(item[1].get("created", "")), reverse=True)


def story_style(meta: dict) -> str:
    """Writer style of a story/log row; stories made before styles existed
    were all written the descriptive way."""
    style = str(meta.get("style") or DEFAULT_STYLE)
    return style if style in WRITER_STYLES else DEFAULT_STYLE


def find_existing_story(output_dir: str | Path, sha256: str,
                        style: str | None = None) -> Path | None:
    """Story already made from this image (same SHA-256) - with the same writer
    style when style is given, so a storyboard done in one style can still be
    written in the other."""
    for folder, meta in list_stories(output_dir):
        if meta.get("source_sha256") == sha256 and (style is None or story_style(meta) == style):
            return folder
    return None


def make_preview(source: Path, target: Path) -> bool:
    """PNG copy for Tk (no JPEG/WebP support without Pillow). Best effort."""
    commands = []
    if shutil.which("ffmpeg"):
        commands.append(["ffmpeg", "-loglevel", "error", "-y", "-i", str(source), str(target)])
    if shutil.which("convert"):
        commands.append(["convert", str(source), str(target)])
    for command in commands:
        try:
            subprocess.run(command, capture_output=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError):
            continue
        if target.is_file():
            return True
    return False


def read_log_rows(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    with log_path.open("r", encoding="utf-8", newline="") as fh:
        lines = [ln for ln in fh if not ln.startswith("#")]
    if not lines:
        return []
    return [{k: row.get(k, "") for k in LOG_FIELDS} for row in csv.DictReader(lines)
            if row.get("date") or row.get("title")]


def row_cost(row: dict) -> float:
    """Writer + narration cost of one log row (missing values count as 0)."""
    total = 0.0
    for key in ("writer_cost_usd", "tts_cost_usd"):
        try:
            total += float(row.get(key) or 0)
        except ValueError:
            continue
    return total


_LOG_LOCK = threading.RLock()  # pipeline appends + background cost updates


def write_log_rows(log_path: Path, rows: list[dict]) -> tuple[int, float]:
    """Rewrite the CSV log with fresh totals on top. Returns (rows, total cost)."""
    total_cost = sum(row_cost(row) for row in rows)
    updated = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with log_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# total_stories={len(rows)}; total_cost_usd={total_cost:.6f}; "
                 f"updated_at={updated}\n")
        fh.write("# cost_source=OpenRouter (writer usage.cost + TTS generation total_cost; "
                 "empty tts_cost_usd = not reported yet)\n")
        writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows), total_cost


def append_log_entry(log_path: Path, entry: dict) -> tuple[int, float]:
    with _LOG_LOCK:
        return write_log_rows(log_path, read_log_rows(log_path) + [entry])


# ---------------------------------------------------------------------------
# Core pipeline (shared by CLI and GUI)
# ---------------------------------------------------------------------------

def speech_rates(output_dir: str | Path) -> dict[str, float]:
    """Characters per second of narration, per voice id (+ "*" for all), from
    the real (non dry-run) stories in output_dir. Pauses between scenes are
    excluded. Empty dict when there is no history yet."""
    per_voice: dict[str, list[tuple[int, float]]] = {}
    for _folder, meta in list_stories(output_dir):
        scenes = meta.get("scenes") or []
        try:
            seconds = float(meta.get("audio_seconds") or 0) - SCENE_GAP_S * (len(scenes) - 1)
        except (TypeError, ValueError):
            continue
        chars = sum(len(str(sc.get("narration", ""))) for sc in scenes)
        if meta.get("dry_run") or seconds <= 1 or not chars:
            continue
        per_voice.setdefault(str(meta.get("voice_id", "")), []).append((chars, seconds))
    rates = {vid: sum(c for c, _ in items) / sum(t for _, t in items)
             for vid, items in per_voice.items()}
    everything = [item for items in per_voice.values() for item in items]
    if everything:
        rates["*"] = sum(c for c, _ in everything) / sum(t for _, t in everything)
    return rates


_COST_THREADS: list[threading.Thread] = []


def _fill_cost_later(folder: Path, log_path: Path, ids: list[str], api_key: str | None) -> None:
    cost = lookup_generation_cost(ids, api_key, deadline_s=COST_FILL_DEADLINE_S)
    if cost is None:
        return
    with _LOG_LOCK:
        meta = load_story_meta(folder)
        if meta is not None:
            meta["tts_cost_usd"] = round(cost, 6)
            (folder / STORY_JSON).write_text(json.dumps(meta, indent=2, ensure_ascii=False)
                                             + "\n", encoding="utf-8")
        update_log_tts_cost(log_path, folder.name, cost)


def start_cost_fill(folder: Path, log_path: Path, ids: list[str], api_key: str | None) -> None:
    """Fetch the narration cost in the background (stats lag ~10-15 s)."""
    thread = threading.Thread(target=_fill_cost_later, args=(folder, log_path, ids, api_key),
                              daemon=True)
    thread.start()
    _COST_THREADS.append(thread)


def wait_cost_fills(timeout_s: float = COST_FILL_DEADLINE_S) -> None:
    deadline = time.monotonic() + timeout_s
    while _COST_THREADS:
        thread = _COST_THREADS.pop(0)
        thread.join(max(0.0, deadline - time.monotonic()))


def backfill_free_costs(output_dir: str | Path) -> int:
    """Stories narrated by a ":free" model cost 0: fill empty tts_cost_usd
    (older runs waited for OpenRouter stats). Returns rows fixed."""
    out = Path(output_dir).expanduser()
    fixed = 0
    with _LOG_LOCK:
        for folder, meta in list_stories(out):
            if meta.get("tts_cost_usd") is None and str(meta.get("tts_model", "")).endswith(":free"):
                meta["tts_cost_usd"] = 0.0
                (folder / STORY_JSON).write_text(json.dumps(meta, indent=2, ensure_ascii=False)
                                                 + "\n", encoding="utf-8")
        log_path = out / LOG_FILENAME
        rows = read_log_rows(log_path)
        for row in rows:
            if (row.get("status", "ok") in ("", "ok") and not row.get("tts_cost_usd")
                    and row.get("tts_model", "").endswith(":free")):
                row["tts_cost_usd"] = f"{0.0:.6f}"
                fixed += 1
        if fixed:
            write_log_rows(log_path, rows)
    return fixed


def failed_storyboards(output_dir: str | Path, style: str | None = None) -> list[Path]:
    """Storyboards whose LAST attempt (per writer style) is an error and that
    still have no story in that style (and still exist on disk) - what
    "Retry failed" / --retry-failed redo. style limits it to one style."""
    out = Path(output_dir).expanduser()
    last: dict[tuple[str, str], str] = {}
    for row in read_log_rows(out / LOG_FILENAME):
        if row.get("source_image"):
            last[(row["source_image"], story_style(row))] = row.get("status") or "ok"
    done = {(meta.get("source_sha256"), story_style(meta)) for _f, meta in list_stories(out)}
    failed: dict[str, Path] = {}
    for (source, row_style), state in last.items():
        path = Path(source)
        if (state == "error" and (style is None or row_style == style) and path.is_file()
                and (file_sha256(path), row_style) not in done):
            failed[source] = path
    return sorted(failed.values(), key=lambda p: p.name.casefold())


def generate_story(
    image_path: str | Path,
    *,
    output_dir: str,
    writer_model: str = WRITER_DEFAULT_MODEL,
    tts_model: str = DEFAULT_TTS_MODEL,
    voices: list[dict],
    language: str = DEFAULT_LANGUAGE,
    openrouter_key: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    cancel_event: threading.Event | None = None,
    timeout_s: int = TTS_TIMEOUT_S,
    style: str = DEFAULT_STYLE,
    target_s: float | None = None,
    rates: dict[str, float] | None = None,
    status: StatusFn | None = None,
) -> dict:
    """Storyboard image -> story folder. Returns a result dict.

    Everything is built in memory and written to a hidden temp folder that is
    renamed at the end, so cancel/error leaves nothing behind (raises
    ig.GenerationCancelled on cancel; errors are logged by run_story_batch).
    A storyboard already turned into a story (same SHA-256) is skipped unless
    force=True. status(msg) reports each step (writer, narration, saving).
    """
    source = Path(image_path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"storyboard not found: {image_path}")
    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    sha = file_sha256(source)
    if not force:
        existing = find_existing_story(out, sha, style)
        if existing is not None:
            return {"skipped": True, "folder": str(existing), "source": str(source),
                    "style": style,
                    "title": (load_story_meta(existing) or {}).get("title", existing.name)}

    def check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ig.GenerationCancelled("generation cancelled")

    t0 = time.perf_counter()
    _say(status, f"{source.name}: sending storyboard to the writer ({writer_model})")
    story, writer_cost = write_story(source, api_key=openrouter_key, model=writer_model,
                                     voices=voices, language=language,
                                     cancel_event=cancel_event, dry_run=dry_run, style=style,
                                     target_s=target_s, rates=rates, status=status)
    check_cancel()
    scene_total = len(story["scenes"])
    writer_used = story.get("writer_model") or parse_model_chain(writer_model)[0]
    _say(status, f"writer {writer_used} replied: \u201c{story['title']}\u201d ({scene_total} scenes, "
                 f"voice {story.get('voice_label') or story.get('voice_id') or '-'})")
    clips = []
    generation_ids = []
    for index, scene in enumerate(story["scenes"], start=1):
        check_cancel()
        _say(status, f"narrating scene {index}/{scene_total} "
                     f"({story.get('voice_label') or tts_model})")
        clip, generation_id = synthesize(scene["narration"], api_key=openrouter_key,
                                         voice_id=story["voice_id"], tts_model=tts_model,
                                         timeout_s=timeout_s, cancel_event=cancel_event,
                                         dry_run=dry_run, status=status)
        clips.append(clip)
        if generation_id:
            generation_ids.append(generation_id)
    _say(status, "joining the scene audio and saving the story folder")
    params, pcm, times = join_scene_audio(clips)
    for scene, (start, end) in zip(story["scenes"], times):
        scene["start"], scene["end"] = start, end
    audio_seconds = len(pcm) / (params[0] * params[1]) / params[2]
    check_cancel()
    # ":free" narration costs 0; paid ones are looked up in the background
    # right after saving (OpenRouter stats lag ~10-15 s).
    tts_cost = 0.0 if (not generation_ids or tts_model.endswith(":free")) else None

    tmp = Path(tempfile.mkdtemp(prefix=".story-", dir=out))
    try:
        ext = source.suffix.lower() or ".png"
        shutil.copy2(source, tmp / f"{STORYBOARD_STEM}{ext}")
        if ext not in (".png", ".gif"):
            make_preview(source, tmp / PREVIEW_PNG)
        write_wav(tmp / AUDIO_WAV, *params, pcm)
        (tmp / SCRIPT_MD).write_text(script_markdown(story, language), encoding="utf-8")
        created = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        meta = {**story, "language": language, "style": style,
                "target_seconds": target_s, "writer_model": writer_used,
                "writer_chain": parse_model_chain(writer_model),
                "tts_model": tts_model, "source_image": str(source),
                "source_sha256": sha, "storyboard": f"{STORYBOARD_STEM}{ext}",
                "audio": AUDIO_WAV, "script": SCRIPT_MD,
                "audio_seconds": round(audio_seconds, 3), "created": created,
                "writer_cost_usd": round(writer_cost, 6),
                "tts_cost_usd": None if tts_cost is None else round(tts_cost, 6),
                "tts_generation_ids": generation_ids, "dry_run": dry_run}
        (tmp / STORY_JSON).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8")
        check_cancel()
        folder = unique_folder(out, safe_folder_name(story["title"]))
        os.replace(tmp, folder)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    elapsed = time.perf_counter() - t0
    entry = {
        "date": created,
        "status": "ok",
        "title": story["title"],
        "folder": folder.name,
        "source_image": str(source),
        "scenes": str(scene_total),
        "voice_id": story["voice_id"],
        "voice_label": story["voice_label"],
        "language": language,
        "style": style,
        "writer_model": writer_used,
        "writer_effort": str(story.get("writer_effort") or ""),
        "tts_model": tts_model,
        "target_seconds": "" if target_s is None else f"{target_s:.0f}",
        "narration_chars": str(sum(len(s["narration"]) for s in story["scenes"])),
        "audio_seconds": f"{audio_seconds:.2f}",
        "writer_cost_usd": f"{writer_cost:.6f}",
        "tts_cost_usd": "" if tts_cost is None else f"{tts_cost:.6f}",
        "total_seconds": f"{elapsed:.2f}",
        "key_hash": ig.key_hash(openrouter_key),
        "error": "",
    }
    log_path = out / LOG_FILENAME
    total_ops, total_cost = append_log_entry(log_path, entry)
    if tts_cost is None:
        start_cost_fill(folder, log_path, generation_ids, openrouter_key)
    return {"skipped": False, "folder": str(folder), "source": str(source),
            "title": story["title"], "story": meta, "entry": entry,
            "log_path": str(log_path), "elapsed": elapsed,
            "cost": writer_cost + (tts_cost or 0.0), "generation_ids": generation_ids,
            "total_ops": total_ops, "total_cost": total_cost}


def log_failure(output_dir: str | Path, image: Path, error: str, settings: dict) -> None:
    """Error row in the CSV log (so the list shows what failed and why)."""
    target = settings.get("target_s")
    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    append_log_entry(out / LOG_FILENAME, {
        "date": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "error",
        "title": "",
        "folder": "",
        "source_image": str(image),
        "language": str(settings.get("language") or DEFAULT_LANGUAGE),
        "style": str(settings.get("style") or DEFAULT_STYLE),
        "writer_model": str(settings.get("writer_model") or WRITER_DEFAULT_MODEL),
        "tts_model": str(settings.get("tts_model") or DEFAULT_TTS_MODEL),
        "target_seconds": "" if not target else f"{float(target):.0f}",  # type: ignore[arg-type]
        "key_hash": ig.key_hash(settings.get("openrouter_key")),  # type: ignore[arg-type]
        "error": " ".join(error.split())[:1000],
    })


def run_story_batch(
    images: list[Path],
    *,
    voices: list[dict],
    dry_run: bool = False,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[dict], None] | None = None,
    on_status: StatusFn | None = None,
    **kwargs: object,
) -> dict:
    """One story per storyboard. Per-image failures are logged (status=error,
    with the reason) and the batch goes on; cancellation stops everything
    (ig.GenerationCancelled, nothing logged for the unfinished storyboard).
    Paid narration costs are waited for (up to COST_FILL_DEADLINE_S) at the end.
    """
    if not images:
        raise ValueError("no storyboard images (.png/.jpg/.jpeg/.webp/.gif) found")
    voices = validate_voices(voices, allow_empty=dry_run)
    output_dir = str(kwargs.get("output_dir") or default_output_dir())
    if not dry_run:
        _say(on_status, "checking the writer model")
        check_writer_model(str(kwargs.get("writer_model") or WRITER_DEFAULT_MODEL))
        backfill_free_costs(output_dir)
        _say(on_status, "reading the voices")
    described = describe_voices(voices, dry_run, cancel_event)
    rates = speech_rates(output_dir)
    results: list[dict] = []
    errors: list[dict] = []
    for index, image in enumerate(images):
        try:
            result = generate_story(image, voices=described, dry_run=dry_run,
                                    cancel_event=cancel_event, rates=rates,
                                    status=on_status, **kwargs)  # type: ignore[arg-type]
        except ig.GenerationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - reported per storyboard
            message = f"{type(exc).__name__}: {exc}"
            errors.append({"source": str(image), "error": message})
            log_failure(output_dir, image, message, kwargs)
            _say(on_status, f"{image.name} failed: {message[:160]}")
            result = None
        else:
            results.append(result)
            if not result.get("skipped"):
                rates = speech_rates(output_dir)  # recalibrate with the new story
        if on_progress is not None:
            on_progress({"done": index + 1, "total": len(images), "result": result,
                         "errors": list(errors)})
    if _COST_THREADS:
        _say(on_status, "stories saved - waiting for the narration cost from OpenRouter...")
        wait_cost_fills()
    for result in results:
        if result.get("skipped"):
            continue
        meta = load_story_meta(Path(result["folder"])) or {}
        tts_cost = meta.get("tts_cost_usd")
        if tts_cost is not None:
            result["entry"]["tts_cost_usd"] = f"{float(tts_cost):.6f}"
            result["cost"] = float(meta.get("writer_cost_usd") or 0) + float(tts_cost)
    return {"results": results, "errors": errors,
            "created": [r for r in results if not r.get("skipped")],
            "skipped": [r for r in results if r.get("skipped")],
            "cost": sum(r.get("cost", 0.0) for r in results)}


def _trash_or_delete(path: Path) -> str:
    """Move path to the desktop Trash (gio) so it can be restored; if that is
    not possible, delete it. Returns "trash" or "deleted"."""
    if shutil.which("gio"):
        try:
            subprocess.run(["gio", "trash", str(path)], capture_output=True, timeout=30,
                           check=True)
            if not path.exists():
                return "trash"
        except (OSError, subprocess.SubprocessError):
            pass
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return "deleted"


def delete_story(folder: str | Path, audio_only: bool = False) -> str:
    """Remove a production: the whole story folder (script, audio, story.json
    and the storyboard *copy*) or only its audio. The original storyboard in
    the input dir is never touched. The log row of the story is marked
    (status "deleted" / "no audio"). Returns "trash" or "deleted".
    Raises FileNotFoundError / ValueError for a folder that is not a story.
    """
    folder = Path(folder).expanduser()
    meta = load_story_meta(folder)
    if meta is None:
        raise ValueError(f"not a story folder (no {STORY_JSON}): {folder}")
    log_path = folder.parent / LOG_FILENAME
    with _LOG_LOCK:
        if audio_only:
            audio = folder / str(meta.get("audio") or AUDIO_WAV)
            if not audio.is_file():
                raise FileNotFoundError(f"this story has no audio anymore: {audio}")
            method = _trash_or_delete(audio)
            meta["audio_deleted"] = True
            (folder / STORY_JSON).write_text(json.dumps(meta, indent=2, ensure_ascii=False)
                                             + "\n", encoding="utf-8")
            new_status = "no audio"
        else:
            method = _trash_or_delete(folder)
            new_status = "deleted"
        rows = read_log_rows(log_path)
        for row in reversed(rows):
            if row.get("folder") == folder.name and (row.get("status") or "ok") != "error":
                row["status"] = new_status
                break
        if rows:
            write_log_rows(log_path, rows)
    return method


def update_log_tts_cost(log_path: Path, folder_name: str, cost: float) -> None:
    """Fill tts_cost_usd of the log row for folder_name (totals recomputed)."""
    with _LOG_LOCK:
        rows = read_log_rows(log_path)
        changed = False
        for row in rows:
            if row.get("folder") == folder_name and not row.get("tts_cost_usd"):
                row["tts_cost_usd"] = f"{cost:.6f}"
                changed = True
        if changed:
            write_log_rows(log_path, rows)


# ---------------------------------------------------------------------------
# Audio player (stdlib: feeds raw PCM to aplay / pw-play / ffplay)
# ---------------------------------------------------------------------------

_APLAY_FORMATS = {1: "U8", 2: "S16_LE", 3: "S24_3LE", 4: "S32_LE"}
_PW_FORMATS = {1: "u8", 2: "s16", 3: "s24", 4: "s32"}
_FF_FORMATS = {1: "u8", 2: "s16le", 3: "s24le", 4: "s32le"}


def player_command(channels: int, width: int, rate: int) -> list[str] | None:
    """Command that plays raw PCM from stdin, or None when no player exists."""
    if shutil.which("aplay"):
        return ["aplay", "-q", "-t", "raw", "-f", _APLAY_FORMATS[width],
                "-c", str(channels), "-r", str(rate), "-"]
    if shutil.which("pw-play"):
        return ["pw-play", "--format", _PW_FORMATS[width], "--channels", str(channels),
                "--rate", str(rate), "-"]
    if shutil.which("ffplay"):
        return ["ffplay", "-loglevel", "quiet", "-nodisp", "-autoexit",
                "-f", _FF_FORMATS[width], "-ch_layout", "mono" if channels == 1 else "stereo",
                "-ar", str(rate), "-i", "-"]
    return None


class WavPlayer:
    """Seekable WAV player. Position follows the wall clock while playing;
    pause/seek kill the player process (instant stop) and restart it."""

    LEAD_S = 0.25   # how far ahead of the clock PCM is written
    CHUNK_S = 0.05

    def __init__(self, path: str | Path, on_end: Callable[[], None] | None = None,
                 command: list[str] | None = None) -> None:
        with wave.open(str(path), "rb") as src:
            self.channels = src.getnchannels()
            self.width = src.getsampwidth()
            self.rate = src.getframerate()
            self.pcm = src.readframes(src.getnframes())
        self.frame_bytes = self.channels * self.width
        self.total_frames = len(self.pcm) // self.frame_bytes
        self.duration = self.total_frames / self.rate if self.rate else 0.0
        self.on_end = on_end
        self.command = command if command is not None else player_command(
            self.channels, self.width, self.rate)
        self._lock = threading.Lock()
        self._pos_frames = 0
        self._playing = False
        self._generation = 0
        self._t0 = 0.0
        self._start_frames = 0
        self._proc: subprocess.Popen | None = None

    @property
    def available(self) -> bool:
        return self.command is not None

    @property
    def playing(self) -> bool:
        return self._playing

    def position(self) -> float:
        with self._lock:
            if not self._playing:
                return self._pos_frames / self.rate
            now = self._start_frames / self.rate + (time.monotonic() - self._t0)
            return min(now, self.duration)

    def play(self) -> None:
        if not self.available:
            raise RuntimeError("no audio player found (install alsa-utils: aplay, "
                               "or pipewire: pw-play, or ffmpeg: ffplay)")
        with self._lock:
            if self._playing:
                return
            if self._pos_frames >= self.total_frames:
                self._pos_frames = 0
            self._generation += 1
            generation = self._generation
            self._playing = True
            self._start_frames = self._pos_frames
            self._t0 = time.monotonic()
        threading.Thread(target=self._feed, args=(generation,), daemon=True).start()

    def pause(self) -> None:
        with self._lock:
            if not self._playing:
                return
            now = self._start_frames / self.rate + (time.monotonic() - self._t0)
            self._pos_frames = min(self.total_frames, int(now * self.rate))
            self._playing = False
            self._generation += 1
            proc, self._proc = self._proc, None
        self._kill(proc)

    def toggle(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def seek(self, seconds: float) -> None:
        was_playing = self._playing
        if was_playing:
            self.pause()
        with self._lock:
            self._pos_frames = max(0, min(self.total_frames, int(seconds * self.rate)))
        if was_playing:
            self.play()

    def skip(self, delta_s: float) -> None:
        self.seek(self.position() + delta_s)

    def stop(self) -> None:
        self.pause()
        with self._lock:
            self._pos_frames = 0

    close = stop

    @staticmethod
    def _kill(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:  # broken pipe after kill
            pass

    def _feed(self, generation: int) -> None:
        assert self.command is not None
        try:
            proc = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            with self._lock:
                if generation == self._generation:
                    self._playing = False
            return
        with self._lock:
            if generation != self._generation:
                self._kill(proc)
                return
            self._proc = proc
            frame = self._start_frames
            t0 = self._t0
            start = self._start_frames
        chunk = max(1, int(self.rate * self.CHUNK_S))
        try:
            while frame < self.total_frames:
                if generation != self._generation:
                    return
                ahead = (frame - start) / self.rate - (time.monotonic() - t0)
                if ahead > self.LEAD_S:
                    time.sleep(min(0.02, ahead - self.LEAD_S))
                    continue
                end = min(self.total_frames, frame + chunk)
                assert proc.stdin is not None
                proc.stdin.write(self.pcm[frame * self.frame_bytes:end * self.frame_bytes])
                proc.stdin.flush()
                frame = end
            assert proc.stdin is not None
            proc.stdin.close()
            while generation == self._generation and proc.poll() is None:
                time.sleep(0.02)
        except (OSError, ValueError):
            pass
        with self._lock:
            if generation != self._generation:
                return
            self._playing = False
            self._pos_frames = self.total_frames
            self._proc = None
        if self.on_end is not None:
            self.on_end()


def scene_at(scenes: list[dict], seconds: float) -> int:
    """Index of the scene playing at `seconds` (gaps belong to the previous one)."""
    current = 0
    for index, scene in enumerate(scenes):
        if seconds + 1e-6 >= float(scene.get("start", 0.0)):
            current = index
    return current


def format_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def find_storyboard_image(folder: Path, meta: dict) -> Path | None:
    """Image the GUI can show: preview.png, else a PNG/GIF storyboard."""
    preview = folder / PREVIEW_PNG
    if preview.is_file():
        return preview
    name = meta.get("storyboard")
    candidates = [folder / name] if name else []
    candidates += sorted(folder.glob(f"{STORYBOARD_STEM}.*"))
    for path in candidates:
        if path.is_file():
            return path
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Storyboard images -> written story + narrated audio "
                    "(OpenRouter writer + Fish Audio voices via OpenRouter; CLI + Tkinter GUI).")
    parser.add_argument("--input-dir", default="", help="Folder with storyboard images.")
    parser.add_argument("--image", action="append", default=[],
                        help="One storyboard image (repeatable; instead of --input-dir).")
    parser.add_argument("--output-dir", default=None,
                        help=f"Where story folders + {LOG_FILENAME} go "
                             "(default: StoryGenerate in user pictures folder).")
    parser.add_argument("--writer-model", default=None,
                        help=f"OpenRouter vision chat model (default {WRITER_DEFAULT_MODEL}). "
                             "Fallback chain with ';': 'first;fallback;fallback-of-fallback' - "
                             "each model gets its 3 retries before the next one is tried.")
    parser.add_argument("--tts-model", default=None, choices=TTS_MODELS,
                        help=f"Fish Audio TTS model on OpenRouter (default {DEFAULT_TTS_MODEL}, "
                             "free; the others are paid per character).")
    parser.add_argument("--language", default=None,
                        help=f"Story language (default {DEFAULT_LANGUAGE}).")
    parser.add_argument("--style", default=None, choices=sorted(WRITER_STYLES),
                        help="Writer behaviour: descriptive = faithful to the panels "
                             "(default); connective = fills the gaps between panels "
                             "(why/how the character got from one scene to the next).")
    parser.add_argument("--duration", default=None, metavar="LENGTH",
                        help="Target narration length, e.g. 90, 1:30 or 2m (empty = "
                             "automatic). Converted to words per voice with the speech "
                             "speed measured on your previous stories.")
    parser.add_argument("--delete", default="", metavar="FOLDER",
                        help="Delete a produced story folder (script + audio; moved to the "
                             "Trash when possible). The original storyboard is kept.")
    parser.add_argument("--audio-only", action="store_true",
                        help="With --delete: remove only the audio of that story.")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Redo only the storyboards whose last attempt failed "
                             "(read from the log of the output dir).")
    parser.add_argument("--voice", action="append", default=[], metavar="ID[=LABEL]",
                        help=f"Fish Audio voice id (repeat up to {MAX_VOICES}); the writer "
                             "picks the best one per story. Default: voices saved in the GUI.")
    parser.add_argument("--openrouter-key", default="",
                        help="OpenRouter key (writer + narrator; else env var, else vault).")
    parser.add_argument("--remember-key", action="store_true",
                        help="Encrypt and remember --openrouter-key (vault shared with "
                             "ImageGenerate).")
    parser.add_argument("--forget-key", action="store_true",
                        help="Delete the remembered OpenRouter key and exit (also used by "
                             "ImageGenerate).")
    parser.add_argument("--force", action="store_true",
                        help="Redo storyboards that already have a story (same image hash).")
    parser.add_argument("--timeout", type=int, default=TTS_TIMEOUT_S)
    parser.add_argument("--dry-run", action="store_true",
                        help="Offline: placeholder story + silent audio, no key, no cost.")
    parser.add_argument("--check-voices", action="store_true",
                        help="Print Fish Audio info for the configured voices and exit "
                             "(public, no key).")
    parser.add_argument("--list", action="store_true", help="List stories in the output dir.")
    parser.add_argument("--play", default="", metavar="FOLDER",
                        help="Play a story folder in the terminal (Ctrl+C stops).")
    parser.add_argument("--gui", action="store_true", help="Force GUI mode.")
    return parser


def settings_from_args(args: argparse.Namespace) -> dict:
    """Precedence: hard defaults < story_config.json < explicit CLI flags."""
    saved = sanitize_config(load_config())
    voices = [parse_voice_spec(spec) for spec in args.voice] if args.voice else saved["voices"]
    duration = args.duration if args.duration is not None else saved.get("duration", "")
    return {
        "output_dir": args.output_dir or saved.get("output_dir") or default_output_dir(),
        "writer_model": args.writer_model or saved.get("writer_model") or WRITER_DEFAULT_MODEL,
        "tts_model": args.tts_model or saved.get("tts_model") or DEFAULT_TTS_MODEL,
        "language": args.language or saved.get("language") or DEFAULT_LANGUAGE,
        "style": args.style or saved.get("style") or DEFAULT_STYLE,
        "target_s": parse_duration(duration),
        "voices": voices,
    }


def play_in_terminal(folder: Path) -> int:
    meta = load_story_meta(folder)
    if meta is None:
        print(f"error: no {STORY_JSON} in {folder}", file=sys.stderr)
        return 2
    player = WavPlayer(folder / meta.get("audio", AUDIO_WAV))
    if not player.available:
        print("error: no audio player found (aplay / pw-play / ffplay)", file=sys.stderr)
        return 2
    scenes = meta.get("scenes", [])
    word = scene_word(meta.get("language", ""))
    print(f"# {meta.get('title', folder.name)}  ({format_clock(player.duration)}) "
          "- Ctrl+C stops")
    shown = -1
    player.play()
    try:
        while player.playing:
            index = scene_at(scenes, player.position()) if scenes else -1
            if index != shown and scenes:
                shown = index
                scene = scenes[index]
                print(f"\n[{format_clock(scene.get('start', 0))}] {word} {scene['number']}"
                      + (f" - {scene['heading']}" if scene.get("heading") else ""))
                print(scene["narration"])
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        player.stop()
    return 0


def main_cli(args: argparse.Namespace) -> int:
    if args.forget_key:
        removed = ig.forget_remembered_key(OPENROUTER_PROVIDER)
        print(f"{'forgot' if removed else 'no'} remembered openrouter key")
        return 0
    try:
        settings = settings_from_args(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.remember_key:
        if not args.openrouter_key:
            print("error: nothing to remember (pass --openrouter-key)", file=sys.stderr)
            return 2
        try:
            print(f"remembered openrouter key in "
                  f"{ig.save_remembered_key(OPENROUTER_PROVIDER, args.openrouter_key)}")
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.play:
        return play_in_terminal(Path(args.play).expanduser())
    if args.delete:
        try:
            method = delete_story(args.delete, audio_only=args.audio_only)
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        what = "audio of" if args.audio_only else "story"
        print(f"{'moved to the Trash' if method == 'trash' else 'deleted'}: {what} "
              f"{Path(args.delete).expanduser()}")
        return 0
    if args.list:
        stories = list_stories(settings["output_dir"])
        if not stories:
            print(f"no stories in {settings['output_dir']}")
        for folder, meta in stories:
            print(f"{meta.get('created', '')[:19]}  {meta.get('title', folder.name)}  "
                  f"({len(meta.get('scenes', []))} scenes, "
                  f"{format_clock(float(meta.get('audio_seconds', 0)))}, "
                  f"voice {meta.get('voice_label') or meta.get('voice_id')})  {folder}")
        return 0
    if args.check_voices:
        try:
            voices = validate_voices(settings["voices"])
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for voice in describe_voices(voices):
            info = voice.get("title") or "(no info: check the voice id)"
            print(f"{voice['id']}  {voice.get('label') or '-'}  {info}  "
                  f"langs={','.join(voice.get('languages', [])) or '?'}  "
                  f"tags={','.join(voice.get('tags', [])) or '-'}")
        return 0
    try:
        if args.retry_failed:
            images = failed_storyboards(settings["output_dir"], settings["style"])
            if not images:
                print(f"nothing to retry: no failed {settings['style']} storyboard in "
                      f"{Path(settings['output_dir']) / LOG_FILENAME}")
                return 0
        elif args.image:
            images = [Path(p).expanduser() for p in args.image]
        elif args.input_dir:
            images = list_storyboards(args.input_dir)
        else:
            print("error: give --input-dir, --image or --retry-failed (or use --gui)",
                  file=sys.stderr)
            return 2
        validate_voices(settings["voices"], allow_empty=args.dry_run)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    openrouter_key, or_source = resolve_openrouter_key(args.openrouter_key or None)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {len(images)} storyboard(s) | writer="
          f"{settings['writer_model']} tts={settings['tts_model']} (key: {or_source}) "
          f"voices={len(settings['voices'])} lang={settings['language']} "
          f"style={settings['style']} duration="
          f"{format_clock(settings['target_s']) if settings['target_s'] else 'auto'}")
    print("press Ctrl+C to cancel")
    cancel_event = threading.Event()
    prev_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(signum: int, frame: object) -> None:
        cancel_event.set()
        ig.abort_all_http()

    def _progress(info: dict) -> None:
        result = info["result"]
        if result is None:
            print(f"[{info['done']}/{info['total']}] error: {info['errors'][-1]['error']}")
        elif result.get("skipped"):
            print(f"[{info['done']}/{info['total']}] skipped (already done as "
                  f"{result.get('style') or DEFAULT_STYLE}): {result['folder']}")
        else:
            print(f"[{info['done']}/{info['total']}] {result['title']} -> {result['folder']}")

    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        batch = run_story_batch(
            images, voices=settings["voices"], dry_run=args.dry_run,
            cancel_event=cancel_event, on_progress=_progress,
            on_status=lambda message: print(f"  status: {message}", flush=True),
            output_dir=settings["output_dir"], writer_model=settings["writer_model"],
            tts_model=settings["tts_model"], language=settings["language"],
            style=settings["style"], target_s=settings["target_s"],
            openrouter_key=openrouter_key, force=args.force, timeout_s=args.timeout)
    except ig.GenerationCancelled:
        print("cancelled: unfinished story removed, nothing logged")
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
    print(f"done: {len(batch['created'])} created, {len(batch['skipped'])} skipped, "
          f"{len(batch['errors'])} failed | cost ${batch['cost']:.6f} (writer + narration)")
    for error in batch["errors"]:
        print(f"failed: {Path(error['source']).name}: {error['error'][:300]}")
    if batch["errors"]:
        print("retry only the failed ones with: python3 story_generate.py --retry-failed"
              + (f" --output-dir '{settings['output_dir']}'" if args.output_dir else ""))
    return 1 if batch["errors"] and not batch["results"] else 0


# ---------------------------------------------------------------------------
# GUI (tkinter, stdlib)
# ---------------------------------------------------------------------------

def run_gui(defaults: dict | None = None) -> None:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk

    saved = sanitize_config(load_config())
    merged = {**saved, **{k: v for k, v in (defaults or {}).items() if v not in (None, "", [])}}
    root = tk.Tk(className="StoryGenerate")
    root.title("StoryGenerate")
    root.geometry("1100x720")

    style = ttk.Style()
    style.configure("Generate.TButton", background="#86efac", foreground="#052e16",
                    font=("TkDefaultFont", 10, "bold"), padding=6)
    style.map("Generate.TButton", background=[("active", "#a7f3d0"), ("disabled", "#d4f5e6")])
    style.configure("Cancel.TButton", background="#f28b82", foreground="#3a0a0a",
                    font=("TkDefaultFont", 10), padding=6)
    style.map("Cancel.TButton", background=[("active", "#f8a8a0"), ("disabled", "#f5ccc8")])
    style.configure("Player.TButton", font=("TkDefaultFont", 12), padding=4)
    style.configure("Help.TButton", background="#8ab4f8", foreground="#052e16",
                    font=("TkDefaultFont", 10, "bold"), padding=4)
    style.map("Help.TButton", background=[("active", "#b0c8f5"), ("disabled", "#c8d8f5")])

    state: dict = {"running": False, "start": 0.0, "after_id": None, "cancel_event": None,
                   "logo_img": None, "player": None, "story": None, "folder": None,
                   "image": None, "image_src": None, "seeking": False, "shown_scene": -1}

    header = ttk.Frame(root, padding=(8, 8, 8, 0))
    header.pack(fill=tk.X)
    try:
        logo_path = Path(__file__).resolve().parent / "img/logo.png"
        if logo_path.is_file():
            logo = tk.PhotoImage(file=str(logo_path))
            if logo.width() > 48 or logo.height() > 48:
                logo = logo.subsample(max(1, logo.width() // 48 + 1),
                                      max(1, logo.height() // 48 + 1))
            state["logo_img"] = logo
            root.iconphoto(True, logo)
            ttk.Label(header, image=logo).pack(side=tk.LEFT, padx=(0, 8))
    except (tk.TclError, OSError):
        state["logo_img"] = None
    ttk.Label(header, text="StoryGenerate", font=("", 14, "bold")).pack(side=tk.LEFT)

    def open_docs() -> None:
        """Open docs.html (repo root) at the StoryGenerate section."""
        import webbrowser

        docs = Path(__file__).resolve().parent / "docs.html"
        if docs.is_file():
            webbrowser.open(docs.as_uri() + "#story")
        else:
            messagebox.showwarning("Docs", f"docs.html not found:\n{docs}")

    help_btn = ttk.Button(header, text="?", width=3, command=open_docs, style="Help.TButton")
    help_btn.pack(side=tk.RIGHT)

    def attach_help(widget: object, text: str) -> None:
        tip: dict = {"window": None}

        def show(_event: object = None) -> None:
            hide()
            window = tk.Toplevel(root)
            window.wm_overrideredirect(True)
            window.wm_attributes("-topmost", True)
            ttk.Label(window, text=text, wraplength=300, justify=tk.LEFT,
                      background="#ffffe0", relief=tk.SOLID, borderwidth=1).pack(padx=2, pady=2)
            x = widget.winfo_rootx() + 16  # type: ignore[attr-defined]
            y = widget.winfo_rooty() + widget.winfo_height() + 4  # type: ignore[attr-defined]
            window.wm_geometry(f"+{x}+{y}")
            tip["window"] = window

        def hide(_event: object = None) -> None:
            if tip.get("window") is not None:
                try:
                    tip["window"].destroy()
                except tk.TclError:
                    pass
                tip["window"] = None

        widget.bind("<Enter>", show)  # type: ignore[attr-defined]
        widget.bind("<Leave>", hide)  # type: ignore[attr-defined]

    attach_help(help_btn, "Open the documentation (docs.html) at the StoryGenerate section.")

    def pick_dir(var: tk.StringVar) -> None:
        typed = var.get().strip()
        start = ig.nearest_existing_dir(typed)
        if typed and (start is None or start != Path(typed).expanduser()):
            messagebox.showerror(
                "Folder not found",
                f"Folder does not exist:\n{typed}\n\n"
                + (f"Opening the nearest existing parent:\n{start}" if start
                   else "Opening the default folder."))
        chosen = filedialog.askdirectory(**({"initialdir": str(start)} if start else {}))
        if chosen:
            var.set(chosen)

    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
    tab_generate = ttk.Frame(notebook, padding=8)
    tab_model = ttk.Frame(notebook, padding=8)
    tab_voices = ttk.Frame(notebook, padding=8)
    tab_player = ttk.Frame(notebook, padding=8)
    for frame, name in ((tab_generate, "Generate"), (tab_model, "Model"),
                        (tab_voices, "Voices"), (tab_player, "Player")):
        notebook.add(frame, text=name)

    in_var = tk.StringVar(value=str(merged.get("input_dir", "")))
    out_var = tk.StringVar(value=str(merged.get("output_dir") or default_output_dir()))
    writer_var = tk.StringVar(value=str(merged.get("writer_model") or WRITER_DEFAULT_MODEL))
    tts_var = tk.StringVar(value=str(merged.get("tts_model") or DEFAULT_TTS_MODEL))
    lang_var = tk.StringVar(value=str(merged.get("language") or DEFAULT_LANGUAGE))
    dry_var = tk.BooleanVar(value=bool(merged.get("dry_run", False)))
    force_var = tk.BooleanVar(value=bool(merged.get("force", False)))
    style_var = tk.StringVar(value=style_label(str(merged.get("style") or DEFAULT_STYLE)))
    duration_var = tk.StringVar(value=str(merged.get("duration") or ""))
    or_key, or_source = resolve_openrouter_key()
    or_key_var = tk.StringVar(value=or_key or "")
    or_remember = tk.BooleanVar(value=or_source == "vault")

    # ---- Generate tab ----
    for row, (label, var, hint) in enumerate((
        ("Storyboards dir:", in_var, "Folder with the storyboard images (.png/.jpg/.webp/.gif). "
                                     "Each image becomes one story."),
        ("Output dir:", out_var, "Where each story folder (title) and "
                                 f"{LOG_FILENAME} are saved. Default: {default_output_dir()}"),
    )):
        ttk.Label(tab_generate, text=label).grid(row=row, column=0, sticky=tk.W, padx=4, pady=2)
        entry = ttk.Entry(tab_generate, textvariable=var, width=70)
        entry.grid(row=row, column=1, sticky=tk.EW, padx=4)
        attach_help(entry, hint)
        ttk.Button(tab_generate, text="Browse",
                   command=lambda v=var: pick_dir(v)).grid(row=row, column=2, padx=4)
    opts = ttk.Frame(tab_generate)
    opts.grid(row=2, column=1, sticky=tk.W, pady=4)
    ttk.Label(opts, text="Language:").pack(side=tk.LEFT, padx=(0, 4))
    ttk.Combobox(opts, textvariable=lang_var, values=LANGUAGES, width=7).pack(side=tk.LEFT)
    ttk.Label(opts, text="Writer:").pack(side=tk.LEFT, padx=(12, 4))
    style_combo = ttk.Combobox(opts, textvariable=style_var, state="readonly", width=28,
                               values=[info["label"] for info in WRITER_STYLES.values()])
    style_combo.pack(side=tk.LEFT)
    attach_help(style_combo,
                "Descritivo: conta o que cada quadro mostra, fiel às imagens.\n"
                "Narrativo: foca nas transições - preenche o que aconteceu entre um quadro "
                "e o próximo (por que/como o personagem chegou lá), conectando a história.")
    ttk.Label(opts, text="Duration:").pack(side=tk.LEFT, padx=(12, 4))
    duration_entry = ttk.Entry(opts, textvariable=duration_var, width=7)
    duration_entry.pack(side=tk.LEFT)
    attach_help(duration_entry,
                "Target narration length: empty = automatic; e.g. 90, 1:30 or 2m. "
                "Converted to words per voice using the speech speed measured on your "
                "previous stories (voices read at different speeds). Expect about ±15%.")
    dry_check = ttk.Checkbutton(opts, text="dry-run", variable=dry_var)
    dry_check.pack(side=tk.LEFT, padx=12)
    attach_help(dry_check, "Offline test: placeholder story and silent audio, no key, no cost.")
    force_check = ttk.Checkbutton(opts, text="redo existing", variable=force_var)
    force_check.pack(side=tk.LEFT)
    attach_help(force_check, "Storyboards that already have a story (same image) are skipped "
                             "to save money. Check to write them again (new folder).")
    tab_generate.columnconfigure(1, weight=1)

    bar = ttk.Frame(tab_generate, padding=(0, 6, 0, 0))
    bar.grid(row=3, column=0, columnspan=3, sticky=tk.EW)
    gen_btn = ttk.Button(bar, text="Generate stories", style="Generate.TButton")
    gen_btn.pack(side=tk.LEFT)
    cancel_btn = ttk.Button(bar, text="Cancel", state=tk.DISABLED, style="Cancel.TButton")
    cancel_btn.pack(side=tk.LEFT, padx=4)
    retry_btn = ttk.Button(bar, text="Retry failed")
    retry_btn.pack(side=tk.LEFT, padx=4)
    attach_help(retry_btn, "Run again only the storyboards whose last attempt failed "
                           "(red rows below) and that still have no story.")
    clock_var = tk.StringVar(value="elapsed: 0.0s")
    ttk.Label(bar, textvariable=clock_var, font=("TkDefaultFont", 11, "bold")).pack(
        side=tk.LEFT, padx=16)
    status_var = tk.StringVar(value="idle")
    status_label = ttk.Label(tab_generate, textvariable=status_var, foreground="#1d4ed8",
                             wraplength=1000, justify=tk.LEFT)
    status_label.grid(row=4, column=0, columnspan=3, sticky=tk.W, padx=4, pady=(4, 0))
    attach_help(status_label, "Progress (stories done / total) and the current step: "
                              "writer, narration of each scene, saving, cost lookup, retries.")

    log_frame = ttk.LabelFrame(tab_generate, text="Stories log (newest first) - red = failed: "
                                                  "double-click for details / retry, "
                                                  "right-click to copy the row",
                               padding=6)
    log_frame.grid(row=5, column=0, columnspan=3, sticky=tk.NSEW, pady=(6, 0))
    tab_generate.rowconfigure(5, weight=1)
    columns = ("status", "date", "title", "storyboard_dir", "style", "scenes", "voice_label",
               "audio_seconds", "target_seconds", "writer_cost_usd", "tts_cost_usd", "detail")
    headings = {"status": "status", "date": "date", "title": "title / storyboard",
                "storyboard_dir": "storyboards dir", "style": "writer", "scenes": "scenes",
                "voice_label": "voice",
                "audio_seconds": "audio s", "target_seconds": "target s",
                "writer_cost_usd": "writer $", "tts_cost_usd": "narration $",
                "detail": "folder / error"}
    widths = {"status": 62, "date": 108, "title": 170, "storyboard_dir": 160, "style": 82,
              "scenes": 48,
              "voice_label": 105, "audio_seconds": 55, "target_seconds": 58,
              "writer_cost_usd": 68, "tts_cost_usd": 80, "detail": 250}
    # Treeview cells are single-line: columns auto-fit their content (with a
    # horizontal scrollbar); right-click -> "Copy row" copies a row in full.
    max_widths = {"title": 320, "storyboard_dir": 260, "voice_label": 200,
                  "detail": 1400 // 3}  # full text: tooltip / Copy row
    tree_frame = ttk.Frame(log_frame)
    tree_frame.pack(fill=tk.BOTH, expand=True)
    tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=10)
    for col in columns:
        tree.heading(col, text=headings[col])
        # stretch=False: a stretchable column would shrink back to the window
        # width and hide the text; wide content scrolls horizontally instead.
        tree.column(col, width=widths[col], minwidth=40, anchor=tk.W, stretch=False)
    tree.tag_configure("error", background="#fde2e1")
    tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
    tree_xscroll = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=tree.xview)
    tree.configure(yscrollcommand=tree_scroll.set, xscrollcommand=tree_xscroll.set)
    tree.grid(row=0, column=0, sticky=tk.NSEW)
    tree_scroll.grid(row=0, column=1, sticky=tk.NS)
    tree_xscroll.grid(row=1, column=0, sticky=tk.EW)
    tree_frame.rowconfigure(0, weight=1)
    tree_frame.columnconfigure(0, weight=1)
    total_var = tk.StringVar(value="")
    ttk.Label(log_frame, textvariable=total_var).pack(anchor=tk.W, pady=(4, 0))
    log_rows_by_item: dict[str, dict] = {}
    body_font = tkfont.nametofont("TkDefaultFont")
    heading_font = tkfont.nametofont("TkHeadingFont")

    def autofit_columns() -> None:
        """Width of each column = its longest text (heading included), capped."""
        for index, col in enumerate(columns):
            texts = [str(tree.item(item, "values")[index]) for item in tree.get_children()]
            width = max([heading_font.measure(headings[col]) + 20]
                        + [body_font.measure(text) + 18 for text in texts])
            tree.column(col, width=max(40, min(width, max_widths.get(col, 160))))

    def current_out_dir() -> Path:
        return Path(out_var.get().strip() or default_output_dir()).expanduser()

    def storyboard_dir_label(source: str) -> str:
        """Folder of the storyboard (from source_image, already in every log row):
        its name first - what tells folders apart - then where it is, with the
        home folder as ~ (a narrow column cuts the less useful end)."""
        if not source:
            return ""
        folder = Path(source).parent
        parent, home = str(folder.parent), str(Path.home())
        if parent == home or parent.startswith(home + os.sep):
            parent = "~" + parent[len(home):]
        return f"{folder.name}  ({parent})"

    def row_details(row: dict) -> str:
        """A log row as readable "Label: value" lines (what "Copy row" copies)."""
        if row.get("status") == "error":
            pairs = [("Status", "error"),
                     ("Error", row.get("error") or "(no detail)"),
                     ("Storyboard", row.get("source_image", "")),
                     ("Storyboards dir", str(Path(row["source_image"]).parent)
                      if row.get("source_image") else ""),
                     ("When", (row.get("date") or "").replace("T", " ")[:19]),
                     ("Writer", f"{row.get('writer_model')} ({row.get('style') or DEFAULT_STYLE})"),
                     ("Narrator", row.get("tts_model", "")),
                     ("Target", f"{row.get('target_seconds')} s" if row.get("target_seconds")
                      else "automatic")]
        else:
            target = row.get("target_seconds")
            pairs = [("Status", "ok"),
                     ("Title", row.get("title", "")),
                     ("Folder", str(current_out_dir() / row.get("folder", ""))),
                     ("Storyboard", row.get("source_image", "")),
                     ("Storyboards dir", str(Path(row["source_image"]).parent)
                      if row.get("source_image") else ""),
                     ("When", (row.get("date") or "").replace("T", " ")[:19]),
                     ("Writer", f"{row.get('writer_model')} ({row.get('style') or DEFAULT_STYLE}"
                                + (f", thinking {row['writer_effort']}" if row.get("writer_effort")
                                   else "") + ")"),
                     ("Narration", f"{row.get('voice_label') or row.get('voice_id') or '-'} via "
                                   f"{row.get('tts_model')} - {row.get('audio_seconds')} s"
                                   + (f" (target {target} s)" if target else "")
                                   + f", {row.get('scenes')} scenes"),
                     ("Cost", f"writer ${row.get('writer_cost_usd') or '0'} + narration "
                              f"${row.get('tts_cost_usd') or '(pending)'}"),
                     ("Took", f"{row.get('total_seconds')} s")]
        return "\n".join(f"{label}: {value}" for label, value in pairs)

    def copy_row(row: dict) -> None:
        root.clipboard_clear()
        root.clipboard_append(row_details(row))
        status_var.set("row copied to the clipboard")

    def refresh_log() -> None:
        for child in tree.get_children():
            tree.delete(child)
        log_rows_by_item.clear()
        log_path = current_out_dir() / LOG_FILENAME
        rows = list(reversed(read_log_rows(log_path)))
        total = 0.0
        failed_now = {str(p) for p in failed_storyboards(current_out_dir(),
                                                          style_from_label(style_var.get()))}
        for row in rows:
            total += row_cost(row)
            failed = row.get("status") == "error"
            values = []
            for col in columns:
                if col == "status":
                    values.append({"error": "✖ error", "deleted": "🗑 deleted",
                                   "no audio": "🔇 no audio"}.get(row.get("status") or "ok",
                                                                 "✔ ok"))
                elif col == "date":
                    values.append((row.get("date") or "")[5:16].replace("T", " "))
                elif col == "title":
                    values.append(row.get("title") or Path(row.get("source_image", "")).name)
                elif col == "storyboard_dir":
                    values.append(storyboard_dir_label(row.get("source_image", "")))
                elif col == "style":
                    values.append(row.get("style") or DEFAULT_STYLE)
                elif col == "detail":
                    values.append(row.get("error", "") if failed else row.get("folder", ""))
                else:
                    values.append(row.get(col) or "")
            item = tree.insert("", tk.END, values=tuple(values),
                               tags=("error",) if failed else ())
            log_rows_by_item[item] = row
        autofit_columns()
        failures = len(failed_now)
        retry_btn.configure(text=f"Retry failed ({failures})" if failures else "Retry failed",
                            state=tk.NORMAL if failures and not state["running"] else tk.DISABLED)
        total_var.set(f"total: {sum(1 for r in rows if (r.get('status') or 'ok') in ('ok', 'no audio'))} stories, "
                      f"{sum(1 for r in rows if r.get('status') == 'error')} failed attempts / "
                      f"${total:.6f} (writer + narration)  ({log_path})")

    def selected_row() -> dict | None:
        selection = tree.selection()
        return log_rows_by_item.get(selection[0]) if selection else None

    def open_row_in_player(row: dict) -> None:
        refresh_stories(select=current_out_dir() / row.get("folder", ""))
        notebook.select(tab_player)

    def show_failure(row: dict) -> None:
        source = Path(row.get("source_image", ""))
        if messagebox.askyesno(
                "Storyboard failed",
                f"{source.name}\n{row.get('date', '')[:19]}  writer {row.get('writer_model')}\n\n"
                f"{row.get('error') or '(no detail)'}\n\nRetry this storyboard now "
                f"({style_label(story_style(row))})?"):
            start_batch([source], retry=True, style=story_style(row))

    def on_row_double_click(_event: object = None) -> None:
        hide_cell_tip()
        row = selected_row()
        if row is None:
            return
        if row.get("status") == "error":
            show_failure(row)
        else:
            open_row_in_player(row)

    tree.bind("<Double-1>", on_row_double_click)
    row_menu = tk.Menu(root, tearoff=0)

    def show_row_menu(event: object) -> None:
        hide_cell_tip()
        item = tree.identify_row(event.y)  # type: ignore[attr-defined]
        if not item:
            return
        tree.selection_set(item)
        row = log_rows_by_item[item]
        row_menu.delete(0, tk.END)
        row_menu.add_command(label="Copy row", command=lambda: copy_row(row))
        row_menu.add_separator()
        if row.get("status") == "error":
            row_menu.add_command(label="Show error", command=lambda: show_failure(row))
            row_menu.add_command(label="Retry this storyboard",
                                 command=lambda: start_batch([Path(row["source_image"])],
                                                             retry=True,
                                                             style=story_style(row)))
        elif row.get("status") == "deleted":
            row_menu.add_command(label="(production deleted)", state=tk.DISABLED)
        else:
            folder = current_out_dir() / row.get("folder", "")
            row_menu.add_command(label="Open in Player", command=lambda: open_row_in_player(row))
            row_menu.add_command(label="Open folder", command=lambda: ig_open(str(folder)))
            row_menu.add_separator()
            if row.get("status") != "no audio":
                row_menu.add_command(label="Delete audio only…",
                                     command=lambda: on_delete(folder, audio_only=True))
            row_menu.add_command(label="Delete script + audio…",
                                 command=lambda: on_delete(folder, audio_only=False))
        # tk_popup (not post): the menu grabs the pointer, so a click outside it
        # or Esc closes it. No grab_release() here: on X11 tk_popup returns at
        # once and releasing the grab would leave the menu stuck on screen.
        row_menu.tk_popup(event.x_root, event.y_root)  # type: ignore[attr-defined]

    def on_delete(folder: Path, audio_only: bool) -> None:
        if state["running"]:
            messagebox.showinfo("Delete", "Wait for the current generation to finish.")
            return
        meta = load_story_meta(folder) or {}
        title = meta.get("title", folder.name)
        what = ("only the AUDIO (audio.wav) of" if audio_only else
                "the whole production (script, audio, story.json and the storyboard copy) of")
        if not messagebox.askyesno(
                "Delete", f"Delete {what}\n\n\u201c{title}\u201d\n{folder}\n\n"
                          "The original storyboard in the storyboards dir is NOT touched. "
                          "Files go to the Trash when possible."
                          + ("" if audio_only else "\n\nThis storyboard will be written "
                             "again the next time you generate this folder.") + "\n\nDelete?",
                icon=messagebox.WARNING, default=messagebox.NO):
            return
        if state["folder"] is not None and Path(state["folder"]).resolve() == folder.resolve():
            stop_play(release=True)
            state.update(story=None, folder=None, image_src=None)
            render_image()
            fill_script({})
        try:
            method = delete_story(folder, audio_only=audio_only)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Delete", str(exc))
            return
        status_var.set(f"{'moved to the Trash' if method == 'trash' else 'deleted'}: "
                       f"{'audio of ' if audio_only else ''}{title}")
        refresh_log()
        refresh_stories(select=folder if audio_only else None)

    tree.bind("<Button-3>", show_row_menu)

    cell_tip: dict = {"window": None, "key": None, "after": None}

    def hide_cell_tip(_event: object = None) -> None:
        if cell_tip["after"] is not None:
            root.after_cancel(cell_tip["after"])
            cell_tip["after"] = None
        if cell_tip["window"] is not None:
            try:
                cell_tip["window"].destroy()
            except tk.TclError:
                pass
            cell_tip["window"] = None
        cell_tip["key"] = None

    def show_cell_tip(text: str, x: int, y: int) -> None:
        cell_tip["after"] = None
        window = tk.Toplevel(root)
        window.wm_overrideredirect(True)
        window.wm_attributes("-topmost", True)
        ttk.Label(window, text=text, wraplength=520, justify=tk.LEFT, background="#ffffe0",
                  relief=tk.SOLID, borderwidth=1, padding=4).pack()
        window.wm_geometry(f"+{x + 14}+{y + 18}")
        cell_tip["window"] = window

    def on_tree_motion(event: object) -> None:
        """Tooltip with the full text of a cell that is wider than its column."""
        item = tree.identify_row(event.y)  # type: ignore[attr-defined]
        column = tree.identify_column(event.x)  # type: ignore[attr-defined]
        key = (item, column)
        if key == cell_tip["key"]:
            return
        hide_cell_tip()
        cell_tip["key"] = key
        if not item or not column.startswith("#"):
            return
        index = int(column[1:]) - 1
        values = tree.item(item, "values")
        if not 0 <= index < len(values):
            return
        text = str(values[index])
        if body_font.measure(text) + 14 <= tree.column(columns[index], "width"):
            return  # fits: nothing hidden
        x, y = event.x_root, event.y_root  # type: ignore[attr-defined]
        cell_tip["after"] = root.after(450, lambda: show_cell_tip(text, x, y))

    tree.bind("<Motion>", on_tree_motion)
    tree.bind("<Leave>", hide_cell_tip)
    tree.bind("<ButtonPress>", hide_cell_tip, add="+")

    # ---- Model tab ----
    ttk.Label(tab_model, text="Writer model:").grid(row=0, column=0, sticky=tk.W, padx=4, pady=2)
    writer_entry = ttk.Entry(tab_model, textvariable=writer_var, width=60)
    writer_entry.grid(row=0, column=1, sticky=tk.EW, padx=4)
    attach_help(writer_entry, "OpenRouter chat model that SEES the storyboard (vision) and "
                              f"writes the story + picks the voice. Default {WRITER_DEFAULT_MODEL}.\n"
                              "Fallbacks: separate models with ';' - e.g. "
                              "'qwen/qwen3.8-27b:free;google/gemini-3.7-flash' tries the first "
                              "(with its 3 retries) and, if it fails, the next one, and so on. "
                              "The log shows which model wrote each story.")
    ttk.Label(tab_model, text="TTS model:").grid(row=1, column=0, sticky=tk.W, padx=4, pady=2)
    tts_combo = ttk.Combobox(tab_model, textvariable=tts_var, values=TTS_MODELS,
                             state="readonly", width=32)
    tts_combo.grid(row=1, column=1, sticky=tk.W, padx=4)
    attach_help(tts_combo, "Fish Audio voice model served by OpenRouter (same key as the "
                           f"writer). {DEFAULT_TTS_MODEL} is free (no availability "
                           "guarantee); the others are paid per character.")
    ttk.Label(tab_model, text="OpenRouter key:").grid(row=2, column=0, sticky=tk.W, padx=4, pady=2)
    key_row = ttk.Frame(tab_model)
    key_row.grid(row=2, column=1, sticky=tk.EW, padx=4)
    key_entry = ttk.Entry(key_row, textvariable=or_key_var, width=44, show="*")
    key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
    attach_help(key_entry, "One key for the writer AND the narrator. Same OpenRouter "
                           "key/vault as ImageGenerate.")
    remember_check = ttk.Checkbutton(key_row, text="remember me", variable=or_remember)
    remember_check.pack(side=tk.LEFT, padx=8)
    if not ig.HAS_FERNET:
        remember_check.configure(state=tk.DISABLED,
                                 text="remember me (pip install cryptography)")
    forget_btn = ttk.Button(key_row, text="Forget", command=lambda: on_forget())
    forget_btn.pack(side=tk.LEFT)
    attach_help(forget_btn, "Delete the saved OpenRouter key (ImageGenerate uses the same one).")
    tab_model.columnconfigure(1, weight=1)

    def on_forget() -> None:
        removed = ig.forget_remembered_key(OPENROUTER_PROVIDER)
        or_remember.set(False)
        or_key_var.set("")
        status_var.set(f"{'forgot' if removed else 'no'} remembered openrouter key")

    # ---- Voices tab ----
    ttk.Label(tab_voices, text=f"Up to {MAX_VOICES} Fish Audio voices. The writer picks the "
                               "most fitting one for each story.").grid(
        row=0, column=0, columnspan=4, sticky=tk.W, padx=4, pady=(0, 6))
    voice_rows: list[tuple[tk.StringVar, tk.StringVar, tk.StringVar]] = []
    saved_voices = list(merged.get("voices") or [])
    for index in range(MAX_VOICES):
        current = saved_voices[index] if index < len(saved_voices) else {}
        id_var = tk.StringVar(value=current.get("id", ""))
        label_var = tk.StringVar(value=current.get("label", ""))
        info_var = tk.StringVar(value="")
        ttk.Label(tab_voices, text=f"Voice {index + 1}:").grid(
            row=index + 1, column=0, sticky=tk.W, padx=4, pady=2)
        id_entry = ttk.Entry(tab_voices, textvariable=id_var, width=36)
        id_entry.grid(row=index + 1, column=1, sticky=tk.W, padx=4)
        attach_help(id_entry, "Fish Audio voice id: the code in the voice page URL "
                              "(fish.audio/m/<id>).")
        label_entry = ttk.Entry(tab_voices, textvariable=label_var, width=22)
        label_entry.grid(row=index + 1, column=2, sticky=tk.W, padx=4)
        attach_help(label_entry, "Optional label to help the writer choose, e.g. "
                                 "'narradora calma', 'menino animado', 'avô'.")
        ttk.Label(tab_voices, textvariable=info_var, foreground="#555").grid(
            row=index + 1, column=3, sticky=tk.W, padx=4)
        voice_rows.append((id_var, label_var, info_var))
    ttk.Button(tab_voices, text="Check voices", command=lambda: on_check_voices()).grid(
        row=MAX_VOICES + 1, column=1, sticky=tk.W, padx=4, pady=8)
    tab_voices.columnconfigure(3, weight=1)

    def current_voices() -> list[dict]:
        return [{"id": v.get().strip(), "label": lab.get().strip()}
                for v, lab, _info in voice_rows if v.get().strip()]

    def on_check_voices() -> None:
        try:
            voices = validate_voices(current_voices())
        except ValueError as exc:
            messagebox.showerror("Voices", str(exc))
            return
        for _v, _l, info in voice_rows:
            info.set("")

        def work() -> None:
            described = {v["id"]: v for v in describe_voices(voices)}

            def show() -> None:
                for id_var, _l, info in voice_rows:
                    voice = described.get(id_var.get().strip())
                    if voice is None:
                        continue
                    if voice.get("title"):
                        info.set(f"{voice['title']} | {','.join(voice.get('languages', [])) or '?'}"
                                 f" | {', '.join(voice.get('tags', [])[:4])}")
                    else:
                        info.set("not found (check the voice id)")
            root.after(0, show)

        threading.Thread(target=work, daemon=True).start()

    # ---- Player tab ----
    player_top = ttk.Frame(tab_player)
    player_top.pack(fill=tk.BOTH, expand=True)
    list_frame = ttk.Frame(player_top)
    list_frame.pack(side=tk.LEFT, fill=tk.Y)
    ttk.Label(list_frame, text="Stories (newest first)").pack(anchor=tk.W)
    story_list = tk.Listbox(list_frame, width=28, exportselection=False)
    story_list.pack(fill=tk.Y, expand=True)
    ttk.Button(list_frame, text="Refresh", command=lambda: refresh_stories()).pack(
        fill=tk.X, pady=(4, 0))
    ttk.Button(list_frame, text="Open folder",
               command=lambda: state["folder"] and ig_open(str(state["folder"]))).pack(fill=tk.X)
    story_folders: list[Path] = []

    panes = ttk.PanedWindow(player_top, orient=tk.HORIZONTAL)
    panes.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
    image_canvas = tk.Canvas(panes, background="#111", highlightthickness=0, width=620)
    script_frame = ttk.Frame(panes)
    panes.add(image_canvas, weight=3)
    panes.add(script_frame, weight=2)
    script_text = tk.Text(script_frame, wrap=tk.WORD, font=("TkDefaultFont", 11), width=38,
                          padx=10, pady=8, cursor="arrow")
    script_scroll = ttk.Scrollbar(script_frame, orient=tk.VERTICAL, command=script_text.yview)
    script_text.configure(yscrollcommand=script_scroll.set, state=tk.DISABLED)
    script_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    script_text.pack(fill=tk.BOTH, expand=True)
    script_text.tag_configure("title", font=("TkDefaultFont", 15, "bold"), spacing3=6)
    script_text.tag_configure("logline", font=("TkDefaultFont", 10, "italic"),
                              foreground="#666", spacing3=8)
    script_text.tag_configure("heading", font=("TkDefaultFont", 11, "bold"), spacing1=8)
    script_text.tag_configure("current", background="#fff3b0")
    script_text.tag_configure("voice", foreground="#666", spacing1=10)

    controls = ttk.Frame(tab_player, padding=(0, 6, 0, 0))
    controls.pack(fill=tk.X)
    time_var = tk.StringVar(value="0:00 / 0:00")
    seek_var = tk.DoubleVar(value=0.0)
    buttons = [
        ("⏮", "Previous scene", lambda: jump_scene(-1)),
        ("⏪ 10s", "Back 10 seconds (Left arrow: 5 s)", lambda: skip(-10)),
        ("▶", "Play / Pause (Space)", lambda: toggle_play()),
        ("10s ⏩", "Forward 10 seconds (Right arrow: 5 s)", lambda: skip(10)),
        ("⏭", "Next scene", lambda: jump_scene(1)),
        ("⏹", "Stop (back to the start)", lambda: stop_play()),
    ]
    control_buttons = {}
    for text, hint, command in buttons:
        button = ttk.Button(controls, text=text, width=6, style="Player.TButton", command=command)
        button.pack(side=tk.LEFT, padx=2)
        attach_help(button, hint)
        control_buttons[hint] = button
    play_btn = control_buttons["Play / Pause (Space)"]
    seek_scale = ttk.Scale(controls, from_=0.0, to=1.0, orient=tk.HORIZONTAL, variable=seek_var)
    seek_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)
    ttk.Label(controls, textvariable=time_var, width=13).pack(side=tk.LEFT)
    scene_var = tk.StringVar(value="")
    ttk.Label(tab_player, textvariable=scene_var, foreground="#555").pack(anchor=tk.W)

    def ig_open(path: str) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except Exception as exc:  # noqa: BLE001 - report to user
            messagebox.showerror("Open failed", str(exc))

    def refresh_stories(select: Path | None = None) -> None:
        stories = list_stories(out_var.get().strip() or default_output_dir())
        story_list.delete(0, tk.END)
        story_folders.clear()
        for folder, meta in stories:
            story_list.insert(tk.END, meta.get("title", folder.name))
            story_folders.append(folder)
        target = select or state["folder"]
        if target is not None:
            for index, folder in enumerate(story_folders):
                if folder.resolve() == Path(target).resolve():
                    story_list.selection_clear(0, tk.END)
                    story_list.selection_set(index)
                    story_list.see(index)
                    if select is not None:
                        load_story(folder)
                    break

    def render_image(_event: object = None) -> None:
        image_canvas.delete("all")
        width = max(50, image_canvas.winfo_width())
        height = max(50, image_canvas.winfo_height())
        src = state["image_src"]
        if src is None:
            if state["story"] is not None:
                image_canvas.create_text(width // 2, height // 2, fill="#ddd",
                                         text="Storyboard preview unavailable\n"
                                              "(use Open folder)", justify=tk.CENTER)
            return
        factor = max(1, math.ceil(max(src.width() / width, src.height() / height)))
        state["image"] = src.subsample(factor, factor) if factor > 1 else src
        image_canvas.create_image(width // 2, height // 2, image=state["image"])

    image_canvas.bind("<Configure>", render_image)

    def load_story(folder: Path) -> None:
        stop_play(release=True)
        meta = load_story_meta(folder)
        if meta is None:
            messagebox.showerror("Player", f"No {STORY_JSON} in {folder}")
            return
        state["story"], state["folder"], state["shown_scene"] = meta, folder, -1
        image_path = find_storyboard_image(folder, meta)
        try:
            state["image_src"] = tk.PhotoImage(file=str(image_path)) if image_path else None
        except tk.TclError:
            state["image_src"] = None
        render_image()
        fill_script(meta)
        try:
            player = WavPlayer(folder / meta.get("audio", AUDIO_WAV),
                               on_end=lambda: root.after(0, on_play_end))
        except (OSError, wave.Error, EOFError) as exc:
            state["player"] = None
            time_var.set("no audio" if meta.get("audio_deleted") else "audio error")
            status_var.set("this story has no audio (deleted)" if meta.get("audio_deleted")
                           else f"audio error: {exc}")
            return
        state["player"] = player
        seek_scale.configure(to=max(0.1, player.duration))
        seek_var.set(0.0)
        if not player.available:
            messagebox.showwarning("Player", "No audio player found: install aplay "
                                             "(alsa-utils), pw-play or ffplay.")
        update_player_ui()

    def fill_script(meta: dict) -> None:
        word = scene_word(meta.get("language", ""))
        script_text.configure(state=tk.NORMAL)
        script_text.delete("1.0", tk.END)
        script_text.insert(tk.END, meta.get("title", "") + "\n", ("title",))
        if meta.get("logline"):
            script_text.insert(tk.END, meta["logline"] + "\n", ("logline",))
        for index, scene in enumerate(meta.get("scenes", [])):
            tag = f"scene{index}"
            heading = f"{word} {scene['number']}"
            if scene.get("heading"):
                heading += f" — {scene['heading']}"
            heading += f"  ({format_clock(scene.get('start', 0))})"
            script_text.insert(tk.END, heading + "\n", ("heading", tag))
            script_text.insert(tk.END, scene["narration"] + "\n", (tag,))
            script_text.tag_bind(tag, "<Button-1>",
                                 lambda _e, s=scene: seek_to(float(s.get("start", 0))))
        if meta.get("voice_id"):
            script_text.insert(tk.END, f"\nVoz: {meta.get('voice_label') or meta['voice_id']}"
                               + (f" — {meta['voice_reason']}" if meta.get("voice_reason") else ""),
                               ("voice",))
        script_text.configure(state=tk.DISABLED)

    def highlight_scene(index: int) -> None:
        if index == state["shown_scene"]:
            return
        state["shown_scene"] = index
        script_text.tag_remove("current", "1.0", tk.END)
        ranges = script_text.tag_ranges(f"scene{index}")
        if ranges:
            script_text.tag_add("current", ranges[0], ranges[-1])
            script_text.see(ranges[0])
        scenes = (state["story"] or {}).get("scenes", [])
        if 0 <= index < len(scenes):
            scene = scenes[index]
            scene_var.set(f"{scene_word((state['story'] or {}).get('language', ''))} "
                          f"{scene['number']}/{len(scenes)}"
                          + (f" — {scene['heading']}" if scene.get("heading") else ""))

    def update_player_ui() -> None:
        player: WavPlayer | None = state["player"]
        if player is None:
            return
        pos = player.position()
        if not state["seeking"]:
            seek_var.set(pos)
        time_var.set(f"{format_clock(pos)} / {format_clock(player.duration)}")
        play_btn.configure(text="⏸" if player.playing else "▶")
        scenes = (state["story"] or {}).get("scenes", [])
        if scenes:
            highlight_scene(scene_at(scenes, pos))

    def poll_player() -> None:
        if state["player"] is not None and state["player"].playing:
            update_player_ui()
        root.after(100, poll_player)

    def toggle_play() -> None:
        player = state["player"]
        if player is None:
            return
        try:
            player.toggle()
        except RuntimeError as exc:
            messagebox.showerror("Player", str(exc))
        update_player_ui()

    def stop_play(release: bool = False) -> None:
        player = state["player"]
        if player is not None:
            player.stop()
            if release:
                state["player"] = None
            else:
                update_player_ui()

    def seek_to(seconds: float) -> None:
        if state["player"] is not None:
            state["player"].seek(seconds)
            state["shown_scene"] = -1
            update_player_ui()

    def skip(delta: float) -> None:
        if state["player"] is not None:
            seek_to(state["player"].position() + delta)

    def jump_scene(step: int) -> None:
        player = state["player"]
        scenes = (state["story"] or {}).get("scenes", [])
        if player is None or not scenes:
            return
        pos = player.position()
        index = scene_at(scenes, pos)
        # "previous" restarts the current scene unless we are at its very start
        if step < 0 and pos - float(scenes[index].get("start", 0)) > 2.0:
            target = index
        else:
            target = max(0, min(len(scenes) - 1, index + step))
        seek_to(float(scenes[target].get("start", 0)))

    def on_play_end() -> None:
        update_player_ui()

    def on_seek_press(_event: object) -> None:
        state["seeking"] = True

    def on_seek_release(_event: object) -> None:
        state["seeking"] = False
        seek_to(seek_var.get())

    seek_scale.bind("<ButtonPress-1>", on_seek_press)
    seek_scale.bind("<ButtonRelease-1>", on_seek_release)

    def on_story_select(_event: object) -> None:
        selection = story_list.curselection()
        if selection and story_folders[selection[0]] != state["folder"]:
            load_story(story_folders[selection[0]])

    story_list.bind("<<ListboxSelect>>", on_story_select)

    def player_key(action: Callable[[], None]) -> Callable[[object], str | None]:
        def handler(event: object) -> str | None:
            if notebook.select() != str(tab_player):
                return None
            if isinstance(getattr(event, "widget", None), (tk.Entry, ttk.Entry)):
                return None
            action()
            return "break"
        return handler

    root.bind("<space>", player_key(toggle_play))
    root.bind("<Left>", player_key(lambda: skip(-5)))
    root.bind("<Right>", player_key(lambda: skip(5)))
    root.bind("<Up>", player_key(lambda: jump_scene(-1)))
    root.bind("<Down>", player_key(lambda: jump_scene(1)))

    # ---- generation ----
    def tick_clock() -> None:
        if state["running"]:
            clock_var.set(f"elapsed: {time.perf_counter() - state['start']:.1f}s")
            show_status()  # keeps the "(N s)" of the current step counting
            state["after_id"] = root.after(100, tick_clock)

    def persist_config() -> None:
        try:
            save_config({"input_dir": in_var.get().strip(), "output_dir": out_var.get().strip(),
                         "writer_model": writer_var.get().strip(),
                         "tts_model": tts_var.get().strip(),
                         "language": lang_var.get().strip(), "voices": current_voices(),
                         "dry_run": bool(dry_var.get()), "force": bool(force_var.get()),
                         "style": style_from_label(style_var.get()),
                         "duration": duration_var.get().strip()})
        except OSError:
            pass

    def show_status() -> None:
        step = state.get("step") or ""
        since = f" ({time.perf_counter() - state.get('step_start', time.perf_counter()):.0f} s)"
        status_var.set(state.get("progress", "")
                       + (f"  |  status: {step}{since}" if step else ""))

    def on_status(message: str) -> None:
        if state["running"]:
            state["step"] = message
            state["step_start"] = time.perf_counter()
            show_status()

    def on_progress(info: dict) -> None:
        if not state["running"]:
            return
        state["progress"] = (f"generating... {info['done']}/{info['total']} done"
                             + (f" ({len(info['errors'])} failed)" if info["errors"] else ""))
        show_status()
        refresh_log()
        result = info.get("result")
        if result and not result.get("skipped"):
            refresh_stories()

    def set_running(running: bool) -> None:
        state["running"] = running
        gen_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        cancel_btn.configure(state=tk.NORMAL if running else tk.DISABLED)
        if running:
            retry_btn.configure(state=tk.DISABLED)

    def on_done(batch: dict | None, error: str | None, cancelled: bool = False) -> None:
        set_running(False)
        if state["after_id"]:
            try:
                root.after_cancel(state["after_id"])
            except tk.TclError:
                pass
            state["after_id"] = None
        refresh_log()
        if cancelled:
            status_var.set("cancelled: unfinished story removed, nothing logged")
            return
        if error:
            status_var.set(f"error: {error}")
            ig.play_chime("error")
            messagebox.showerror("Story generation failed", error)
            return
        assert batch is not None
        status_var.set(f"finished: {len(batch['created'])} created, "
                       f"{len(batch['skipped'])} skipped, {len(batch['errors'])} failed | "
                       f"${batch['cost']:.6f} (writer + narration)")
        ig.play_chime("error" if batch["errors"] else "success")
        if batch["errors"]:
            messagebox.showwarning(
                "Some storyboards failed",
                "\n\n".join(f"{Path(e['source']).name}: {e['error'][:300]}"
                            for e in batch["errors"][:5])
                + ("\n\n…" if len(batch["errors"]) > 5 else "")
                + "\n\nThey are the red rows in the log. To try again: click "
                  "'Retry failed' (only these), or double-click / right-click a red row.")
        newest = batch["created"][-1]["folder"] if batch["created"] else None
        refresh_stories(select=Path(newest) if newest else None)
        if newest and not batch["errors"]:
            notebook.select(tab_player)

    def worker(images: list[Path], kwargs: dict) -> None:
        try:
            batch = run_story_batch(images, **kwargs)
        except ig.GenerationCancelled:
            root.after(0, lambda: on_done(None, None, True))
        except Exception as exc:  # noqa: BLE001 - show any failure in GUI
            message = f"{type(exc).__name__}: {exc}"
            root.after(0, lambda: on_done(None, message))
        else:
            root.after(0, lambda: on_done(batch, None))

    def start_batch(images: list[Path], retry: bool = False, style: str | None = None) -> None:
        """Validate the settings, confirm the cost and run the batch in a thread.
        style overrides the Writer dropdown (retrying a row keeps its style)."""
        style = style or style_from_label(style_var.get())
        if state["running"] or not images:
            return
        dry_run = bool(dry_var.get())
        try:
            voices = validate_voices(current_voices(), allow_empty=dry_run)
        except ValueError as exc:
            messagebox.showerror("Voices", str(exc))
            notebook.select(tab_voices)
            return
        try:
            target_s = parse_duration(duration_var.get())
        except ValueError as exc:
            messagebox.showerror("Duration", str(exc))
            return
        or_typed = or_key_var.get().strip()
        try:
            if or_remember.get() and or_typed:
                ig.save_remembered_key(OPENROUTER_PROVIDER, or_typed)
        except RuntimeError as exc:
            messagebox.showwarning("Remember key", str(exc))
        what = (f"Retry {len(images)} failed storyboard(s)" if retry
                else f"Write and narrate {len(images)} storyboard(s)")
        if not dry_run and not messagebox.askyesno(
                "Confirm", f"{what}?\n\nWriter: {style_label(style)}, duration "
                           f"{format_clock(target_s) if target_s else 'automatic'}.\n"
                           "Each story costs one writer call plus the narration "
                           f"({tts_var.get()}), both on OpenRouter. Storyboards already done are "
                           "skipped" + ("" if not force_var.get() else " — except now, "
                                        "'redo existing' is checked") + ".\n\nContinue?"):
            return
        cancel_event = threading.Event()
        kwargs = {
            "voices": voices,
            "dry_run": dry_run,
            "cancel_event": cancel_event,
            "on_progress": lambda info: root.after(0, lambda: on_progress(info)),
            "on_status": lambda message: root.after(0, lambda: on_status(message)),
            "output_dir": str(current_out_dir()),
            "writer_model": writer_var.get().strip() or WRITER_DEFAULT_MODEL,
            "tts_model": tts_var.get().strip() or DEFAULT_TTS_MODEL,
            "language": lang_var.get().strip() or DEFAULT_LANGUAGE,
            "style": style,
            "target_s": target_s,
            "openrouter_key": or_typed or resolve_openrouter_key()[0],
            "force": bool(force_var.get()) and not retry,
        }
        state.update(start=time.perf_counter(), cancel_event=cancel_event,
                     progress=f"generating... 0/{len(images)}", step="starting",
                     step_start=time.perf_counter())
        set_running(True)
        persist_config()
        show_status()
        tick_clock()
        threading.Thread(target=worker, args=(images, kwargs), daemon=True).start()

    def on_generate() -> None:
        try:
            images = list_storyboards(in_var.get().strip())
        except FileNotFoundError as exc:
            messagebox.showerror("Storyboards dir", str(exc))
            return
        if not images:
            messagebox.showerror("Storyboards dir", "No images (.png/.jpg/.webp/.gif) found.")
            return
        start_batch(images)

    def on_retry_failed() -> None:
        failed = failed_storyboards(current_out_dir(), style_from_label(style_var.get()))
        if not failed:
            messagebox.showinfo("Retry failed", f"No failed storyboard left to retry in "
                                                f"{style_var.get()}.")
            refresh_log()
            return
        start_batch(failed, retry=True)

    def on_cancel() -> None:
        event = state.get("cancel_event")
        if state["running"] and event is not None:
            event.set()
            ig.abort_all_http()
            status_var.set("cancelling... (aborting requests)")

    retry_btn.configure(command=on_retry_failed)
    # "Retry failed (n)" counts the failures of the selected writer style
    style_var.trace_add("write", lambda *_a: refresh_log())
    gen_btn.configure(command=on_generate)
    cancel_btn.configure(command=on_cancel)
    refresh_log()
    refresh_stories()
    if story_folders:
        story_list.selection_set(0)
        root.after(200, lambda: load_story(story_folders[0]))
    poll_player()

    def on_close() -> None:
        if state["running"]:
            return
        stop_play(release=True)
        persist_config()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cli_work = bool(args.input_dir or args.image or args.retry_failed or args.list or args.play
                    or args.delete
                    or args.check_voices
                    or args.forget_key or args.remember_key)
    if args.gui or not cli_work:
        defaults: dict = {"input_dir": args.input_dir, "output_dir": args.output_dir,
                          "writer_model": args.writer_model, "tts_model": args.tts_model,
                          "language": args.language}
        if args.voice:
            try:
                defaults["voices"] = [parse_voice_spec(v) for v in args.voice]
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        run_gui(defaults)
        return 0
    return main_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
