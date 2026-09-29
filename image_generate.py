#!/usr/bin/env python3
"""Generate images via OpenRouter with CLI and Tkinter GUI.

CLI examples:
    python image_generate.py --prompt "a red panda astronaut" --output-dir ./out
    python image_generate.py --prompt "a cat" --prop 16:9 --resolution 1K \\
        --context-dir ./ctx --memory-dir ./mem --model meta/muse-image
    python image_generate.py --gui
    python image_generate.py --list-log --output-dir ./out

GUI (stdlib tkinter):
    - Text field for prompt, entries for output/context/memory dirs.
    - Dropdowns for aspect ratio (--prop) and resolution tier (--resolution).
    - "Generate" button shows elapsed clock until the image arrives.
    - Bottom list shows rows from log_image_generate.csv.

Log file (in output dir): log_image_generate.csv
    Top lines (comments starting with #) hold total cost summary.
    Then a header row plus one row per operation with:
    date, prompt summary (1 line max, via free OpenRouter model),
    full intact prompt, image size, resolution,
    operation cost, generation timestamp, total timestamp (request->receipt).
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import http.client
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import socket
import struct
import sys
import threading
import time
import urllib.parse
import subprocess
import tempfile
import wave
import zlib
from typing import Callable
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken

    HAS_FERNET = True
except ImportError:  # optional dependency, only needed for remembered keys
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]
    HAS_FERNET = False

API_URL = "https://openrouter.ai/api/v1/images"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
IMAGE_MODEL_ENDPOINTS_URL = "https://openrouter.ai/api/v1/images/models/{model}/endpoints"
DEFAULT_MODEL = "meta/muse-image"
DEFAULT_PROVIDER = "openrouter"
LOG_FILENAME = "log_image_generate.csv"

ASPECT_RATIOS = [
    "auto", "1:1", "1:2", "1:4", "1:8", "2:1", "2:3", "3:2", "3:4",
    "4:1", "4:3", "4:5", "5:4", "8:1", "9:16", "16:9",
    "9:19.5", "19.5:9", "9:20", "20:9", "9:21", "21:9",
]
RESOLUTIONS = ["512", "1K", "2K", "4K"]
OUTPUT_FORMATS = ["png", "jpeg", "webp"]
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

MAX_CONTEXT_CHARS = 20000
MAX_SUMMARY_CHARS = 150
REQUEST_TIMEOUT_S = 300
SUMMARY_TIMEOUT_S = 60

LOG_FIELDS = [
    "date",
    "prompt_summary",
    "prompt_full",
    "image_file",
    "image_bytes",
    "width",
    "height",
    "resolution_req",
    "aspect_ratio_req",
    "seed",
    "cost_usd",
    "generation_timestamp",
    "total_seconds",
    "model",
    "provider",
    "key_hash",
]


def default_output_dir() -> str:
    """Default output dir: ImageGenerate inside the user pictures folder."""
    home = Path.home()
    for folder in ("Imagens", "Pictures", "images"):
        candidate = home / folder
        if candidate.is_dir():
            return str(candidate / "ImageGenerate")
    return str(home / "Imagens" / "ImageGenerate")


# ---------------------------------------------------------------------------
# Provider registry (one entry + one request function per provider)
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict] = {
    "openrouter": {
        "label": "OpenRouter",
        "env_var": "OPENROUTER_API_KEY",
        "default_model": DEFAULT_MODEL,
    },
    "gemini": {
        "label": "Gemini",
        "env_var": "GEMINI_API_KEY",
        "default_model": "",
    },
    "openai": {
        "label": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-image-1",
    },
}


# Accepted model slug prefixes per direct provider (prefix-based, not an
# allowlist, so future models of the same families keep working).
# OpenRouter accepts any slug: capability discovery adapts the request.
MODEL_PREFIXES: dict[str, tuple[str, ...] | None] = {
    "openrouter": None,
    "gemini": ("gemini-", "imagen-"),
    "openai": ("gpt-image-", "dall-e-"),
}


def _check_model_for_provider(provider: str, model: str) -> str:
    """Validate the model slug belongs to the provider's families."""
    clean = model.strip()
    prefixes = MODEL_PREFIXES.get(provider)
    if prefixes is not None and not clean:
        raise ValueError(
            f"o provider {provider} exige um modelo "
            f"(ex.: {', '.join(_model_examples(provider))})"
        )
    if prefixes is not None and clean and not clean.startswith(prefixes):
        raise ValueError(
            f"modelo {clean!r} não parece ser do provider {provider} "
            f"(esperado: {', '.join(_model_examples(provider))}); "
            f"troque de provider ou de modelo"
        )
    return clean


def _model_examples(provider: str) -> list[str]:
    return {
        "gemini": ["gemini-2.5-flash-image", "imagen-4.0-generate-001"],
        "openai": ["gpt-image-1", "dall-e-3"],
    }.get(provider, [])


def normalize_provider(name: str) -> str:
    """Lowercase provider slug restricted to safe chars (used in file names)."""
    slug = name.strip().lower()
    if not slug or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise ValueError(f"nome de provider inválido: {name!r}")
    if slug not in PROVIDERS:
        raise ValueError(
            f"provider desconhecido: {slug!r} "
            f"(suportados: {sorted(PROVIDERS)}; nada foi improvisado)"
        )
    return slug


MIN_COUNT = 1
MAX_COUNT = 30


def parse_count(raw: str | int | None) -> int:
    """Parse image count (natural number MIN_COUNT-MAX_COUNT). Raises ValueError."""
    text = str(raw).strip() if raw is not None else ""
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(f"invalid count: {raw!r} (need a natural number {MIN_COUNT}-{MAX_COUNT})")
    value = int(text)
    if not MIN_COUNT <= value <= MAX_COUNT:
        raise ValueError(f"invalid count: {raw!r} (need a natural number {MIN_COUNT}-{MAX_COUNT})")
    return value


# ---------------------------------------------------------------------------
# Remembered-keys vault (one Fernet key + one encrypted blob per provider)
# ---------------------------------------------------------------------------

VAULT_DIRNAME = "image_generate"


def vault_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / VAULT_DIRNAME


def _vault_paths(provider: str) -> tuple[Path, Path]:
    directory = vault_dir()
    return directory / f"{provider}.fkey", directory / f"{provider}_api_key.enc"


def _require_fernet() -> None:
    if not HAS_FERNET:
        raise RuntimeError("remembered keys need 'pip install cryptography'")


def _vault_save(slot: str, api_key: str) -> Path:
    """Encrypt and store a key under a vault slot name. Returns the blob path.

    Slots are provider slugs here; story_generate.py adds its own (e.g. fishaudio).
    """
    _require_fernet()
    fkey_path, blob_path = _vault_paths(slot)
    fkey_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(fkey_path.parent, 0o700)
    except OSError:
        pass
    if fkey_path.exists():
        fernet_key = fkey_path.read_bytes().strip()
    else:
        assert Fernet is not None
        fernet_key = Fernet.generate_key()
        fkey_path.write_bytes(fernet_key + b"\n")
        os.chmod(fkey_path, 0o600)
    assert Fernet is not None
    token = Fernet(fernet_key).encrypt(api_key.encode("utf-8"))
    blob_path.write_bytes(token + b"\n")
    os.chmod(blob_path, 0o600)
    return blob_path


def _vault_load(slot: str) -> str | None:
    """Decrypt and return the key stored under slot, or None if absent."""
    _require_fernet()
    fkey_path, blob_path = _vault_paths(slot)
    if not fkey_path.exists() or not blob_path.exists():
        return None
    assert Fernet is not None
    fernet_key = fkey_path.read_bytes().strip()
    token = blob_path.read_bytes().strip()
    try:
        return Fernet(fernet_key).decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(f"stored key for {slot} is corrupted or invalid") from exc


def _vault_forget(slot: str) -> bool:
    """Delete key material stored under slot. Returns True if anything removed."""
    removed = False
    for path in _vault_paths(slot):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
    return removed


def save_remembered_key(provider: str, api_key: str) -> Path:
    """Encrypt and store provider key. Returns the blob path."""
    _require_fernet()
    return _vault_save(normalize_provider(provider), api_key)


def load_remembered_key(provider: str) -> str | None:
    """Decrypt and return the stored provider key, or None if absent."""
    _require_fernet()
    return _vault_load(normalize_provider(provider))


def forget_remembered_key(provider: str) -> bool:
    """Delete stored key material for provider. Returns True if anything removed."""
    return _vault_forget(normalize_provider(provider))


def list_remembered_providers() -> list[str]:
    """Providers with a stored key blob on disk (no decryption attempted)."""
    if not vault_dir().is_dir():
        return []
    return sorted(
        p for p in PROVIDERS if (vault_dir() / f"{p}_api_key.enc").exists()
    )


def key_hash(api_key: str | None) -> str:
    """Short SHA-256 identifier for the log (never contains the key)."""
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def resolve_api_key(provider: str, cli_key: str | None = None) -> tuple[str | None, str]:
    """Resolve key by precedence: CLI flag > env var > remembered vault."""
    provider = normalize_provider(provider)
    if cli_key:
        return cli_key, "flag"
    env_key = os.environ.get(PROVIDERS[provider]["env_var"], "").strip()
    if env_key:
        return env_key, "env"
    if HAS_FERNET:
        try:
            stored = load_remembered_key(provider)
        except RuntimeError:
            stored = None
        if stored:
            return stored, "vault"
    return None, "none"


CONFIG_FILENAME = "config.json"

CONFIG_KEYS = (
    "output_dir",
    "context_dir",
    "memory_dir",
    "provider",
    "model",
    "summary_model",
    "prop",
    "resolution",
    "output_format",
    "dry_run",
    "analyse",
    "chosen_dir",
    "prompt",
)


def config_path() -> Path:
    return vault_dir() / CONFIG_FILENAME


def load_gui_config() -> dict:
    """Load persisted GUI settings (missing/corrupt file -> {})."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in CONFIG_KEYS if k in data}


def save_gui_config(settings: dict) -> Path:
    """Persist GUI settings atomically (only known keys)."""
    directory = vault_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: settings[k] for k in CONFIG_KEYS if k in settings},
                              indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def sanitize_gui_config(data: dict) -> dict:
    """Clamp loaded settings to valid values."""
    clean: dict = {}
    for key in ("output_dir", "context_dir", "memory_dir", "model", "summary_model"):
        value = data.get(key, "")
        if isinstance(value, str):
            clean[key] = value
    try:
        clean["provider"] = normalize_provider(str(data.get("provider", DEFAULT_PROVIDER)))
    except ValueError:
        clean["provider"] = DEFAULT_PROVIDER
    prop = str(data.get("prop", "1:1"))
    clean["prop"] = prop if prop in ASPECT_RATIOS else "1:1"
    res = str(data.get("resolution", "1K"))
    clean["resolution"] = res if res in RESOLUTIONS else "1K"
    fmt = str(data.get("output_format", "png"))
    clean["output_format"] = fmt if fmt in OUTPUT_FORMATS else "png"
    clean["dry_run"] = bool(data.get("dry_run", False))
    dirs = data.get("analyse", [])
    clean["analyse"] = [d for d in dirs if isinstance(d, str) and d.strip()] \
        if isinstance(dirs, list) else []
    clean["chosen_dir"] = str(data.get("chosen_dir", "") or "")
    prompt = data.get("prompt", "")
    clean["prompt"] = prompt if isinstance(prompt, str) else ""
    return clean


# ---------------------------------------------------------------------------
# Context / memory helpers
# ---------------------------------------------------------------------------

def nearest_existing_dir(path: str | os.PathLike | None) -> Path | None:
    """Return path if it is an existing directory, else its nearest existing
    ancestor directory (None for an empty path or when nothing exists)."""
    text = str(path or "").strip()
    if not text:
        return None
    current = Path(text).expanduser()
    while True:
        if current.is_dir():
            return current
        if current.parent == current:
            return None
        current = current.parent


# Dynamic dirs: generation i (0-based) uses
#   <base>/<start + (i // batch) % (range - start + 1)>
# i.e. `batch` consecutive generations share a folder, folders go
# start..range and cycle back to start. With start=1, range=5, batch=1
# generations 1..5 use folders 1..5 and generation 6 uses folder 1 again;
# with start=2, range=12, batch=3 generations 1-3 -> 2, 4-6 -> 3, 7-9 -> 4...
DYNAMIC_DIR_KEYS = ("output_dir", "context_dir", "memory_dir")


def parse_dynamic_start(raw: str | int | None, label: str = "Dynamic") -> int:
    """Parse a Dynamic dir start folder (natural number >= 1; empty -> 1)."""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return 1
    if not re.fullmatch(r"[0-9]+", text) or int(text) < 1:
        raise ValueError(f"invalid {label} start: {raw!r} (need a natural number >= 1)")
    return int(text)


def parse_dynamic_range(raw: str | int | None, label: str = "Dynamic", start: int = 1) -> int:
    """Parse a Dynamic dir range = last folder (natural number > start). Raises ValueError."""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        raise ValueError(f"{label} range is empty (need a natural number > {start})")
    if not re.fullmatch(r"[0-9]+", text) or int(text) <= start:
        raise ValueError(f"invalid {label} range: {raw!r} (need a natural number > "
                         f"{'start ' if start != 1 else ''}{start})")
    return int(text)


def parse_dynamic_batch(raw: str | int | None, label: str = "Dynamic") -> int:
    """Parse a Dynamic dir batch = generations per folder (natural number >= 1;
    empty -> 1)."""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return 1
    if not re.fullmatch(r"[0-9]+", text) or int(text) < 1:
        raise ValueError(f"invalid {label} batch: {raw!r} (need a natural number >= 1)")
    return int(text)


def parse_dynamic_spec(raw: object, label: str = "Dynamic") -> tuple[int, int, int]:
    """Parse one Dynamic spec into (start, range, batch).

    Accepts a plain range (str/int, start 1, batch 1) or a dict
    {"start": ..., "range": ..., "batch": ...} (start/batch default 1).
    """
    if isinstance(raw, dict):
        start = parse_dynamic_start(raw.get("start"), label)
        return (start, parse_dynamic_range(raw.get("range"), label, start),
                parse_dynamic_batch(raw.get("batch"), label))
    return 1, parse_dynamic_range(raw, label), 1  # type: ignore[arg-type]


def dynamic_dir_for(base: str, dir_range: int, index: int, start: int = 1,
                    batch: int = 1) -> str:
    """Subfolder used by generation `index` (0-based): `batch` generations per
    folder, folders <base>/<start>..<base>/<range>, cycling back to start."""
    step = index // max(1, batch)
    return str(Path(base).expanduser() / str(start + step % (dir_range - start + 1)))


def check_dynamic_dirs(dynamic_dirs: dict | None, dirs: dict,
                       total: int) -> dict[str, tuple[int, int, int]]:
    """Validate {dir_key: spec} (see parse_dynamic_spec) against the base dirs
    and the generation total.

    Returns {dir_key: (start, range, batch)}. Raises ValueError (bad key/start/range/batch,
    missing base folder, total <= 1) or FileNotFoundError (context/memory
    subfolders that the batch would need but do not exist; output subfolders
    are created).
    """
    parsed: dict[str, tuple[int, int, int]] = {}
    for key, raw in (dynamic_dirs or {}).items():
        if key not in DYNAMIC_DIR_KEYS:
            raise ValueError(f"unknown dynamic dir: {key!r} (use {', '.join(DYNAMIC_DIR_KEYS)})")
        label = key.replace("_dir", "").capitalize() + " dir Dynamic"
        start, dir_range, batch = parse_dynamic_spec(raw, label)
        if total <= 1:
            raise ValueError(f"{label} needs more than one generation (count > 1)")
        base = str(dirs.get(key) or "").strip()
        if not base:
            raise ValueError(f"{label} needs a base folder")
        if key != "output_dir":
            needed = sorted({dynamic_dir_for(base, dir_range, i, start, batch)
                             for i in range(total)}, key=lambda path: int(Path(path).name))
            missing = [path for path in needed if not Path(path).is_dir()]
            if missing:
                raise FileNotFoundError(
                    f"{label}: subfolder(s) not found: {', '.join(missing)}")
        parsed[key] = (start, dir_range, batch)
    return parsed


def load_context_text(context_dir: str | None) -> str:
    """Read all .md/.txt files from context dir, concatenated with headers."""
    if not context_dir:
        return ""
    root = Path(context_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"context dir not found: {context_dir}")
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".md", ".txt"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(root)
            parts.append(f"=== {rel} ===\n{text.strip()}")
    combined = "\n\n".join(parts).strip()
    if len(combined) > MAX_CONTEXT_CHARS:
        combined = combined[:MAX_CONTEXT_CHARS] + "\n[context truncated]"
    return combined


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        return mime
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")


def load_memory_references(memory_dir: str | None, limit: int = 16) -> list[dict]:
    """Encode reference images as OpenRouter input_references entries."""
    if not memory_dir:
        return []
    root = Path(memory_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"memory dir not found: {memory_dir}")
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    refs: list[dict] = []
    for path in files[:limit]:
        raw = path.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        url = f"data:{guess_mime(path)};base64,{b64}"
        refs.append({"type": "image_url", "image_url": {"url": url}})
    return refs


def build_final_prompt(base_prompt: str, context_text: str, prop: str, resolution: str,
                       caps: dict | None = None) -> str:
    """Append context and output-size instruction to the user prompt.

    The size sentences are a fallback hint: when caps confirm the API
    already carries that parameter, the sentence is skipped (less prompt
    pollution, smaller content-filter surface).
    """
    chunks = [base_prompt.strip()]
    if context_text:
        chunks.append(f"Context:\n{context_text}")
    if prop and prop != "auto" and (caps is None or "aspect_ratio" not in caps):
        chunks.append(f"Generate the image with aspect ratio {prop}.")
    if resolution and (caps is None or "resolution" not in caps):
        chunks.append(f"Generate the image at resolution tier {resolution}.")
    return "\n\n".join(c for c in chunks if c)


TEMPLATE_PATTERN = re.compile(r"\{\{([^{}]*)\}\}")


def extract_template_vars(prompt: str) -> list[str]:
    """Return unique {{variable}} names found in the prompt, in order."""
    seen: set[str] = set()
    names: list[str] = []
    for match in TEMPLATE_PATTERN.finditer(prompt):
        name = match.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def apply_template_values(prompt: str, values: dict[str, str]) -> str:
    """Replace each {{name}} with its value (unmatched placeholders stay)."""
    return TEMPLATE_PATTERN.sub(
        lambda m: values.get(m.group(1).strip(), m.group(0)), prompt
    )


def resolve_injection_rows(
    cells: list[list[str]], var_names: list[str]
) -> list[list[str]]:
    """Fill empty cells: repeat the value above; first row falls back to the
    variable name itself."""
    rows: list[list[str]] = []
    previous = [""] * len(var_names)
    for row in cells:
        filled: list[str] = []
        for j in range(len(var_names)):
            value = (row[j] if j < len(row) else "").strip()
            if not value:
                value = previous[j] or var_names[j]
            filled.append(value)
            previous[j] = value
        rows.append(filled)
    return rows


def truncate_prompt(prompt: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Collapse prompt to a single line with max length (offline fallback)."""
    one_line = " ".join(prompt.strip().split())
    if len(one_line) > limit:
        return one_line[: limit - 1] + "\u2026"
    return one_line


def _extract_message_text(message: dict) -> str:
    """Pull the answer text from a chat message (content only, never reasoning).

    Reasoning/thinking fields are deliberately ignored: logging them would
    leak chain-of-thought into the CSV instead of the requested summary.
    Empty content yields "" so the caller falls back to local truncation."""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and isinstance(b.get("text"), str)]
        if "".join(parts).strip():
            return "\n".join(parts)
    return ""


_SUMMARY_LABEL_RE = re.compile(
    r"^(?:\*{0,2}\s*(?:summary|resumo|answer|resposta)\s*:+\s*\*{0,2}\s*)+",
    re.IGNORECASE,
)
_SUMMARY_META_RES = tuple(
    re.compile(pat, re.IGNORECASE) for pat in (
        r"^here'?s\b",
        r"^here is\b",
        r"^sure\b",
        r"^of course\b",
        r"^certainly\b",
        r"^understood\b",
        r"^great\b",
        r"^okay\b",
        r"^ok\b",
        r"^the user\b",
        r"^user\b",
        r"^you (want|ask|request|said|provided)\b",
        r"^your (prompt|request|message)\b",
        r"^this (prompt|request|image|story)\b",
        r"^i('ll|'m| will| am| have| understand| summarize| need| should| must)\b",
        r"^we (need|will|should|must|have)\b",
        r"^let me\b",
        r"^the task\b",
        r"^my task\b",
        r"^the prompt\b",
        r"^as an ai\b",
        r"^based on\b",
        r"^to (summarize|create|complete)\b",
        r"^think",
        r"^thought\b",
        r"^analy[sz]",
        r"^step\s*\d",
        r"^process\s*:",
        r"^\d+\s*[.)]\s",
    )
)
_SUMMARY_META_SUBSTRINGS = (
    "thinking process",
    "chain of thought",
    "the user wants",
    "the user asks",
    "user wants me",
    "user asks me",
    "wants me to",
    "asks me to",
    "your prompt",
    "this prompt",
    "the task is",
    "my task is",
    "we need to",
    "i need to",
    "let me ",
    "same language as",
    "as an ai",
)


def clean_summary_text(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Reduce raw model output to a single summary sentence.

    Keeps the first non-empty line, strips wrapping quotes and
    "Summary:"-style labels, and rejects thinking-process leakage
    (ValueError -> caller falls back to plain truncation).
    """
    line = ""
    for raw_line in text.splitlines():
        if raw_line.strip():
            line = raw_line.strip()
            break
    line = line.strip("\"'`*“”‘’").strip()
    line = _SUMMARY_LABEL_RE.sub("", line).strip("\"'`*“”‘’ ").strip()
    line = re.sub(r"^[\-\*\u2022>\s]+", "", line).strip()
    lowered = line.lower()
    if not line:
        raise ValueError("empty summary from model")
    if (any(sub in lowered for sub in _SUMMARY_META_SUBSTRINGS)
            or any(pat.match(line) for pat in _SUMMARY_META_RES)):
        raise ValueError("model returned meta commentary instead of a summary")
    one_line = " ".join(line.split())
    if len(one_line) > limit:
        cut = one_line[:limit].rsplit(" ", 1)[0] or one_line[:limit]
        one_line = cut.rstrip(".,;:") + "."
    return one_line


def play_chime(kind: str = "success") -> None:
    """Play a short, quiet notification blip (success = high, error = low)."""
    try:
        freq = 880.0 if kind == "success" else 220.0
        rate = 44100
        duration = 0.12
        fade = 0.02
        peak = 0.12  # keep it subtle: ~12% of full scale
        samples: list[float] = []
        t = 0.0
        while t < duration:
            amp = peak
            if t < fade:  # fade in/out to avoid clicks
                amp *= t / fade
            elif t > duration - fade:
                amp *= (duration - t) / fade
            samples.append(amp * math.sin(2 * math.pi * freq * t))
            t += 1.0 / rate
        data = struct.pack("<" + "h" * len(samples),
                           *(int(s * 32767) for s in samples))
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        with wave.open(path, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(data)
        subprocess.run(["aplay", "-q", path], capture_output=True)
        os.unlink(path)
    except Exception:
        pass


def _summary_instruction(prompt: str) -> str:
    """Build the single user message asking for the 1-sentence summary.

    Single user-only message (no system role): some free shared-pool
    providers return null content when a system message is present, and
    reasoning models leak thinking into ``content`` unless thinking is
    capped (see ``reasoning`` budget in :func:`summarize_prompt_remote`).
    The instruction matches the prompt language (PT heuristic, else EN).
    """
    if re.search(
        r"[ãõçâêôáéíóúàü]|"
        r"\b(uma|para|com|como|historia|história|menina|menino|voce|você|este|esta|"
        r"isto|isso|não|nao|mais|sobre|entre|quando|onde|storyboard)\b",
        prompt, re.IGNORECASE,
    ):
        return (
            "Sem conversação, apenas a resposta. Resuma em uma frase de até "
            f"{MAX_SUMMARY_CHARS} caracteres o texto abaixo, no mesmo idioma dele. "
            "Responda somente com a frase do resumo, sem explicações, sem "
            "numeração, sem aspas e sem reticências.\n\n"
            f"{prompt}"
        )
    return (
        "No conversation, only the answer. Summarize the text below in one "
        f"sentence of at most {MAX_SUMMARY_CHARS} characters, in the same "
        "language as the text. Reply with only the summary sentence, no "
        "explanations, no numbering, no quotes, no ellipsis.\n\n"
        f"{prompt}"
    )


# Thinking budget for summary calls: reasoning models share one token
# budget between thinking and answer, so uncapped thinking eats the whole
# max_tokens and the API returns null/thinking-only content. Proven live
# against nvidia/nemotron-3-super (64 thinking tokens -> clean answer).
_SUMMARY_REASONING_BUDGET = 64
_SUMMARY_MAX_TOKENS = 200


def summarize_prompt_remote(
    prompt: str,
    api_key: str,
    model: str,
    timeout_s: int,
    cancel_event: threading.Event | None = None,
) -> str:
    """Ask an OpenRouter chat model for a 1-sentence summary of the prompt."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _summary_instruction(prompt)}],
        "max_tokens": _SUMMARY_MAX_TOKENS,
        "temperature": 0.0,
        # Cap thinking so reasoning models still leave budget for the answer.
        # Unknown to a provider -> ignored or 4xx -> caller falls back to
        # truncation, same as any other remote failure.
        "reasoning": {"max_tokens": _SUMMARY_REASONING_BUDGET},
    }
    status, raw = _post_json(CHAT_URL, body, _openrouter_headers(api_key), timeout_s, cancel_event)
    if status != 200:
        raise RuntimeError(f"OpenRouter HTTP {status}: {raw[:2000]}")
    payload = json.loads(raw)
    message = payload["choices"][0]["message"]
    if isinstance(message.get("refusal"), str) and message["refusal"].strip():
        raise ValueError(f"model refused the summary request: {message['refusal'][:200]}")
    content = _extract_message_text(message)
    return clean_summary_text(content)


def summarize_prompt(
    prompt: str,
    api_key: str | None = None,
    model: str = "",
    timeout_s: int = SUMMARY_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
) -> str:
    """Summarize prompt via chat model; empty model skips to truncation (no API call)."""
    if api_key and model.strip():
        try:
            return summarize_prompt_remote(prompt, api_key, model.strip(), timeout_s, cancel_event)
        except GenerationCancelled:
            raise
        except Exception as exc:
            print(f"warning: remote summary failed ({type(exc).__name__}: {exc}); "
                  "using truncation instead", file=sys.stderr)
            pass
    return truncate_prompt(prompt)


# ---------------------------------------------------------------------------
# Image introspection (stdlib only, no Pillow)
# ---------------------------------------------------------------------------

def png_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


def jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        return None
    i = 2
    sof_markers = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
    while i + 4 < len(raw):
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        if marker == 0xD9:  # EOI
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        size = struct.unpack(">H", raw[i + 2 : i + 4])[0]
        if marker in sof_markers and i + 9 < len(raw):
            height, width = struct.unpack(">HH", raw[i + 5 : i + 9])
            return width, height
        i += 2 + size
    return None


def webp_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        return None
    kind = raw[12:16]
    if kind == b"VP8 " and len(raw) >= 30:
        width = struct.unpack("<H", raw[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", raw[28:30])[0] & 0x3FFF
        return width, height
    if kind == b"VP8L" and len(raw) >= 25:
        b0, b1, b2, b3 = raw[21], raw[22], raw[23], raw[24]
        width = 1 + (((b1 & 0x3F) << 8) | b0)
        height = 1 + (((b3 & 0xF) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
        return width, height
    if kind == b"VP8X" and len(raw) >= 30:
        width = 1 + int.from_bytes(raw[24:27], "little")
        height = 1 + int.from_bytes(raw[27:30], "little")
        return width, height
    return None


def gif_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) >= 10 and raw[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", raw[6:10])
        return width, height
    return None


def inspect_image(path: Path) -> tuple[int, int, int]:
    """Return (file_bytes, width, height) for a saved image file."""
    raw = path.read_bytes()
    size = len(raw)
    dims = (
        png_dimensions(raw)
        or jpeg_dimensions(raw)
        or webp_dimensions(raw)
        or gif_dimensions(raw)
    )
    width, height = dims if dims else (0, 0)
    return size, width, height


def target_dimensions(prop: str, resolution: str) -> tuple[int, int]:
    """Estimate pixel size for placeholder images from ratio + tier."""
    base = {"512": 512, "1K": 1024, "2K": 2048, "4K": 4096}.get(resolution, 1024)
    try:
        left, right = prop.replace(" ", "").split(":")
        w_ratio, h_ratio = float(left), float(right)
    except (ValueError, ZeroDivisionError):
        return base, base
    if w_ratio <= 0 or h_ratio <= 0:
        return base, base
    if w_ratio >= h_ratio:
        width = base
        height = max(1, round(base * h_ratio / w_ratio))
    else:
        height = base
        width = max(1, round(base * w_ratio / h_ratio))
    return width, height


def write_placeholder_png(path: Path, width: int, height: int) -> None:
    """Write a small solid-color PNG using only stdlib (for --dry-run)."""
    width = max(1, min(width, 1024))
    height = max(1, min(height, 1024))
    gray = (200, 210, 220)
    row = b"\x00" + bytes(gray) * width
    compressed = zlib.compress(row * height, level=6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + tag + data
        out += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return out

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    path.write_bytes(png)


# ---------------------------------------------------------------------------
# Cancellable HTTP layer (http.client so Cancel can abort blocked reads)
# ---------------------------------------------------------------------------

class GenerationCancelled(Exception):
    """Raised when the user cancels an in-flight generation."""


_HTTP_LOCK = threading.Lock()
_HTTP_CONNS: set[http.client.HTTPConnection] = set()


def abort_all_http() -> None:
    """Close every in-flight HTTP connection (unblocks reads with an error).

    The server may still finish processing on its side; cancellation only
    stops our wait, and results are discarded (nothing saved, nothing logged).
    """
    with _HTTP_LOCK:
        conns = list(_HTTP_CONNS)
    for conn in conns:
        try:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    # shutdown() reliably releases a thread blocked in recv();
                    # close() alone does not.
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _openrouter_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "HTTP-Referer": "https://github.com/krosct/ImageGenerate",
        "X-Title": "ImageGenerate",
    }


def _post_json(
    url: str,
    body: dict,
    headers: dict,
    timeout_s: int,
    cancel_event: threading.Event | None = None,
) -> tuple[int, str]:
    """POST JSON and return (status, raw_text). Aborts on cancel_event."""
    parts = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    default_port = 443 if parts.scheme == "https" else 80
    conn = conn_cls(parts.hostname or "", parts.port or default_port, timeout=timeout_s)
    with _HTTP_LOCK:
        _HTTP_CONNS.add(conn)
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("generation cancelled")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request("POST", path, body=json.dumps(body).encode("utf-8"), headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("generation cancelled")
        return resp.status, raw
    except (OSError, http.client.HTTPException) as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("generation cancelled") from exc
        raise
    finally:
        with _HTTP_LOCK:
            _HTTP_CONNS.discard(conn)
        try:
            conn.close()
        except Exception:
            pass


def _fetch_json(
    url: str,
    headers: dict,
    timeout_s: int,
    cancel_event: threading.Event | None = None,
) -> tuple[int, str]:
    """GET JSON (capability discovery) with the same cancel semantics as _post_json."""
    parts = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    default_port = 443 if parts.scheme == "https" else 80
    conn = conn_cls(parts.hostname or "", parts.port or default_port, timeout=timeout_s)
    with _HTTP_LOCK:
        _HTTP_CONNS.add(conn)
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("generation cancelled")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("generation cancelled")
        return resp.status, raw
    except (OSError, http.client.HTTPException) as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("generation cancelled") from exc
        raise
    finally:
        with _HTTP_LOCK:
            _HTTP_CONNS.discard(conn)
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Provider request functions (one per provider, same input/output contract)
# Each returns (payload, start_ts, elapsed_s) where payload has:
#   data: [{b64_json, media_type?}], usage: {cost?}, created
# ---------------------------------------------------------------------------

class ContentPolicyError(RuntimeError):
    """Raised when the provider blocks the prompt via content filter (HTTP 400)."""


_CONTENT_POLICY_MARKERS = (
    "content management policy",
    "content_policy",
    "content policy",
    "content filter",
    "filtered due to the prompt",
    "triggering our content",
    "moderation",
    "guardrail",
    "refusal",
    "refused",
    "moderation_blocked",
    "content_policy_violation",
    "prohibited_content",
)


def _is_content_policy_refusal(status: int, raw: str) -> bool:
    """Detect provider content-filter rejections (HTTP 400 + filter wording)."""
    if status != 400:
        return False
    lowered = (raw or "").lower()
    return any(marker in lowered for marker in _CONTENT_POLICY_MARKERS)


_CAPABILITY_MISMATCH_MARKERS = (
    "supports the requested parameter",
    "filter by image capabilities",
)


# Hard API ceilings for images per call ("n"), independent of the model
# (OpenRouter validates n <= 10 before routing; OpenAI documents 1-10).
# Larger counts are split by _fan_out_requests into several calls.
OPENROUTER_MAX_N = 10
OPENAI_MAX_N = 10

# The Zod issues arrive JSON-encoded inside a JSON string, so quotes may be
# escaped (\") and newlines may be literal "\n" sequences.
_ESC_WS = r'(?:\s|\\n)*'
_N_TOO_BIG_MAX = re.compile(r'\\*"maximum\\*"' + _ESC_WS + ':' + _ESC_WS + r'(\d+)')
_N_TOO_BIG_PATH = re.compile(r'\\*"path\\*"' + _ESC_WS + ':' + _ESC_WS + r'\['
                             + _ESC_WS + r'\\*"n\\*"' + _ESC_WS + r'\]')


def _n_limit_from_error(raw: str) -> int | None:
    """Max images per call from a router validation error on "n"
    (ZodError too_big, e.g. ... "maximum": 10 ... "path": ["n"]), else None."""
    text = raw or ""
    if "too_big" not in text or not _N_TOO_BIG_PATH.search(text):
        return None
    match = _N_TOO_BIG_MAX.search(text)
    return max(1, int(match.group(1))) if match else None


def _is_capability_mismatch(status: int, raw: str) -> bool:
    """Detect router rejections for parameters no endpoint supports (HTTP 400)."""
    if status != 400:
        return False
    lowered = (raw or "").lower()
    return any(marker in lowered for marker in _CAPABILITY_MISMATCH_MARKERS)


def _content_policy_message(model: str, prompt: str, status: int, raw: str,
                            ref_count: int, provider_label: str = "") -> str:
    """Build a user-facing (pt-BR) message for content-filter blocks."""
    provider_name = provider_label or "unknown"
    provider_detail = raw[:500].strip()
    try:
        error_obj = json.loads(raw).get("error", {})
        if isinstance(error_obj, dict):
            meta = error_obj.get("metadata", {})
            if isinstance(meta, dict) and meta.get("provider_name"):
                provider_name = str(meta["provider_name"])
            if isinstance(error_obj.get("message"), str) and error_obj["message"].strip():
                provider_detail = error_obj["message"].strip()[:500]
    except (ValueError, AttributeError):
        pass
    preview = " ".join((prompt or "").strip().split())[:300] or "(empty)"
    extras = ""
    if ref_count:
        extras = f" (+{ref_count} reference image(s) from Memory dir)"
    return (
        f"O provedor bloqueou o prompt por filtro de conteudo "
        f"(OpenRouter HTTP {status}, provider {provider_name}, model {model}). "
        f"Mesmo prompts simples podem ser barrados quando Context/Memory "
        f"adicionam texto ou imagens ocultas ao prompt final{extras}. "
        f"Prompt enviado (inicio): \"{preview}\". "
        f"O que tentar: 1) simplifique o prompt; "
        f"2) limpe Context dir e Memory dir e teste de novo; "
        f"3) rode com --dry-run para validar o fluxo; "
        f"4) troque de modelo/provedor; 5) tente mais tarde. "
        f"Detalhe do provedor: {provider_detail}"
    )


# ---------------------------------------------------------------------------
# Model capability discovery (makes the app agnostic to the image model)
# ---------------------------------------------------------------------------

CAPS_TIMEOUT_S = 15
_CAPS_CACHE: dict[str, dict | None] = {}
_CAPS_LOCK = threading.Lock()


def _model_capabilities(
    model: str,
    api_key: str,
    cancel_event: threading.Event | None = None,
    refresh: bool = False,
) -> dict | None:
    """Fetch (and cache) the model's supported_parameters from OpenRouter.

    Merges every endpoint via intersection, so the adapted request is
    accepted no matter which endpoint the router picks. Returns None when
    the model is unknown or the discovery call fails: callers then send
    the request unchanged (best effort). Cancel propagates and nothing
    is cached on cancellation.
    """
    key = model.strip()
    with _CAPS_LOCK:
        if not refresh and key in _CAPS_CACHE:
            return _CAPS_CACHE[key]
    url = IMAGE_MODEL_ENDPOINTS_URL.format(model=urllib.parse.quote(key, safe=""))
    headers = _openrouter_headers(api_key)
    caps: dict | None
    try:
        status, raw = _fetch_json(url, headers, CAPS_TIMEOUT_S, cancel_event)
        if status != 200:
            caps = None
        else:
            endpoints = json.loads(raw).get("endpoints") or []
            param_sets: list[dict] = []
            for ep in endpoints:
                params = ep.get("supported_parameters") if isinstance(ep, dict) else None
                if isinstance(params, dict):
                    param_sets.append(params)
            caps = _intersect_capabilities(param_sets) or None
    except (OSError, http.client.HTTPException, ValueError):
        caps = None
    with _CAPS_LOCK:
        _CAPS_CACHE[key] = caps
    return caps


def _intersect_capabilities(param_sets: list[dict]) -> dict:
    """Intersect supported_parameters across endpoints (conservative merge)."""
    if not param_sets:
        return {}
    merged: dict = dict(param_sets[0])
    for params in param_sets[1:]:
        for name in list(merged):
            if name not in params:
                del merged[name]
                continue
            merged[name] = _intersect_descriptors(merged[name], params[name])
            if merged[name] is None:
                del merged[name]
    return merged


def _intersect_descriptors(first: object, second: object) -> dict | None:
    """Intersect two capability descriptors; None when incompatible."""
    if not isinstance(first, dict) or not isinstance(second, dict):
        return first if isinstance(first, dict) else None
    if first.get("type") != second.get("type"):
        return None
    kind = first.get("type")
    if kind == "enum":
        values = [v for v in first.get("values", []) if v in (second.get("values") or [])]
        return {"type": "enum", "values": values} if values else None
    if kind == "range":
        try:
            lo = max(int(first.get("min", 1)), int(second.get("min", 1)))
            hi = min(int(first.get("max", lo)), int(second.get("max", lo)))
        except (TypeError, ValueError):
            return None
        return {"type": "range", "min": lo, "max": hi} if hi >= lo else None
    return first


def _cap_values(caps: dict, name: str) -> list[str] | None:
    """Enum values for a parameter, or None when absent/not an enum."""
    desc = caps.get(name)
    if isinstance(desc, dict) and desc.get("type") == "enum":
        values = desc.get("values")
        if isinstance(values, list) and values:
            return [str(v) for v in values]
    return None


def _cap_max(caps: dict, name: str) -> int | None:
    """Upper bound for a range parameter, or None when unbounded/unknown."""
    desc = caps.get(name)
    if isinstance(desc, dict) and desc.get("type") == "range":
        try:
            return max(1, int(desc.get("max", 1)))
        except (TypeError, ValueError):
            return None
    return None


def _parse_ratio(text: str) -> float | None:
    """Parse "W:H" into a float ratio, or None when not numeric."""
    try:
        left, right = (float(x) for x in text.replace(" ", "").split(":", 1))
    except ValueError:
        return None
    if left <= 0 or right <= 0:
        return None
    return left / right


def _closest_ratio(requested: str, values: list[str]) -> str | None:
    """Supported ratio closest to the requested one (log-distance), else None."""
    if requested in values:
        return requested
    target = _parse_ratio(requested)
    if target is None:
        return None
    best: str | None = None
    best_diff = float("inf")
    for value in values:
        candidate = _parse_ratio(value)
        if candidate is None:
            continue
        diff = abs(math.log(target / candidate))
        if diff < best_diff:
            best, best_diff = value, diff
    return best


def _adapt_image_body(body: dict, caps: dict | None) -> dict:
    """Trim/adjust request fields to what the model endpoint supports."""
    if not caps:
        return body
    adapted = dict(body)
    if "resolution" not in caps:
        adapted.pop("resolution", None)
    else:
        values = _cap_values(caps, "resolution")
        if values and adapted.get("resolution") not in values:
            adapted.pop("resolution", None)
    if "aspect_ratio" not in caps:
        adapted.pop("aspect_ratio", None)
    else:
        values = _cap_values(caps, "aspect_ratio")
        requested = adapted.get("aspect_ratio")
        if values and requested:
            closest = _closest_ratio(str(requested), values)
            if closest:
                adapted["aspect_ratio"] = closest
            else:
                adapted.pop("aspect_ratio", None)
    if "output_format" not in caps:
        adapted.pop("output_format", None)
    else:
        values = _cap_values(caps, "output_format")
        if values and adapted.get("output_format") not in values:
            adapted["output_format"] = values[0]
    if "seed" not in caps:
        adapted.pop("seed", None)
    if "n" not in caps:
        adapted.pop("n", None)
    refs = adapted.get("input_references")
    if refs:
        if "input_references" not in caps:
            adapted.pop("input_references", None)
            print(f"warning: model {body.get('model', '')!r} does not support "
                  "input_references; memory dir images were ignored",
                  file=sys.stderr)
        else:
            max_refs = _cap_max(caps, "input_references")
            if max_refs and len(refs) > max_refs:
                adapted["input_references"] = refs[:max_refs]
    return adapted


def _merge_payload(merged: dict, payload: dict) -> dict:
    """Merge one more single-request payload into the accumulated result."""
    merged.setdefault("data", []).extend(payload.get("data") or [])
    merged_usage = merged.get("usage")
    payload_usage = payload.get("usage")
    if isinstance(merged_usage, dict) and isinstance(payload_usage, dict):
        try:
            merged_usage["cost"] = (float(merged_usage.get("cost") or 0)
                                    + float(payload_usage.get("cost") or 0))
        except (TypeError, ValueError):
            pass
    return merged


def _fan_out_requests(count, per_call, call):
    """Call call(n, call_index) until count images are received.

    call returns (payload, effective_seed); the index lets adapters vary
    the seed per call (seed+i) so repeated n=1 requests still vary.
    Returns (merged_payload, effective_seeds) trimmed to count. An empty
    response stops the loop (avoids spinning forever). At most count calls.
    """
    merged: dict | None = None
    seeds: list = []
    received = 0
    calls = 0
    total = max(1, count)
    while received < total and calls < total:
        n = max(1, min(max(1, per_call), total - received))
        payload, effective_seed = call(n, calls)
        items = payload.get("data") or []
        seeds.extend([effective_seed] * len(items))
        received += len(items)
        merged = payload if merged is None else _merge_payload(merged, payload)
        calls += 1
        if not items:
            break
    assert merged is not None
    merged["data"] = (merged.get("data") or [])[:total]
    merged["seeds"] = seeds[:total]
    return merged, merged["seeds"]


def _bearer_headers(api_key: str) -> dict:
    """Minimal JSON headers for direct provider APIs (no router metadata)."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _split_data_url(url: str) -> tuple[str, str] | None:
    """Split a data: URL into (mime_type, b64) for native image inputs."""
    if not url.startswith("data:"):
        return None
    header, _, b64 = url.partition(",")
    if not b64 or ";base64" not in header:
        return None
    return header[5:].split(";")[0] or "image/png", b64


def request_openrouter(
    *,
    api_key: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    references: list[dict],
    output_format: str,
    seed: int | None,
    count: int,
    timeout_s: int,
    cancel_event: threading.Event | None = None,
) -> tuple[dict, float, float]:
    """POST to OpenRouter images API. Returns (payload, start_ts, elapsed_s).

    Model-agnostic: the request body is adapted to the model's capabilities
    (unsupported params dropped, enums/ranges clamped). Providers that only
    accept n=1 are called repeatedly (seed varied per call) and their
    payloads merged, so the requested image count is honored for any model.
    On a capability-mismatch 400, capabilities are refreshed and the
    adapted request retried once. payload["seeds"] holds the effective
    seed used per returned image (None when the model got no seed).
    """
    base: dict = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "output_format": output_format,
        "n": count,
    }
    if references:
        base["input_references"] = references
    if seed is not None:
        base["seed"] = seed
    start = time.perf_counter()
    start_ts = time.time()
    headers = _openrouter_headers(api_key)
    caps = _model_capabilities(model, api_key, cancel_event)
    last_error: Exception | None = None
    n_limit = OPENROUTER_MAX_N
    for attempt in (0, 1):
        body = _adapt_image_body(dict(base), caps)
        send_seed = seed is not None and "seed" in body
        max_n = _cap_max(caps, "n") if caps else None
        if max_n is None:
            max_n = int(body.get("n", 1) or 1)
        max_n = max(1, min(max_n, n_limit))

        def single(n: int, index: int) -> tuple[dict, int | None]:
            one = dict(body)
            if "n" in one:
                one["n"] = n
            effective = seed + index if send_seed and seed is not None else None
            if send_seed:
                one["seed"] = effective
            else:
                one.pop("seed", None)
            status, raw = _post_json(API_URL, one, headers, timeout_s, cancel_event)
            if status != 200:
                if _is_content_policy_refusal(status, raw):
                    ref_count = len(one.get("input_references") or [])
                    raise ContentPolicyError(
                        _content_policy_message(model, prompt, status, raw, ref_count)
                    )
                raise RuntimeError(f"OpenRouter HTTP {status}: {raw[:2000]}")
            return json.loads(raw), effective

        try:
            merged, _seeds = _fan_out_requests(count, max_n, single)
            elapsed = time.perf_counter() - start
            return merged, start_ts, elapsed
        except RuntimeError as exc:
            if isinstance(exc, ContentPolicyError) or attempt == 1:
                raise
            last_error = exc
            # Router says n is too big: retry split into calls of that size.
            router_limit = _n_limit_from_error(str(exc))
            if router_limit is not None and router_limit < max_n:
                n_limit = router_limit
                continue
            if not _is_capability_mismatch(400, str(exc)):
                raise
            caps = _model_capabilities(model, api_key, cancel_event, refresh=True)
            if not caps:
                raise last_error
    assert last_error is not None  # loop always returns or raises above
    raise last_error


OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
GEMINI_GENERATE_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
                       "{model}:generateContent")
GEMINI_PREDICT_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
                      "{model}:predict")

OPENAI_GPT_PREFIX = "gpt-image-"
OPENAI_DALLE3_PREFIX = "dall-e-3"
OPENAI_DALLE2_PREFIX = "dall-e-2"

GEMINI_NATIVE_PREFIX = "gemini-"
GEMINI_IMAGEN_PREFIX = "imagen-"
GEMINI_ASPECTS = ["1:1", "3:4", "4:3", "9:16", "16:9"]
GEMINI_SIZES = {"512": "1K", "1K": "1K", "2K": "2K", "4K": "4K"}
IMAGEN_SIZES = {"512": "1K", "1K": "1K", "2K": "2K", "4K": "2K"}


def _gemini_headers(api_key: str) -> dict:
    return {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _openai_size(aspect_ratio: str, model: str) -> str:
    """Map aspect ratio to an OpenAI size string for the model family."""
    ratio = _parse_ratio(aspect_ratio)
    landscape = ratio is not None and ratio > 1.05
    portrait = ratio is not None and ratio < 1 / 1.05
    if model.startswith(OPENAI_DALLE3_PREFIX):
        if landscape:
            return "1792x1024"
        if portrait:
            return "1024x1792"
        return "1024x1024"
    if model.startswith(OPENAI_DALLE2_PREFIX):
        return "1024x1024"
    if landscape:
        return "1536x1024"
    if portrait:
        return "1024x1536"
    return "1024x1024"


def request_openai(
    *,
    api_key: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    references: list[dict],
    output_format: str,
    seed: int | None,
    count: int,
    timeout_s: int,
    cancel_event: threading.Event | None = None,
) -> tuple[dict, float, float]:
    """POST to the OpenAI Images API. Returns (payload, start_ts, elapsed_s).

    The generations endpoint is text-only: reference images are rejected
    with an informative error (no improvisation). OpenAI has no seed
    parameter, so a requested seed is dropped (stderr note). dall-e-3
    only accepts n=1: extra images are fetched via repeated calls with
    the payloads merged. payload["seeds"] is all None (model got no seed).
    """
    model = _check_model_for_provider("openai", model)
    if not api_key:
        raise RuntimeError(
            "missing openai API key (set OPENAI_API_KEY, use --api-key, "
            "or save it with --provider openai --remember-key)"
        )
    if references:
        raise RuntimeError(
            f"modelo {model!r} via OpenAI (generations) não aceita imagens de "
            "referência; limpe Memory dir ou use outro provider/modelo"
        )
    if seed is not None:
        print(f"warning: modelo {model!r} via OpenAI não suporta seed; "
              "a seed pedida foi ignorada", file=sys.stderr)
    per_call = 1 if model.startswith(OPENAI_DALLE3_PREFIX) else min(max(1, count), OPENAI_MAX_N)
    body: dict = {
        "model": model,
        "prompt": prompt,
        "n": min(per_call, max(1, count)),
        "size": _openai_size(aspect_ratio, model),
    }
    if model.startswith(OPENAI_GPT_PREFIX):
        if output_format in ("png", "jpeg", "webp"):
            body["output_format"] = output_format
        body["quality"] = {"512": "low", "1K": "medium",
                           "2K": "high", "4K": "high"}.get(resolution, "auto")
    elif model.startswith(OPENAI_DALLE3_PREFIX):
        body["response_format"] = "b64_json"
        body["quality"] = "hd" if resolution in ("2K", "4K") else "standard"
    elif model.startswith(OPENAI_DALLE2_PREFIX):
        body["response_format"] = "b64_json"
    headers = _bearer_headers(api_key)
    start = time.perf_counter()
    start_ts = time.time()

    def single(n: int, _index: int) -> tuple[dict, None]:
        one = dict(body)
        one["n"] = n
        status, raw = _post_json(OPENAI_IMAGES_URL, one, headers, timeout_s, cancel_event)
        if status != 200:
            if _is_content_policy_refusal(status, raw):
                raise ContentPolicyError(
                    _content_policy_message(model, prompt, status, raw, 0,
                                            provider_label="OpenAI")
                )
            raise RuntimeError(f"OpenAI HTTP {status}: {raw[:2000]}")
        payload = json.loads(raw)
        items = []
        for item in payload.get("data") or []:
            if isinstance(item, dict) and item.get("b64_json"):
                items.append({"b64_json": item["b64_json"]})
        if (payload.get("data") or []) and not items:
            raise RuntimeError(
                "OpenAI returned no image bytes (only URLs); "
                "use a gpt-image model or report this as a bug"
            )
        payload["data"] = items
        return payload, None

    merged, seeds = _fan_out_requests(count, per_call, single)
    return merged, start_ts, time.perf_counter() - start


def _gemini_image_parts(prompt: str, references: list[dict]) -> list[dict]:
    """Build generateContent parts (text + inlineData reference images)."""
    parts: list[dict] = [{"text": prompt}]
    for ref in references:
        try:
            url = ref["image_url"]["url"]
        except (KeyError, TypeError):
            continue
        split = _split_data_url(url)
        if split is None:
            print("warning: skipping a non-data reference image "
                  "(Gemini needs data: URLs)", file=sys.stderr)
            continue
        mime, b64 = split
        parts.append({"inlineData": {"mimeType": mime, "data": b64}})
    return parts


def _gemini_parse_images(payload: dict, model: str) -> list[dict]:
    """Extract shared-contract image items from a generateContent response."""
    candidates = payload.get("candidates") or []
    if not candidates:
        feedback = payload.get("promptFeedback") or {}
        reason = feedback.get("blockReason", "") if isinstance(feedback, dict) else ""
        if reason:
            raise ContentPolicyError(
                f"O provedor bloqueou o prompt por filtro de conteudo "
                f"(Google, model {model}). O que tentar: 1) simplifique o prompt; "
                f"2) limpe Context dir e Memory dir e teste de novo; "
                f"3) rode com --dry-run para validar o fluxo. "
                f"Detalhe do provedor: blockReason={reason}"
            )
        raise RuntimeError(f"Gemini returned no candidates: {str(payload)[:500]}")
    items = []
    for part in (candidates[0].get("content") or {}).get("parts") or []:
        inline = part.get("inlineData") if isinstance(part, dict) else None
        if isinstance(inline, dict) and inline.get("data"):
            item: dict = {"b64_json": inline["data"]}
            if inline.get("mimeType"):
                item["media_type"] = inline["mimeType"]
            items.append(item)
    return items


def request_gemini(
    *,
    api_key: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    references: list[dict],
    output_format: str,
    seed: int | None,
    count: int,
    timeout_s: int,
    cancel_event: threading.Event | None = None,
) -> tuple[dict, float, float]:
    """POST to the Gemini/Imagen native APIs. Returns (payload, start_ts, elapsed_s).

    gemini-* models use generateContent (imageConfig, inlineData refs, one
    image per call, no seed support); imagen-* models use :predict
    (numberOfImages up to 4, seed supported, no reference input).
    payload["seeds"] holds the effective seed per image (None when unused).
    """
    model = _check_model_for_provider("gemini", model)
    if not api_key:
        raise RuntimeError(
            "missing gemini API key (set GEMINI_API_KEY, use --api-key, "
            "or save it with --provider gemini --remember-key)"
        )
    _ = output_format
    headers = _gemini_headers(api_key)
    start = time.perf_counter()
    start_ts = time.time()
    if model.startswith(GEMINI_IMAGEN_PREFIX):
        if references:
            raise RuntimeError(
                f"modelo {model!r} via Gemini (:predict) não aceita imagens de "
                "referência; limpe Memory dir ou use um modelo gemini-*-image"
            )
        url = GEMINI_PREDICT_URL.format(model=urllib.parse.quote(model, safe=""))
        closest = _closest_ratio(aspect_ratio, GEMINI_ASPECTS) or "1:1"
        base: dict = {
            "instances": [{"prompt": prompt}],
            "parameters": {
                "sampleCount": 1,
                "aspectRatio": closest,
                "sampleImageSize": IMAGEN_SIZES.get(resolution, "1K"),
            },
        }
        send_seed = seed is not None
        per_call = 4

        def single_predict(n: int, index: int) -> tuple[dict, int | None]:
            one = {"instances": base["instances"],
                   "parameters": dict(base["parameters"], sampleCount=n)}
            effective = seed + index if send_seed and seed is not None else None
            if send_seed:
                one["parameters"]["seed"] = effective
            status, raw = _post_json(url, one, headers, timeout_s, cancel_event)
            if status != 200:
                if _is_content_policy_refusal(status, raw):
                    raise ContentPolicyError(
                        _content_policy_message(model, prompt, status, raw, 0,
                                                provider_label="Google")
                    )
                raise RuntimeError(f"Gemini HTTP {status}: {raw[:2000]}")
            payload = json.loads(raw)
            items = []
            for pred in payload.get("predictions") or []:
                if isinstance(pred, dict) and pred.get("bytesBase64Encoded"):
                    item = {"b64_json": pred["bytesBase64Encoded"]}
                    if pred.get("mimeType"):
                        item["media_type"] = pred["mimeType"]
                    items.append(item)
            payload["data"] = items
            return payload, effective

        merged, _seeds = _fan_out_requests(count, per_call, single_predict)
        return merged, start_ts, time.perf_counter() - start

    closest = _closest_ratio(aspect_ratio, GEMINI_ASPECTS) or "1:1"
    if seed is not None:
        print(f"warning: modelo {model!r} via Gemini (generateContent) não "
              "suporta seed; a seed pedida foi ignorada", file=sys.stderr)
    url = GEMINI_GENERATE_URL.format(model=urllib.parse.quote(model, safe=""))
    parts = _gemini_image_parts(prompt, references)
    base = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {
                "aspectRatio": closest,
                "imageSize": GEMINI_SIZES.get(resolution, "1K"),
            },
        },
    }

    def single_generate(_n: int, _index: int) -> tuple[dict, None]:
        status, raw = _post_json(url, base, headers, timeout_s, cancel_event)
        if status != 200:
            if _is_content_policy_refusal(status, raw):
                raise ContentPolicyError(
                    _content_policy_message(model, prompt, status, raw,
                                            len(references), provider_label="Google")
                )
            raise RuntimeError(f"Gemini HTTP {status}: {raw[:2000]}")
        payload = json.loads(raw)
        payload["data"] = _gemini_parse_images(payload, model)
        return payload, None

    merged, _seeds = _fan_out_requests(count, 1, single_generate)
    return merged, start_ts, time.perf_counter() - start


REQUEST_FUNCS = {
    "openrouter": request_openrouter,
    "gemini": request_gemini,
    "openai": request_openai,
}


def extension_for(media_type: str | None, fallback: str) -> str:
    mapping = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
    if media_type in mapping:
        return mapping[media_type]
    if fallback == "jpeg":
        return "jpg"
    return fallback


def save_images(
    output_dir: Path, payload: dict, output_format: str, stamp: str, request_start: float,
    collected: list | None = None,
) -> tuple[list[Path], float]:
    """Decode b64_json images to disk. Returns (paths, receive_ts).

    Appends each file to collected (if given) right after writing, so
    cancellation can remove files saved before the abort.
    """
    items = payload.get("data", [])
    if not items:
        raise RuntimeError(f"API returned no images: {str(payload)[:500]}")
    paths: list[Path] = []
    for index, item in enumerate(items):
        b64 = item.get("b64_json", "")
        if not b64:
            continue
        ext = extension_for(item.get("media_type"), output_format)
        suffix = "" if len(items) == 1 else f"_{index + 1}"
        path = output_dir / f"image_{stamp}{suffix}.{ext}"
        counter = 1
        while path.exists():
            path = output_dir / f"image_{stamp}{suffix}_{counter}.{ext}"
            counter += 1
        path.write_bytes(base64.b64decode(b64))
        paths.append(path)
        if collected is not None:
            collected.append(path)
    receive_ts = time.time()
    _ = request_start
    return paths, receive_ts


# ---------------------------------------------------------------------------
# CSV log
# ---------------------------------------------------------------------------

def read_log_rows(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    rows: list[dict] = []
    with log_path.open("r", encoding="utf-8", newline="") as fh:
        lines = [ln for ln in fh if not ln.startswith("#")]
    if not lines:
        return []
    reader = csv.DictReader(lines)
    for row in reader:
        if row.get("date") or row.get("prompt_summary") or row.get("image_file"):
            rows.append({k: row.get(k, "") for k in LOG_FIELDS})
    return rows


def _log_row_timestamp(row: dict) -> float:
    try:
        return dt.datetime.fromisoformat(str(row.get("date") or "")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return float("-inf")


def sort_log_rows_newest_first(rows: list[dict]) -> list[dict]:
    """Order log rows by date, newest first (ties keep reverse file order,
    so the last appended row of a batch stays on top)."""
    return sorted(reversed(rows), key=_log_row_timestamp, reverse=True)


def collect_log_rows(log_paths: list[Path]) -> list[dict]:
    """Read and merge several CSV logs (duplicates skipped), newest first."""
    rows: list[dict] = []
    seen: set[Path] = set()
    for log_path in log_paths:
        try:
            key = Path(log_path).expanduser().resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        rows.extend(read_log_rows(key))
    return sort_log_rows_newest_first(rows)


def write_log(log_path: Path, rows: list[dict]) -> tuple[int, float]:
    total_cost = 0.0
    for row in rows:
        try:
            total_cost += float(row.get("cost_usd") or 0)
        except ValueError:
            continue
    updated = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with log_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# total_operations={len(rows)}; total_cost_usd={total_cost:.6f}; updated_at={updated}\n")
        fh.write("# cost_source=openrouter usage.cost per operation listed below\n")
        writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows), total_cost


def append_log_entries(log_path: Path, entries: list[dict]) -> tuple[int, float]:
    rows = read_log_rows(log_path)
    rows.extend(entries)
    return write_log(log_path, rows)


# ---------------------------------------------------------------------------
# Analyse: compare folders side by side and copy the chosen images
# ---------------------------------------------------------------------------

CHOSEN_DIRNAME = "chosen"
REPORT_MD = "report.md"
REPORT_CSV = "report.csv"
REPORT_FIELDS = [
    "row", "source_folder", "source_path", "file", "copied_as", "bytes", "width", "height",
    "modified", "prompt_summary", "prompt_full", "model", "provider", "seed",
    "resolution_req", "aspect_ratio_req", "cost_usd", "generated", "alternatives",
]


def default_chosen_dir(output_dir: str | None = None) -> str:
    """<parent of the output dir>/chosen: next to the folders being compared
    (e.g. output .../generated/muse 2 -> .../generated/chosen)."""
    return str(Path(output_dir or default_output_dir()).expanduser().parent / CHOSEN_DIRNAME)


def list_folder_images(folder: str | Path) -> list[Path]:
    """Images directly inside folder, newest first (modification time, then name)."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"folder not found: {folder}")
    images = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(images, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def build_analysis_rows(folders: list[str | Path], newest_first: bool = True
                        ) -> list[list[Path | None]]:
    """Row i = the i-th newest (or oldest) image of every folder; a folder with
    fewer images leaves None in its cell. One column per folder."""
    columns = [list_folder_images(f) for f in folders]
    if not newest_first:
        columns = [list(reversed(c)) for c in columns]
    height = max((len(c) for c in columns), default=0)
    return [[c[i] if i < len(c) else None for c in columns] for i in range(height)]


THUMB_SIZE = 150
THUMB_ZOOM_LEVELS = (100, 150, 220, 320, 480)  # Analyse tab Zoom -/+ (pixels)
PREVIEW_STEP = 80  # hover preview sizes are multiples of this (better cache reuse)
PREVIEW_GAP = 28   # pixels between the pointer and the preview window
PREVIEW_CAPTION_H = 80


def preview_geometry(img_w: int, img_h: int, pointer_x: int, screen_w: int,
                     screen_h: int) -> tuple[int, str]:
    """Longest side (px) and side ("right"/"left" of the pointer) of the Analyse
    hover preview: as big as the wider free side of the screen allows (at most
    75% of its width, 85% of its height minus the caption), never larger than
    the image itself and never over the pointer (that would fire <Leave> and
    make the preview blink)."""
    right = screen_w - pointer_x - PREVIEW_GAP - 8
    left = pointer_x - PREVIEW_GAP - 8
    side = "right" if right >= left else "left"
    box_w = min(screen_w * 0.75, max(right, left))
    box_h = screen_h * 0.85 - PREVIEW_CAPTION_H
    img_w, img_h = max(1, img_w), max(1, img_h)
    scale = min(box_w / img_w, box_h / img_h)
    if scale >= 1:  # fits whole: show it at its own size
        return max(img_w, img_h), side
    longest = max(img_w, img_h) * scale
    return max(2 * PREVIEW_STEP, int(longest // PREVIEW_STEP) * PREVIEW_STEP), side


def thumbnail_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / VAULT_DIRNAME / "thumbs"


def make_thumbnail(source: str | Path, size: int = THUMB_SIZE) -> Path | None:
    """Small PNG copy of an image for the Analyse grid (Tk shows only PNG/GIF
    and full-size images would use a lot of memory). Cached by path, mtime
    and size; made with ImageMagick `convert` or `ffmpeg`. None when neither
    tool works (the GUI then subsamples PNG/GIF itself)."""
    src = Path(source)
    try:
        st = src.stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{src.resolve()}|{st.st_mtime_ns}|{st.st_size}|{size}".encode()).hexdigest()
    out = thumbnail_cache_dir() / f"{key}.png"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    commands = []
    if shutil.which("convert"):
        commands.append(["convert", f"{src}[0]", "-thumbnail", f"{size}x{size}", str(out)])
    if shutil.which("ffmpeg"):
        commands.append(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-frames:v", "1",
                         "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                         str(out)])
    for command in commands:
        try:
            subprocess.run(command, capture_output=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.is_file():
            return out
    return None


def parse_pick(spec: str, folder_count: int) -> tuple[int, int]:
    """CLI "ROW:COL" (1-based; COL = folder position) -> (row_index, folder_index)."""
    row_text, sep, col_text = spec.partition(":")
    if not sep or not row_text.strip().isdigit() or not col_text.strip().isdigit():
        raise ValueError(f"invalid --choose {spec!r} (expected ROW:COL, e.g. 3:2)")
    row, col = int(row_text), int(col_text)
    if row < 1 or not 1 <= col <= folder_count:
        raise ValueError(f"invalid --choose {spec!r}: row >= 1 and column 1..{folder_count}")
    return row - 1, col - 1


def _log_index(folder: Path, parents: int = 2) -> dict[str, dict]:
    """image_file -> generation log row for the images of folder.

    Looks at the folder's own log_image_generate.csv first and then at the
    logs of up to `parents` folders above it (images are often moved into
    subfolders while the log stays behind). File names carry a timestamp, so
    matching by name is safe; the nearest log wins.
    """
    index: dict[str, dict] = {}
    current = folder
    for _level in range(parents + 1):
        for row in read_log_rows(current / LOG_FILENAME):
            index.setdefault(row.get("image_file", ""), row)
        if current.parent == current:
            break
        current = current.parent
    return index


def choose_images(
    folders: list[str | Path],
    picks: dict[int, int],
    chosen_root: str | Path,
    newest_first: bool = True,
) -> dict:
    """Copy the picked images ({row_index: folder_index}, one per row) into a new
    <chosen_root>/<timestamp>/ folder with report.md + report.csv describing
    each one (origin, size, date, prompt/model/seed from the source log, and
    the alternatives it was chosen over). Originals are only read.
    Returns {"folder", "report_md", "report_csv", "rows"}.
    """
    if not picks:
        raise ValueError("no image selected")
    folders = [Path(f).expanduser() for f in folders]
    rows = build_analysis_rows(folders, newest_first)
    for row_index, folder_index in picks.items():
        if not 0 <= row_index < len(rows) or not 0 <= folder_index < len(folders):
            raise ValueError(f"selection out of range: row {row_index + 1}, "
                             f"column {folder_index + 1}")
        if rows[row_index][folder_index] is None:
            raise ValueError(f"row {row_index + 1} has no image in column {folder_index + 1} "
                             f"({folders[folder_index].name})")
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest = Path(chosen_root).expanduser() / stamp
    counter = 2
    while dest.exists():
        dest = Path(chosen_root).expanduser() / f"{stamp}_{counter}"
        counter += 1
    dest.mkdir(parents=True)
    logs = [_log_index(f) for f in folders]
    width = len(str(len(rows)))
    report_rows: list[dict] = []
    for row_index in sorted(picks):
        folder_index = picks[row_index]
        source = rows[row_index][folder_index]
        assert source is not None
        folder = folders[folder_index]
        copied_as = f"{row_index + 1:0{width}d}_{folder.name}_{source.name}"
        shutil.copy2(source, dest / copied_as)
        size, img_w, img_h = inspect_image(source)
        log = logs[folder_index].get(source.name, {})
        alternatives = [f"{folders[i].name}/{cell.name}" for i, cell in enumerate(rows[row_index])
                        if cell is not None and i != folder_index]
        report_rows.append({
            "row": str(row_index + 1),
            "source_folder": str(folder),
            "source_path": str(source),
            "file": source.name,
            "copied_as": copied_as,
            "bytes": str(size),
            "width": str(img_w),
            "height": str(img_h),
            "modified": dt.datetime.fromtimestamp(source.stat().st_mtime).astimezone()
                        .isoformat(timespec="seconds"),
            "prompt_summary": log.get("prompt_summary", ""),
            "prompt_full": log.get("prompt_full", ""),
            "model": log.get("model", ""),
            "provider": log.get("provider", ""),
            "seed": log.get("seed", ""),
            "resolution_req": log.get("resolution_req", ""),
            "aspect_ratio_req": log.get("aspect_ratio_req", ""),
            "cost_usd": log.get("cost_usd", ""),
            "generated": log.get("date", ""),
            "alternatives": "; ".join(alternatives),
        })
    with (dest / REPORT_CSV).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(report_rows)
    (dest / REPORT_MD).write_text(
        analysis_report_markdown(folders, rows, picks, report_rows, newest_first, dest),
        encoding="utf-8")
    return {"folder": str(dest), "report_md": str(dest / REPORT_MD),
            "report_csv": str(dest / REPORT_CSV), "rows": report_rows}


def analysis_report_markdown(folders: list[Path], rows: list[list[Path | None]],
                             picks: dict[int, int], report_rows: list[dict],
                             newest_first: bool, dest: Path) -> str:
    """Human-readable report of a Choose: what was compared and what was kept."""
    created = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    counts = [sum(1 for row in rows if row[i] is not None) for i in range(len(folders))]
    chosen_per_folder = [sum(1 for f in picks.values() if f == i) for i in range(len(folders))]
    lines = [
        "# Chosen images", "",
        f"- Created: {created}",
        f"- Destination: `{dest}`",
        f"- Order: {'newest first' if newest_first else 'oldest first'} "
        "(row N = the N-th image of every folder by modification time)",
        f"- Rows compared: {len(rows)}; chosen: {len(picks)}; "
        f"rows without a choice: {len(rows) - len(picks)}",
        "", "## Folders compared", "",
        "| # | Folder | Images | Chosen | Share of choices |", "|---|---|---|---|---|",
    ]
    for i, folder in enumerate(folders):
        share = f"{100 * chosen_per_folder[i] / len(picks):.0f}%" if picks else "-"
        lines.append(f"| {i + 1} | `{folder}` | {counts[i]} | {chosen_per_folder[i]} | {share} |")
    lines += ["", "## Chosen images", "",
              "| Row | From | File | Copied as | Size | Modified | Model | Seed |",
              "|---|---|---|---|---|---|---|---|"]
    for r in report_rows:
        lines.append(f"| {r['row']} | {Path(r['source_folder']).name} | {r['file']} | "
                     f"{r['copied_as']} | {r['width']}x{r['height']} ({r['bytes']} B) | "
                     f"{r['modified'][:19].replace('T', ' ')} | {r['model'] or '-'} | "
                     f"{r['seed'] or '-'} |")
    lines += ["", "## Details", ""]
    for r in report_rows:
        lines += [f"### Row {r['row']} - {r['copied_as']}", "",
                  f"- Source: `{r['source_path']}`",
                  f"- Chosen over: {r['alternatives'] or '(no other image in this row)'}"]
        if r["prompt_full"]:
            lines += [f"- Generated: {r['generated'][:19].replace('T', ' ')} with "
                      f"{r['provider'] or '?'} / {r['model'] or '?'} "
                      f"(aspect {r['aspect_ratio_req'] or '?'}, resolution "
                      f"{r['resolution_req'] or '?'}, seed {r['seed'] or '-'}, "
                      f"cost ${r['cost_usd'] or '0'})",
                      f"- Prompt: {r['prompt_full']}"]
        else:
            lines.append("- No generation info (image not found in the folder's "
                         f"{LOG_FILENAME})")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core generation pipeline (shared by CLI and GUI)
# ---------------------------------------------------------------------------

def run_generation(
    *,
    prompt: str,
    output_dir: str,
    context_dir: str | None,
    memory_dir: str | None,
    model: str,
    aspect_ratio: str,
    resolution: str,
    output_format: str,
    seed: int | None,
    count: int,
    api_key: str | None,
    timeout_s: int = REQUEST_TIMEOUT_S,
    dry_run: bool = False,
    summary_model: str = "",
    provider: str = DEFAULT_PROVIDER,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Run one generation request and update the CSV log. Returns result dict.

    If cancel_event is set (or abort_all_http() closes the sockets), both
    in-flight requests abort, partial files are removed, nothing is logged,
    and GenerationCancelled is raised.
    """
    provider = normalize_provider(provider)
    if not prompt.strip():
        raise ValueError("prompt is empty")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    context_text = load_context_text(context_dir)
    references = load_memory_references(memory_dir)
    prompt_caps: dict | None = None
    if provider == "openrouter" and not dry_run:
        try:
            prompt_caps = _model_capabilities(model, api_key or "", cancel_event)
        except GenerationCancelled:
            raise
        except Exception:
            prompt_caps = None
    final_prompt = build_final_prompt(prompt, context_text, aspect_ratio, resolution,
                                      prompt_caps)

    request_start = time.time()
    t0 = time.perf_counter()
    # Image request and summary request run in parallel threads so the
    # summary does not add latency to the generation.
    image_box: dict = {}
    summary_box: dict = {}

    def is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def fetch_images() -> None:
        try:
            local_paths: list[Path] = []
            # Registered upfront so cancellation removes files saved so far.
            image_box["paths"] = local_paths
            local_cost = 0.0
            local_created = ""
            local_raw: int | float | str = ""
            payload: dict = {}
            if dry_run:
                stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                width, height = target_dimensions(aspect_ratio, resolution)
                for i in range(max(1, count)):
                    if is_cancelled():
                        raise GenerationCancelled("generation cancelled")
                    suffix = "" if count == 1 else f"_{i + 1}"
                    path = out / f"image_{stamp}{suffix}.{output_format if output_format != 'jpeg' else 'jpg'}"
                    counter = 1
                    while path.exists():
                        path = out / f"image_{stamp}{suffix}_{counter}.png"
                        counter += 1
                    write_placeholder_png(path, width, height)
                    local_paths.append(path)
                local_created = dt.datetime.now().astimezone().isoformat(timespec="seconds")
                local_raw = local_created
                image_box["seeds"] = [seed] * len(local_paths)
            else:
                if not api_key:
                    env_var = PROVIDERS[provider]["env_var"]
                    raise RuntimeError(
                        f"missing {provider} API key (set {env_var}, use --api-key, "
                        f"or save it with --provider {provider} --remember-key)"
                    )
                request_func = REQUEST_FUNCS[provider]
                payload, _, _ = request_func(
                    api_key=api_key,
                    model=model,
                    prompt=final_prompt,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    references=references,
                    output_format=output_format,
                    seed=seed,
                    count=count,
                    timeout_s=timeout_s,
                    cancel_event=cancel_event,
                )
                stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                local_paths, receive_ts = save_images(
                    out, payload, output_format, stamp, request_start, local_paths)
                _ = receive_ts
                usage = payload.get("usage", {}) if isinstance(payload.get("usage"), dict) else {}
                try:
                    local_cost = float(usage.get("cost") or 0.0)
                except (TypeError, ValueError):
                    local_cost = 0.0
                local_raw = payload.get("created", "")
                try:
                    local_created = dt.datetime.fromtimestamp(
                        float(local_raw), tz=dt.timezone.utc
                    ).astimezone().isoformat(timespec="seconds")
                except (TypeError, ValueError, OSError, OverflowError):
                    local_created = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            image_box["paths"] = local_paths
            image_box["cost"] = local_cost
            image_box["created"] = local_created
            image_box["raw"] = local_raw
            image_box["elapsed"] = time.perf_counter() - t0
            image_box["seeds"] = payload.get("seeds") or []
        except Exception as exc:
            image_box["error"] = exc

    def fetch_summary() -> None:
        if not summary_model or not summary_model.strip():
            summary_box["summary"] = "No model selected"
            return
        # summarize_prompt already falls back to truncation on any failure
        # (except cancellation, which propagates).
        try:
            summary_box["summary"] = summarize_prompt(
                prompt,
                api_key=None if dry_run else api_key,
                model=summary_model,
                timeout_s=SUMMARY_TIMEOUT_S,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            summary_box["error"] = exc

    def discard_partials() -> None:
        for key in ("paths",):
            for path in image_box.get(key, []):
                try:
                    Path(path).unlink()
                except OSError:
                    pass

    image_thread = threading.Thread(target=fetch_images, daemon=True)
    summary_thread = threading.Thread(target=fetch_summary, daemon=True)
    image_thread.start()
    summary_thread.start()
    image_thread.join()
    summary_thread.join()
    if is_cancelled() or isinstance(image_box.get("error"), GenerationCancelled):
        discard_partials()
        raise GenerationCancelled("generation cancelled")
    if "error" in image_box:
        raise image_box["error"]
    if isinstance(summary_box.get("error"), GenerationCancelled):
        discard_partials()
        raise summary_box["error"]
    if "summary" not in summary_box:
        summary_box["summary"] = truncate_prompt(prompt)
    paths = image_box["paths"]
    cost = image_box["cost"]
    created_iso = image_box["created"]
    api_created_raw = image_box["raw"]
    elapsed = image_box["elapsed"]
    summary = summary_box["summary"]

    entries: list[dict] = []
    finished_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    per_image_cost = cost / len(paths) if paths and cost else (0.0 if not paths else cost)
    effective_seeds = image_box.get("seeds") or []
    for index, path in enumerate(paths):
        size_bytes, width, height = inspect_image(path)
        effective = (effective_seeds[index] if index < len(effective_seeds)
                     else seed)
        entries.append(
            {
                "date": finished_iso,
                "prompt_summary": summary,
                "prompt_full": prompt.strip(),
                "image_file": path.name,
                "image_bytes": str(size_bytes),
                "width": str(width),
                "height": str(height),
                "resolution_req": resolution,
                "aspect_ratio_req": aspect_ratio,
                "seed": "" if effective is None else str(effective),
                "cost_usd": f"{per_image_cost:.6f}",
                "generation_timestamp": str(created_iso or api_created_raw),
                "total_seconds": f"{elapsed:.2f}",
                "model": model,
                "provider": provider,
                "key_hash": key_hash(api_key),
            }
        )

    log_path = out / LOG_FILENAME
    total_ops, total_cost = append_log_entries(log_path, entries)

    return {
        "images": [str(p) for p in paths],
        "entries": entries,
        "log_path": str(log_path),
        "elapsed": elapsed,
        "cost": cost,
        "total_ops": total_ops,
        "total_cost": total_cost,
    }


def run_generation_batch(prompts: list[str], dynamic_dirs: dict | None = None,
                         on_progress: Callable[[dict], None] | None = None,
                         **kwargs: object) -> dict:
    """Run one generation per prompt (Injection mode). Aggregates the results.

    kwargs are forwarded to run_generation(); a "count" key is overridden
    to 1 per prompt. Shares the cancel_event: cancelling aborts the whole
    batch, removes partial files and logs nothing.

    dynamic_dirs ({dir_key: range or {"start", "range"}}, see
    check_dynamic_dirs) makes generation i use <base>/<start + (i // batch) % (range -
    start + 1)> for that dir. With a single prompt it is
    repeated kwargs["count"] times (one call each, seed + i like the
    provider fan-out) so every generation can use its own folders.

    on_progress (optional) is called after each finished generation of a
    multi-generation batch with {"done", "total", "images", "log_paths"}
    (already logged to CSV), so UIs can refresh the log live.
    """
    total = len(prompts) if len(prompts) > 1 else int(kwargs.get("count") or 1)  # type: ignore[call-overload]
    dynamic = check_dynamic_dirs(dynamic_dirs, kwargs, total) if dynamic_dirs else {}
    if len(prompts) <= 1 and not dynamic:
        return run_generation(prompt=prompts[0], **kwargs)  # type: ignore[arg-type]
    repeated = len(prompts) <= 1
    if repeated:
        prompts = prompts * total
    seed = kwargs.get("seed")
    images: list[str] = []
    entries: list[dict] = []
    log_paths: list[str] = []
    total_cost = 0.0
    total_elapsed = 0.0
    for index, one_prompt in enumerate(prompts):
        overrides: dict = {"count": 1}
        for key, (start, dir_range, batch) in dynamic.items():
            overrides[key] = dynamic_dir_for(str(kwargs[key]), dir_range, index, start, batch)
        if repeated and isinstance(seed, int):
            overrides["seed"] = seed + index
        result = run_generation(prompt=one_prompt, **{**kwargs, **overrides})  # type: ignore[arg-type]
        images.extend(result["images"])
        entries.extend(result["entries"])
        total_cost += result["cost"]
        total_elapsed += result["elapsed"]
        if result["log_path"] not in log_paths:
            log_paths.append(result["log_path"])
        if on_progress is not None:
            on_progress({"done": index + 1, "total": len(prompts),
                         "images": list(images), "log_paths": list(log_paths)})
    return {
        "images": images,
        "entries": entries,
        "log_path": log_paths[-1] if log_paths else "",
        "log_paths": log_paths,
        "elapsed": total_elapsed,
        "cost": total_cost,
        "total_ops": len(entries),
        "total_cost": total_cost,
    }


def parse_injection_row(spec: str, var_names: list[str]) -> list[str]:
    """Parse "name=value,name=value" into a values list ordered by var_names.

    Unknown variable names raise ValueError. Missing variables become "".
    """
    values: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ValueError(f"invalid --inject item: {part!r} (expected name=value)")
        if name not in var_names:
            raise ValueError(
                f"unknown variable {name!r} (prompt variables: {', '.join(var_names)})")
        values[name] = value.strip()
    return [values.get(name, "") for name in var_names]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate images via OpenRouter (CLI + Tkinter GUI)."
    )
    parser.add_argument("--prompt", default="", help="Text prompt for image generation.")
    parser.add_argument("--prompt-file", default="", help="Read prompt from .txt/.md file.")
    parser.add_argument("--output-dir", default=None,
                        help="Directory to save images + CSV log (default: ImageGenerate in user pictures folder).")
    parser.add_argument("--context-dir", default="", help="Directory with .md/.txt context files.")
    parser.add_argument("--memory-dir", default="", help="Directory with reference images.")
    for name in ("output", "context", "memory"):
        parser.add_argument(f"--dynamic-{name}", dest=f"dynamic_{name}", default=None,
                            metavar="RANGE",
                            help=f"Dynamic {name} dir (needs --count > 1 or several "
                                 f"--inject): generations use <{name}-dir>/START.."
                                 f"<{name}-dir>/RANGE in order, cycling back to START "
                                 "(RANGE = last folder, natural number > START).")
        parser.add_argument(f"--dynamic-{name}-start", dest=f"dynamic_{name}_start",
                            default=None, metavar="START",
                            help=f"First folder of --dynamic-{name} (default: 1).")
        parser.add_argument(f"--dynamic-{name}-batch", dest=f"dynamic_{name}_batch",
                            default=None, metavar="BATCH",
                            help=f"Generations per folder for --dynamic-{name} (default: 1): "
                                 "with 3, generations 1-3 use START, 4-6 use START+1, ...")
    parser.add_argument("--model", default=None,
                        help="Image model slug (default: provider default model).")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, choices=sorted(PROVIDERS),
                        help="Image provider (each has its own key vault).")
    parser.add_argument("--prop", "--aspect-ratio", dest="prop", default="1:1",
                        choices=ASPECT_RATIOS, help="Aspect ratio (also appended to prompt).")
    parser.add_argument("--resolution", default="1K", choices=RESOLUTIONS,
                        help="Resolution tier (also appended to prompt).")
    parser.add_argument("--output-format", default="png", choices=OUTPUT_FORMATS)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--count", "--n", dest="count", type=int, default=1)
    parser.add_argument("--inject", action="append", default=[],
                        metavar="NAME=VALUE[,NAME=VALUE…]",
                        help="Values for {{variables}} in the prompt, one option "
                             "per generation (implies one generation per option). "
                             "Example: --inject 'pessoa=menino,objeto=sorvete'. "
                             "Empty value repeats the previous row / variable name.")
    parser.add_argument("--api-key", default="",
                        help="API key for the provider (else env var, else remembered vault).")
    parser.add_argument("--remember-key", action="store_true",
                        help="Encrypt and remember the key in the provider vault.")
    parser.add_argument("--forget-key", action="store_true",
                        help="Delete the remembered key for the provider and exit.")
    parser.add_argument("--summary-model", default="",
                        help="Chat model used to summarize the prompt for the log "
                             "(empty = local truncation, no API call).")
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT_S)
    parser.add_argument("--dry-run", action="store_true", help="Skip API, write placeholder PNG.")
    parser.add_argument("--gui", action="store_true", help="Force GUI mode.")
    parser.add_argument("--list-log", action="store_true", help="Print CSV log rows and exit.")
    parser.add_argument("--analyse", action="append", default=[], metavar="DIR",
                        help="Analyse: compare the images of these folders (repeat, at least "
                             "1). Row N = the N-th newest image of each folder. Prints the "
                             "table; add --choose to copy picks.")
    parser.add_argument("--choose", action="append", default=[], metavar="ROW:COL",
                        help="With --analyse: pick the image of row ROW in folder COL "
                             "(1-based, one per row; repeat). Copies them into a new "
                             f"<chosen-dir>/<timestamp>/ with {REPORT_MD} + {REPORT_CSV}.")
    parser.add_argument("--oldest-first", action="store_true",
                        help="With --analyse: align rows from the oldest image instead.")
    parser.add_argument("--chosen-dir", default=None,
                        help=f"With --choose: where the '{CHOSEN_DIRNAME}' copies go "
                             f"(default: <parent of output-dir>/{CHOSEN_DIRNAME}).")
    return parser


def resolve_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        text = Path(args.prompt_file).read_text(encoding="utf-8", errors="replace")
        if args.prompt:
            return f"{args.prompt}\n\n{text}"
        return text
    return args.prompt


def analyse_cli(args: argparse.Namespace, output_dir: str) -> int:
    """--analyse [--choose ROW:COL ...]: print the comparison table / copy picks."""
    folders = [Path(f).expanduser() for f in args.analyse]
    newest_first = not getattr(args, "oldest_first", False)
    try:
        rows = build_analysis_rows(folders, newest_first)
        picks: dict[int, int] = {}
        for spec in getattr(args, "choose", []) or []:
            row, col = parse_pick(spec, len(folders))
            if row in picks:
                raise ValueError(f"row {row + 1} picked twice (only one image per row)")
            picks[row] = col
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not picks:
        print("row  " + "  |  ".join(f"[{i + 1}] {f.name}" for i, f in enumerate(folders)))
        for index, row in enumerate(rows, start=1):
            print(f"{index:>3}  " + "  |  ".join(cell.name if cell else "-" for cell in row))
        counts = ", ".join(f"{f.name}: {sum(1 for r in rows if r[i])}"
                           for i, f in enumerate(folders))
        print(f"{len(rows)} rows ({'newest' if newest_first else 'oldest'} first) | {counts}")
        print("pick with --choose ROW:COL (e.g. --choose 1:2)")
        return 0
    try:
        result = choose_images(folders, picks, args.chosen_dir or default_chosen_dir(output_dir),
                               newest_first)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"copied {len(result['rows'])} image(s) to {result['folder']}")
    print(f"report: {result['report_md']} (+ {REPORT_CSV})")
    return 0


def main_cli(args: argparse.Namespace) -> int:
    output_dir = args.output_dir or default_output_dir()
    provider = normalize_provider(args.provider)
    if args.forget_key:
        if forget_remembered_key(provider):
            print(f"forgot remembered key for {provider}")
        else:
            print(f"no remembered key for {provider}")
        return 0
    api_key, source = resolve_api_key(provider, args.api_key or None)
    if args.remember_key:
        if not args.api_key and source == "env":
            api_key = os.environ.get(PROVIDERS[provider]["env_var"], "").strip() or None
        if not api_key:
            print("error: nothing to remember (pass --api-key or set env var)", file=sys.stderr)
            return 2
        try:
            blob = save_remembered_key(provider, api_key)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"remembered {provider} key in {blob}")
    if getattr(args, "analyse", None):
        return analyse_cli(args, output_dir)
    if args.list_log:
        log_path = Path(output_dir) / LOG_FILENAME
        rows = read_log_rows(log_path)
        if not rows:
            print(f"no log entries at {log_path}")
            return 0
        writer = csv.DictWriter(sys.stdout, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        return 0
    prompt = resolve_prompt(args)
    if not prompt.strip():
        print("error: provide --prompt or --prompt-file (or use --gui)", file=sys.stderr)
        return 2
    try:
        count = parse_count(args.count)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    var_names = extract_template_vars(prompt)
    prompts = [prompt]
    if args.inject:
        if not var_names:
            print("error: --inject given but the prompt has no {{variables}}",
                  file=sys.stderr)
            return 2
        try:
            cells = [parse_injection_row(spec, var_names) for spec in args.inject]
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not any(cell.strip() for row in cells for cell in row):
            print("error: all --inject rows are empty", file=sys.stderr)
            return 2
        rows = resolve_injection_rows(cells, var_names)
        prompts = [apply_template_values(prompt, dict(zip(var_names, row)))
                   for row in rows]
        count = len(prompts)
    elif count > 1 and var_names:
        print("error: prompt has {{variables}} and count > 1: pass one --inject "
              "'name=value,…' option per generation (or use the GUI Injection tab)",
              file=sys.stderr)
        return 2
    dynamic_dirs = {f"{name}_dir": {"start": getattr(args, f"dynamic_{name}_start", None),
                                    "range": getattr(args, f"dynamic_{name}", None),
                                    "batch": getattr(args, f"dynamic_{name}_batch", None)}
                    for name in ("output", "context", "memory")
                    if any(getattr(args, f"dynamic_{name}{suffix}", None) is not None
                           for suffix in ("", "_start", "_batch"))}
    try:
        check_dynamic_dirs(dynamic_dirs, {"output_dir": output_dir,
                                          "context_dir": args.context_dir,
                                          "memory_dir": args.memory_dir}, count)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    model = args.model or PROVIDERS[provider]["default_model"]
    print(f"[{started}] requesting provider={provider} model={model} "
          f"prop={args.prop} res={args.resolution} (key: {source}) ...")
    t0 = time.perf_counter()
    print("press Ctrl+C to cancel")
    cancel_event = threading.Event()
    prev_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(signum: int, frame: object) -> None:
        cancel_event.set()
        abort_all_http()

    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        result = run_generation_batch(
            prompts,
            dynamic_dirs=dynamic_dirs,
            output_dir=output_dir,
            context_dir=args.context_dir or None,
            memory_dir=args.memory_dir or None,
            model=model,
            aspect_ratio=args.prop,
            resolution=args.resolution,
            output_format=args.output_format,
            seed=args.seed,
            count=count,
            api_key=api_key,
            timeout_s=args.timeout,
            dry_run=args.dry_run,
            summary_model=args.summary_model,
            provider=provider,
            cancel_event=cancel_event,
        )
    except GenerationCancelled:
        print("cancelled: partial files removed, nothing logged")
        return 130
        signal.signal(signal.SIGINT, prev_sigint)
    print(f"done in {result['elapsed']:.1f}s (cli measured {time.perf_counter() - t0:.1f}s)")
    for image in result["images"]:
        print(f"saved: {image}")
    for extra_log in result.get("log_paths", [])[:-1]:
        print(f"log: {extra_log}")
    print(f"log: {result['log_path']} | ops={result['total_ops']} total_cost=${result['total_cost']:.6f}")
    return 0


# ---------------------------------------------------------------------------
# GUI (tkinter, stdlib)
# ---------------------------------------------------------------------------

def run_gui(defaults: dict | None = None) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    defaults = defaults or {}
    # Precedence: hard defaults < config.json < explicit caller defaults.
    saved = sanitize_gui_config(load_gui_config())
    merged = {**saved, **{k: v for k, v in defaults.items() if v not in (None, "")}}
    # WM_CLASS="ImageGenerate" so Ubuntu/GNOME dock shows our name
    # instead of the default "tk" gear entry.
    root = tk.Tk(className="ImageGenerate")
    root.title("ImageGenerate")
    try:
        root.iconname("ImageGenerate")
    except tk.TclError:
        pass
    root.geometry("850x600")

    # Modern button colors for Generate (light green) and Cancel (light red)
    style = ttk.Style()
    style.configure("Generate.TButton", background="#86efac", foreground="#052e16",
                    font=("TkDefaultFont", 10, "bold"), padding=6)
    style.map("Generate.TButton", background=[("active", "#a7f3d0"), ("disabled", "#d4f5e6")])
    style.configure("Cancel.TButton", background="#f28b82", foreground="#3a0a0a",
                    font=("TkDefaultFont", 10), padding=6)
    style.map("Cancel.TButton", background=[("active", "#f8a8a0"), ("disabled", "#f5ccc8")])
    style.configure("Help.TButton", background="#8ab4f8", foreground="#052e16",
                    font=("TkDefaultFont", 10, "bold"), padding=4)
    style.map("Help.TButton", background=[("active", "#b0c8f5"), ("disabled", "#c8d8f5")])
    style.configure("MuteOff.TButton", background="#c8f0d8", foreground="#052e16",
                    font=("TkDefaultFont", 9), padding=4)
    style.map("MuteOff.TButton", background=[("active", "#d4f5e6")])
    style.configure("MuteOn.TButton", background="#f5ccc8", foreground="#3a0a0a",
                    font=("TkDefaultFont", 9), padding=4)
    style.map("MuteOn.TButton", background=[("active", "#f8d0c8")])

    # Brand logo (img/logo.png next to this file): window icon + header.
    # Missing/corrupt file -> plain text header, never blocks startup.
    state: dict = {"running": False, "start": 0.0, "elapsed": 0.0, "after_id": None,
                   "logo_img": None, "batch_logs": []}
    try:
        _logo_path = Path(__file__).resolve().parent / "img/logo.png"
        if _logo_path.is_file():
            _logo = tk.PhotoImage(file=str(_logo_path))
            if _logo.width() > 48 or _logo.height() > 48:
                _logo = _logo.subsample(max(1, _logo.width() // 48 + 1),
                                        max(1, _logo.height() // 48 + 1))
            state["logo_img"] = _logo
            try:
                root.iconphoto(True, _logo)
            except tk.TclError:
                pass
    except (tk.TclError, OSError):
        state["logo_img"] = None

    if state["logo_img"] is not None:
        header = ttk.Frame(root, padding=(8, 8, 8, 0))
        header.pack(fill=tk.X)
        ttk.Label(header, image=state["logo_img"]).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(header, text="ImageGenerate",
                  font=("", 14, "bold")).pack(side=tk.LEFT)
    else:
        header = ttk.Frame(root, padding=(8, 8, 8, 0))
        header.pack(fill=tk.X)
        ttk.Label(header, text="ImageGenerate",
                  font=("", 14, "bold")).pack(side=tk.LEFT)

    def open_docs() -> None:
        """Open docs.html (repo root) in the default browser."""
        import webbrowser

        docs = Path(__file__).resolve().parent / "docs.html"
        if docs.is_file():
            webbrowser.open(docs.as_uri())
        else:
            messagebox.showwarning("Docs", f"docs.html not found:\n{docs}")

    ttk.Button(header, text="?", width=3, command=open_docs, style="Help.TButton").pack(side=tk.RIGHT)

    def pick_dir(var: tk.StringVar) -> None:
        """Open the folder dialog at the typed folder; if it does not exist,
        warn and open at its nearest existing parent (empty -> dialog default)."""
        typed = var.get().strip()
        start = nearest_existing_dir(typed)
        if typed and (start is None or start != Path(typed).expanduser()):
            messagebox.showerror(
                "Folder not found",
                f"Folder does not exist:\n{typed}\n\n"
                + (f"Opening the nearest existing parent:\n{start}" if start
                   else "Opening the default folder."))
        options = {"initialdir": str(start)} if start else {}
        chosen = filedialog.askdirectory(**options)
        if chosen:
            var.set(chosen)

    # ---- tabbed layout: Generate / Model / Dir ----
    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
    tab_generate = ttk.Frame(notebook, padding=8)
    tab_model = ttk.Frame(notebook, padding=8)
    tab_dir = ttk.Frame(notebook, padding=8)
    notebook.add(tab_generate, text="Generate")
    notebook.add(tab_model, text="Model")
    notebook.add(tab_dir, text="Dir")
    tab_injection = ttk.Frame(notebook, padding=8)
    notebook.add(tab_injection, text="Injection")
    notebook.hide(tab_injection)
    tab_analyse = ttk.Frame(notebook, padding=8)
    notebook.add(tab_analyse, text="Analyse")
    injection_entries: list[list[ttk.Entry]] = []

    out_var = tk.StringVar(value=str(merged.get("output_dir") or default_output_dir()))
    ctx_var = tk.StringVar(value=str(merged.get("context_dir", "")))
    mem_var = tk.StringVar(value=str(merged.get("memory_dir", "")))
    provider_var = tk.StringVar(value=str(merged.get("provider") or DEFAULT_PROVIDER))
    try:
        provider_var.set(normalize_provider(provider_var.get()))
    except ValueError:
        provider_var.set(DEFAULT_PROVIDER)
    model_var = tk.StringVar(value=str(
        merged.get("model") or PROVIDERS[provider_var.get()]["default_model"]
    ))
    summary_var = tk.StringVar(value=str(merged.get("summary_model", "")))
    prop_var = tk.StringVar(value=str(merged.get("prop", "1:1")))
    res_var = tk.StringVar(value=str(merged.get("resolution", "1K")))
    fmt_var = tk.StringVar(value=str(merged.get("output_format", "png")))
    seed_var = tk.StringVar(value="")
    count_var = tk.StringVar(value="1")
    _initial_key, _initial_source = resolve_api_key(provider_var.get())
    key_var = tk.StringVar(value=_initial_key or "")
    remember_var = tk.BooleanVar(value=_initial_source == "vault")
    dry_var = tk.BooleanVar(value=bool(merged.get("dry_run", False)))

    def reload_key_for_provider(update_model: bool = True) -> None:
        try:
            current = normalize_provider(provider_var.get())
        except ValueError:
            return
        if update_model and not model_var.get().strip():
            model_var.set(PROVIDERS[current]["default_model"])
        key, source = resolve_api_key(current)
        key_var.set(key or "")
        remember_var.set(source == "vault")

    def attach_help(widget: object, text: str) -> None:
        """Short hover tooltip explaining a widget to new users."""
        tip: dict = {"window": None}

        def show(_event: object = None) -> None:
            hide()
            window = tk.Toplevel(root)
            window.wm_overrideredirect(True)
            window.wm_attributes("-topmost", True)
            label = ttk.Label(window, text=text, wraplength=280, justify=tk.LEFT,
                              background="#ffffe0", relief=tk.SOLID, borderwidth=1)
            label.pack(padx=2, pady=2)
            x = widget.winfo_rootx() + 16  # type: ignore[attr-defined]
            y = widget.winfo_rooty() + widget.winfo_height() + 4  # type: ignore[attr-defined]
            window.wm_geometry(f"+{x}+{y}")
            tip["window"] = window

        def hide(_event: object = None) -> None:
            window = tip.get("window")
            if window is not None:
                try:
                    window.destroy()  # type: ignore[attr-defined]
                except tk.TclError:
                    pass
                tip["window"] = None

        widget.bind("<Enter>", show)  # type: ignore[attr-defined]
        widget.bind("<Leave>", hide)  # type: ignore[attr-defined]

    VAULT_HELP = str(vault_dir())

    # ---- Model tab: provider, model, api key ----
    ttk.Label(tab_model, text="Provider:").grid(row=0, column=0, sticky=tk.W, padx=4, pady=2)
    provider_combo = ttk.Combobox(tab_model, textvariable=provider_var,
                                  values=sorted(PROVIDERS), width=58, state="readonly")
    provider_combo.grid(row=0, column=1, sticky=tk.EW, padx=4)
    provider_combo.bind("<<ComboboxSelected>>", lambda _e: reload_key_for_provider())
    ttk.Label(tab_model, text="Model:").grid(row=1, column=0, sticky=tk.W, padx=4, pady=2)
    ttk.Entry(tab_model, textvariable=model_var, width=60).grid(row=1, column=1, sticky=tk.EW, padx=4)
    ttk.Label(tab_model, text="Summary model:").grid(row=2, column=0, sticky=tk.W, padx=4, pady=2)
    summary_entry = ttk.Entry(tab_model, textvariable=summary_var, width=60)
    summary_entry.grid(row=2, column=1, sticky=tk.EW, padx=4)
    attach_help(summary_entry,
                "Chat model that writes the 1-sentence log summary. "
                "Tip: use a free or small model (e.g. openrouter/free) so summaries cost nothing. "
                "Free routers vary per call; on any failure the log falls back to local truncation. "
                "Required: generation will not start with this field empty.")
    ttk.Label(tab_model, text="API key:").grid(row=3, column=0, sticky=tk.W, padx=4, pady=2)
    key_row = ttk.Frame(tab_model)
    key_row.grid(row=3, column=1, sticky=tk.EW, padx=4)
    ttk.Entry(key_row, textvariable=key_var, width=44, show="*").pack(side=tk.LEFT, fill=tk.X, expand=True)
    remember_check = ttk.Checkbutton(key_row, text="remember me", variable=remember_var)
    remember_check.pack(side=tk.LEFT, padx=8)
    if not HAS_FERNET:
        remember_check.configure(state=tk.DISABLED)
        remember_check.configure(text="remember me (needs: pip install cryptography)")
    forget_btn = ttk.Button(key_row, text="Forget",
                            command=lambda: on_forget_key())
    forget_btn.pack(side=tk.LEFT)
    tab_model.columnconfigure(1, weight=1)

    # ---- Dir tab: output / context / memory dirs ----
    # Each dir has a "Dynamic" checkbox below it (enabled only when n > 1);
    # when checked Start (default 1) and Range entries appear and the
    # generations use <dir>/<start>..<dir>/<range>, cycling back to start.
    dir_entries: dict = {}
    dynamic_widgets: dict = {}
    drow = 0

    def digits_entry(parent: ttk.Frame, variable: tk.StringVar) -> ttk.Entry:
        return ttk.Entry(parent, textvariable=variable, width=6, validate="key",
                         validatecommand=(root.register(
                             lambda v: v == "" or v.isdigit()), "%P"))

    for label, var, key in (("Output dir:", out_var, "output_dir"),
                            ("Context dir:", ctx_var, "context_dir"),
                            ("Memory dir:", mem_var, "memory_dir")):
        ttk.Label(tab_dir, text=label).grid(row=drow, column=0, sticky=tk.W, padx=4, pady=2)
        entry = ttk.Entry(tab_dir, textvariable=var, width=60)
        entry.grid(row=drow, column=1, sticky=tk.EW, padx=4)
        dir_entries[label] = entry
        ttk.Button(tab_dir, text="Browse", command=lambda v=var: pick_dir(v)).grid(
            row=drow, column=2, padx=4
        )
        dyn_row = ttk.Frame(tab_dir)
        dyn_row.grid(row=drow + 1, column=1, sticky=tk.W, padx=4, pady=(0, 6))
        dyn_var = tk.BooleanVar(value=False)
        start_var = tk.StringVar(value="1")
        range_var = tk.StringVar(value="")
        batch_var = tk.StringVar(value="1")
        dyn_check = ttk.Checkbutton(dyn_row, text="Dynamic", variable=dyn_var,
                                    state=tk.DISABLED)
        dyn_check.pack(side=tk.LEFT)
        start_label = ttk.Label(dyn_row, text="Start:")
        start_entry = digits_entry(dyn_row, start_var)
        range_label = ttk.Label(dyn_row, text="Range:")
        range_entry = digits_entry(dyn_row, range_var)
        batch_label = ttk.Label(dyn_row, text="Batch:")
        batch_entry = digits_entry(dyn_row, batch_var)
        dynamic_widgets[key] = {"var": dyn_var, "start": start_var, "range": range_var,
                                "batch": batch_var, "check": dyn_check,
                                "fields": ((start_label, start_entry), (range_label, range_entry),
                                           (batch_label, batch_entry)),
                                "name": label.rstrip(":")}

        def toggle_range(w: dict = dynamic_widgets[key]) -> None:
            for field_label, field_entry in w["fields"]:
                if w["var"].get():
                    field_label.pack(side=tk.LEFT, padx=(12, 4))
                    field_entry.pack(side=tk.LEFT)
                else:
                    field_label.pack_forget()
                    field_entry.pack_forget()

        dyn_var.trace_add("write", lambda *_a, f=toggle_range: f())
        folder = label.rstrip(":").lower()
        attach_help(dyn_check,
                    "Only available when n (image count) is greater than 1. "
                    f"When checked, the generations use numbered subfolders of the {folder}: "
                    f"<{folder}>/<Start>, <Start+1>, ... up to <Range>, then cycle back to Start; "
                    "Batch generations share each folder "
                    "(e.g. Start 2, Range 5, Batch 1: folders 2, 3, 4, 5, 2, ...; "
                    "Batch 3: 2, 2, 2, 3, 3, 3, 4, ...).")
        attach_help(start_entry,
                    "First subfolder number to use (natural number, default 1). "
                    "Use it to resume a batch without redoing the first folders.")
        attach_help(range_entry,
                    "Last subfolder number to use before cycling back to Start. "
                    "Required when Dynamic is checked (natural number > Start).")
        attach_help(batch_entry,
                    "How many consecutive generations go to the same subfolder before "
                    "moving to the next one (natural number, default 1). E.g. Batch 3, "
                    "Start 2, Range 12: generations 1-3 -> 2, 4-6 -> 3, 7-9 -> 4, ...")
        drow += 2
    tab_dir.columnconfigure(1, weight=1)

    def refresh_dynamic_state() -> None:
        """Dynamic checkboxes are enabled only while count > 1."""
        try:
            enabled = parse_count(count_var.get()) > 1
        except ValueError:
            enabled = False
        for w in dynamic_widgets.values():
            if enabled:
                w["check"].configure(state=tk.NORMAL)
            else:
                w["var"].set(False)
                w["check"].configure(state=tk.DISABLED)

    count_var.trace_add("write", lambda *_a: refresh_dynamic_state())
    # A new Output dir means the last batch's Dynamic logs no longer belong
    # in the Summary list.
    out_var.trace_add("write", lambda *_a: None if state["running"]
                      else state.update(batch_logs=[]))

    # ---- Analyse tab: folders side by side, one pick per row, Choose ----
    # Row N = the N-th newest (or oldest) image of every folder; clicking an
    # image selects it (only one per row); Choose copies the picks + a report.
    analyse: dict = {"folders": [str(Path(d).expanduser()) for d in merged.get("analyse", [])
                                 if Path(d).expanduser().is_dir()],
                     "newest_first": True, "rows": [], "picks": {}, "cells": {},
                     "thumbs": {}, "generation": 0, "size": THUMB_SIZE}
    chosen_var = tk.StringVar(value=str(merged.get("chosen_dir", "") or ""))
    an_top = ttk.Frame(tab_analyse)
    an_top.pack(fill=tk.X)
    add_btn = ttk.Button(an_top, text="Add folder…", command=lambda: analyse_add_folder())
    add_btn.pack(side=tk.LEFT)
    attach_help(add_btn, "Add a folder of images to compare (one column per folder; "
                         "at least 1, no limit).")
    order_btn = ttk.Button(an_top, text="Order: newest first ⇅",
                           command=lambda: analyse_invert())
    order_btn.pack(side=tk.LEFT, padx=6)
    attach_help(order_btn, "Invert the order: rows aligned from the newest image of each "
                           "folder, or from the oldest.")
    ttk.Button(an_top, text="Clear selection", command=lambda: analyse_clear()).pack(side=tk.LEFT)
    ttk.Button(an_top, text="Refresh", command=lambda: analyse_render()).pack(side=tk.LEFT, padx=6)
    zoom_in_btn = ttk.Button(an_top, text="Zoom +", width=7, command=lambda: analyse_zoom(1))
    zoom_in_btn.pack(side=tk.RIGHT)
    zoom_var = tk.StringVar(value=f"{THUMB_SIZE} px")
    ttk.Label(an_top, textvariable=zoom_var, width=7, anchor=tk.CENTER).pack(side=tk.RIGHT)
    zoom_out_btn = ttk.Button(an_top, text="Zoom −", width=7, command=lambda: analyse_zoom(-1))
    zoom_out_btn.pack(side=tk.RIGHT)
    attach_help(zoom_in_btn, "Bigger previews (up to 480 px). Selections are kept. "
                             "Double-click an image to open it full size.")
    attach_help(zoom_out_btn, "Smaller previews (down to 100 px), to see more rows at once.")
    an_chips = ttk.Frame(tab_analyse)
    an_chips.pack(fill=tk.X, pady=(6, 4))

    an_grid_frame = ttk.Frame(tab_analyse)
    an_grid_frame.pack(fill=tk.BOTH, expand=True)
    an_canvas = tk.Canvas(an_grid_frame, highlightthickness=0, background="#f4f4f4")
    an_vscroll = ttk.Scrollbar(an_grid_frame, orient=tk.VERTICAL, command=an_canvas.yview)
    an_hscroll = ttk.Scrollbar(an_grid_frame, orient=tk.HORIZONTAL, command=an_canvas.xview)
    an_canvas.configure(yscrollcommand=an_vscroll.set, xscrollcommand=an_hscroll.set)
    an_canvas.grid(row=0, column=0, sticky=tk.NSEW)
    an_vscroll.grid(row=0, column=1, sticky=tk.NS)
    an_hscroll.grid(row=1, column=0, sticky=tk.EW)
    an_grid_frame.rowconfigure(0, weight=1)
    an_grid_frame.columnconfigure(0, weight=1)
    an_inner = tk.Frame(an_canvas, background="#f4f4f4")
    an_canvas.create_window((0, 0), window=an_inner, anchor=tk.NW)
    an_inner.bind("<Configure>", lambda _e: an_canvas.configure(scrollregion=an_canvas.bbox("all")))

    def an_wheel(event: object) -> None:
        num = getattr(event, "num", 0)
        delta = getattr(event, "delta", 0)
        step = -1 if (num == 4 or delta > 0) else 1
        if getattr(event, "state", 0) & 0x1:  # Shift + wheel -> horizontal
            an_canvas.xview_scroll(step, "units")
        else:
            an_canvas.yview_scroll(step, "units")

    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        an_canvas.bind(seq, an_wheel)

    an_info_var = tk.StringVar(value="Add at least one folder to start.")
    an_info = ttk.Label(tab_analyse, textvariable=an_info_var, justify=tk.LEFT)
    an_info.pack(fill=tk.X, pady=(6, 2))
    # wrap only when the text really does not fit the current window width
    an_info.bind("<Configure>", lambda e: an_info.configure(wraplength=max(200, e.width - 8)))
    an_bottom = ttk.Frame(tab_analyse)
    an_bottom.pack(fill=tk.X)
    ttk.Label(an_bottom, text="Chosen dir:").pack(side=tk.LEFT)
    chosen_entry = ttk.Entry(an_bottom, textvariable=chosen_var, width=50)
    chosen_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
    attach_help(chosen_entry, f"Where Choose puts its copies: a new <chosen dir>/<date_time>/ "
                              f"folder with the images + {REPORT_MD} and {REPORT_CSV}. Empty = "
                              f"<parent of Output dir>/{CHOSEN_DIRNAME}.")
    ttk.Button(an_bottom, text="Browse", command=lambda: pick_dir(chosen_var)).pack(side=tk.LEFT)
    choose_btn = ttk.Button(an_bottom, text="Choose", style="Generate.TButton",
                            command=lambda: analyse_choose())
    choose_btn.pack(side=tk.LEFT, padx=(8, 0))
    attach_help(choose_btn, "Copy the selected images (one per row) and write a report with "
                            "where each one came from, its prompt/model/seed and what it was "
                            "chosen over. Originals are never moved or changed.")

    COLOR_PICKED, COLOR_IDLE = "#16a34a", "#d4d4d4"

    def analyse_add_folder() -> None:
        start = analyse["folders"][-1] if analyse["folders"] else out_var.get().strip()
        start_dir = nearest_existing_dir(start)
        chosen = filedialog.askdirectory(**({"initialdir": str(start_dir)} if start_dir else {}))
        if not chosen:
            return
        if chosen in analyse["folders"]:
            messagebox.showinfo("Analyse", "This folder is already in the table.")
            return
        analyse["folders"].append(chosen)
        analyse_render()

    def analyse_remove_folder(index: int) -> None:
        removed = Path(analyse["folders"][index]).expanduser()
        keep = {path for path in analyse_pick_paths() if Path(path).parent != removed}
        analyse["folders"].pop(index)
        analyse_render(keep=keep)

    def analyse_pick_paths() -> set[str]:
        return {str(analyse["rows"][r][c]) for r, c in analyse["picks"].items()
                if r < len(analyse["rows"]) and analyse["rows"][r][c] is not None}

    def analyse_render(keep: set[str] | None = None) -> None:
        """Rebuild the chips and the grid; keep selections by image path."""
        keep = analyse_pick_paths() if keep is None else keep
        preview_hide()  # the cells are rebuilt
        for child in an_chips.winfo_children():
            child.destroy()
        for index, folder in enumerate(analyse["folders"]):
            chip = ttk.Frame(an_chips, relief=tk.GROOVE, padding=(6, 2))
            chip.pack(side=tk.LEFT, padx=(0, 6))
            ttk.Label(chip, text=f"{index + 1}. {Path(folder).name}").pack(side=tk.LEFT)
            ttk.Button(chip, text="✕", width=2,
                       command=lambda i=index: analyse_remove_folder(i)).pack(side=tk.LEFT,
                                                                             padx=(4, 0))
            attach_help(chip, folder)
        for child in an_inner.winfo_children():
            child.destroy()
        analyse["cells"].clear()
        analyse["generation"] += 1
        try:
            rows = build_analysis_rows(analyse["folders"], analyse["newest_first"])
        except FileNotFoundError as exc:
            messagebox.showerror("Analyse", str(exc))
            rows = []
        analyse["rows"] = rows
        picks: dict[int, int] = {}
        dropped = 0
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                if cell is not None and str(cell) in keep:
                    if r in picks:
                        dropped += 1
                    else:
                        picks[r] = c
        analyse["picks"] = picks
        order_btn.configure(text="Order: newest first ⇅" if analyse["newest_first"]
                            else "Order: oldest first ⇅")
        if not analyse["folders"]:
            an_info_var.set("Add at least one folder to start.")
            return
        bg = "#f4f4f4"
        tk.Label(an_inner, text="row", background=bg, font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=0, padx=4, pady=4)
        for c, folder in enumerate(analyse["folders"]):
            count = sum(1 for row in rows if row[c] is not None)
            tk.Label(an_inner, text=f"{c + 1}. {Path(folder).name}\n{count} image(s)",
                     background=bg, font=("TkDefaultFont", 9, "bold"), width=20).grid(
                row=0, column=c + 1, padx=4, pady=4)
        pending: list[tuple[int, int, Path]] = []
        for r, row in enumerate(rows):
            tk.Label(an_inner, text=str(r + 1), background=bg).grid(row=r + 1, column=0, padx=4)
            for c, cell in enumerate(row):
                if cell is None:
                    tk.Label(an_inner, text="—", background=bg, width=20, height=8,
                             foreground="#999").grid(row=r + 1, column=c + 1, padx=4, pady=4)
                    continue
                label = tk.Label(an_inner, text="loading…", width=20, height=8,
                                 background="white", relief=tk.FLAT, cursor="hand2",
                                 highlightthickness=4, highlightbackground=COLOR_IDLE)
                label.grid(row=r + 1, column=c + 1, padx=4, pady=4)
                label.bind("<Button-1>", lambda _e, rr=r, cc=c: analyse_toggle(rr, cc))
                label.bind("<Double-1>", lambda _e, path=cell: open_path(str(path)))
                for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    label.bind(seq, an_wheel)
                stat = cell.stat()
                bind_preview(label, cell,
                             f"{cell.name}  ·  {Path(cell).parent}\nmodified "
                             f"{dt.datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M:%S}"
                             f" · {stat.st_size / 1024:.0f} KB  ·  click = select, "
                             "double-click = open full size")
                analyse["cells"][(r, c)] = label
                pending.append((r, c, cell))
        analyse_paint()
        if dropped:
            an_info_var.set(an_info_var.get() + f"  |  {dropped} selection(s) dropped: the new "
                                                "order put two picks in the same row")
        threading.Thread(target=analyse_load_thumbs, args=(analyse["generation"], pending),
                         daemon=True).start()

    def analyse_load_thumbs(generation: int, pending: list[tuple[int, int, Path]]) -> None:
        for r, c, path in pending:
            if generation != analyse["generation"]:
                return
            thumb = make_thumbnail(path, analyse["size"])
            root.after(0, lambda rr=r, cc=c, p=path, t=thumb: analyse_set_thumb(generation, rr,
                                                                                  cc, p, t))

    def analyse_set_thumb(generation: int, r: int, c: int, path: Path, thumb: Path | None) -> None:
        if generation != analyse["generation"] or (r, c) not in analyse["cells"]:
            return
        size = analyse["size"]
        cache_key = f"{thumb or path}@{size}"
        image = analyse["thumbs"].get(cache_key)
        if image is None:
            try:
                if thumb is not None:
                    image = tk.PhotoImage(file=str(thumb))
                elif path.suffix.lower() in (".png", ".gif"):
                    full = tk.PhotoImage(file=str(path))
                    factor = max(1, math.ceil(max(full.width(), full.height()) / size))
                    image = full.subsample(factor, factor)
            except tk.TclError:
                image = None
            if image is not None:
                analyse["thumbs"][cache_key] = image
        label = analyse["cells"][(r, c)]
        if image is None:
            label.configure(text=f"{path.name}\n(no preview:\ninstall ImageMagick)")
        else:
            label.configure(image=image, text="", width=size, height=size)

    def analyse_toggle(r: int, c: int) -> None:
        if analyse["picks"].get(r) == c:
            del analyse["picks"][r]
        else:
            analyse["picks"][r] = c  # only one per row: replaces the previous pick
        analyse_paint()

    def analyse_paint() -> None:
        for (r, c), label in analyse["cells"].items():
            picked = analyse["picks"].get(r) == c
            label.configure(highlightbackground=COLOR_PICKED if picked else COLOR_IDLE,
                            background="#dcfce7" if picked else "white")
        rows, folders, picks = analyse["rows"], analyse["folders"], analyse["picks"]
        parts = [f"rows: {len(rows)}", f"selected: {len(picks)}",
                 f"rows without a pick: {len(rows) - len(picks)}"]
        for c, folder in enumerate(folders):
            total = sum(1 for row in rows if row[c] is not None)
            chosen = sum(1 for col in picks.values() if col == c)
            parts.append(f"{c + 1}. {Path(folder).name}: {total} image(s), {chosen} selected")
        an_info_var.set("  |  ".join(parts))
        choose_btn.configure(state=tk.NORMAL if picks else tk.DISABLED)

    # Hover preview: as big as the free side of the screen allows (preview_geometry)
    # in a floating window next to the pointer, kept inside the screen.
    preview: dict = {"win": None, "after": None, "path": None, "images": {}}

    def preview_hide(_event: object = None) -> None:
        if preview["after"] is not None:
            root.after_cancel(preview["after"])
            preview["after"] = None
        if preview["win"] is not None:
            try:
                preview["win"].destroy()
            except tk.TclError:
                pass
            preview["win"] = None
        preview["path"] = None

    def preview_show(path: Path, caption: str, x: int, y: int) -> None:
        """Build the preview window hidden and map it only when the image is
        ready and the window already has its final place (no flash at the
        screen corner, no empty dark box)."""
        preview["after"] = None
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        _bytes, img_w, img_h = inspect_image(path)
        if not img_w or not img_h:
            img_w, img_h = 1600, 1000
        size, side = preview_geometry(img_w, img_h, x, screen_w, screen_h)
        win = tk.Toplevel(root)
        win.withdraw()  # stays invisible until placed
        win.wm_overrideredirect(True)
        win.wm_attributes("-topmost", True)
        preview.update(win=win, path=path)

        def reveal(image: object) -> None:
            if preview["win"] is not win or preview["path"] != path:
                return  # pointer already left this image
            frame = tk.Frame(win, background="#222", padx=4, pady=4)
            frame.pack()
            if image is None:
                tk.Label(frame, text="no preview (install ImageMagick)", foreground="#ddd",
                         background="#222", padx=20, pady=20).pack()
                width = 320
            else:
                tk.Label(frame, image=image, background="#222", borderwidth=0).pack()
                width = image.width()  # type: ignore[attr-defined]
            tk.Label(frame, text=caption, foreground="#eee", background="#222",
                     justify=tk.LEFT, wraplength=max(320, width)).pack(anchor=tk.W,
                                                                      pady=(4, 0))
            win.update_idletasks()  # sizes are computed while still withdrawn
            win_w, win_h = win.winfo_reqwidth(), win.winfo_reqheight()
            left = x + PREVIEW_GAP if side == "right" else x - PREVIEW_GAP - win_w
            left = min(max(0, left), max(0, screen_w - win_w))
            top = min(max(0, y - win_h // 3), max(0, screen_h - win_h))
            win.wm_geometry(f"{win_w}x{win_h}+{left}+{top}")
            win.deiconify()

        key = f"{path}@{size}"
        if key in preview["images"]:
            reveal(preview["images"][key])
            return

        def work() -> None:
            thumb = make_thumbnail(path, size)
            root.after(0, lambda: load(thumb))

        def load(thumb: Path | None) -> None:
            image = None
            try:
                if thumb is not None:
                    image = tk.PhotoImage(file=str(thumb))
                elif path.suffix.lower() in (".png", ".gif"):
                    full = tk.PhotoImage(file=str(path))
                    factor = max(1, math.ceil(max(full.width(), full.height()) / size))
                    image = full.subsample(factor, factor)
            except tk.TclError:
                image = None
            if image is not None:
                if len(preview["images"]) >= 12:  # big images: keep memory bounded
                    preview["images"].pop(next(iter(preview["images"])))
                preview["images"][key] = image
            reveal(image)

        threading.Thread(target=work, daemon=True).start()

    def bind_preview(widget: tk.Widget, path: Path, caption: str) -> None:
        def enter(event: object) -> None:
            preview_hide()
            x, y = event.x_root, event.y_root  # type: ignore[attr-defined]
            preview["after"] = root.after(350, lambda: preview_show(path, caption, x, y))

        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", preview_hide)
        for seq in ("<Button-1>", "<Double-1>", "<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(seq, preview_hide, add="+")

    def analyse_zoom(step: int) -> None:
        levels = THUMB_ZOOM_LEVELS
        current = min(range(len(levels)), key=lambda i: abs(levels[i] - analyse["size"]))
        target = max(0, min(len(levels) - 1, current + step))
        zoom_out_btn.configure(state=tk.NORMAL if target > 0 else tk.DISABLED)
        zoom_in_btn.configure(state=tk.NORMAL if target < len(levels) - 1 else tk.DISABLED)
        if levels[target] == analyse["size"]:
            return
        analyse["size"] = levels[target]
        zoom_var.set(f"{levels[target]} px")
        analyse["thumbs"].clear()  # free the previous size from memory
        analyse_render()

    def analyse_invert() -> None:
        keep = analyse_pick_paths()
        analyse["newest_first"] = not analyse["newest_first"]
        analyse_render(keep=keep)

    def analyse_clear() -> None:
        analyse["picks"].clear()
        analyse_paint()

    def analyse_choose() -> None:
        if not analyse["picks"]:
            return
        target = chosen_var.get().strip() or default_chosen_dir(out_var.get().strip() or None)
        try:
            result = choose_images(analyse["folders"], dict(analyse["picks"]), target,
                                   analyse["newest_first"])
        except (ValueError, OSError) as exc:
            messagebox.showerror("Choose", str(exc))
            return
        persist_gui_config()
        if messagebox.askyesno("Choose", f"Copied {len(result['rows'])} image(s) to\n"
                                         f"{result['folder']}\n\nwith {REPORT_MD} and "
                                         f"{REPORT_CSV}.\n\nOpen the folder?"):
            open_path(result["folder"])

    root.after(300, analyse_render)

    # ---- Generate tab: per-generation options (not assigned to a tab
    # in the requested layout, kept here next to the prompt) ----
    opts = ttk.Frame(tab_generate)
    opts.pack(fill=tk.X, pady=4)
    ttk.Label(opts, text="Aspect:").pack(side=tk.LEFT, padx=4)
    prop_combo = ttk.Combobox(opts, textvariable=prop_var, values=ASPECT_RATIOS, width=8, state="readonly")
    prop_combo.pack(side=tk.LEFT)
    ttk.Label(opts, text="Resolution:").pack(side=tk.LEFT, padx=(12, 4))
    ttk.Combobox(opts, textvariable=res_var, values=RESOLUTIONS, width=6, state="readonly").pack(side=tk.LEFT)
    ttk.Label(opts, text="Format:").pack(side=tk.LEFT, padx=(12, 4))
    ttk.Combobox(opts, textvariable=fmt_var, values=OUTPUT_FORMATS, width=6, state="readonly").pack(side=tk.LEFT)
    ttk.Label(opts, text="Seed:").pack(side=tk.LEFT, padx=(12, 4))
    ttk.Entry(opts, textvariable=seed_var, width=8).pack(side=tk.LEFT)
    dry_check = ttk.Checkbutton(opts, text="dry-run", variable=dry_var)
    dry_check.pack(side=tk.LEFT, padx=12)

    tooltip: dict = {"window": None, "canvas": None, "label": None}

    def draw_ratio_on_canvas(canvas: tk.Canvas, label_widget: ttk.Label, prop: str) -> None:
        canvas.delete("all")
        max_w, max_h, pad = 240, 140, 10
        try:
            left, right = prop.replace(" ", "").split(":")
            wr, hr = float(left), float(right)
        except (ValueError, ZeroDivisionError, AttributeError):
            canvas.create_text(max_w // 2, max_h // 2, text="auto")
            try:
                label_widget.config(text=prop)
            except Exception:
                pass
            return
        if wr <= 0 or hr <= 0:
            return
        scale = min((max_w - 2 * pad) / wr, (max_h - 2 * pad) / hr)
        w, h = wr * scale, hr * scale
        x0, y0 = (max_w - w) / 2, (max_h - h) / 2
        canvas.create_rectangle(x0, y0, x0 + w, y0 + h, outline="red", width=3)
        try:
            label_widget.config(text=prop)
        except Exception:
            pass

    def show_ratio_tooltip(_event: object = None) -> None:
        hide_ratio_tooltip()
        tip = tk.Toplevel(root)
        tip.wm_overrideredirect(True)
        tip.wm_attributes("-topmost", True)
        x = prop_combo.winfo_rootx() + prop_combo.winfo_width() + 8
        y = prop_combo.winfo_rooty()
        tip.wm_geometry(f"+{x}+{y}")
        ttk.Label(tip, text="Ratio preview:").pack(padx=6, pady=(6, 0), anchor=tk.W)
        canvas = tk.Canvas(tip, width=240, height=140, bg="white",
                           highlightthickness=1, highlightbackground="gray")
        canvas.pack(padx=6, pady=4)
        label = ttk.Label(tip, text=prop_var.get())
        label.pack(padx=6, pady=(0, 6), anchor=tk.W)
        draw_ratio_on_canvas(canvas, label, prop_var.get().strip())
        tooltip["window"] = tip
        tooltip["canvas"] = canvas
        tooltip["label"] = label

    def hide_ratio_tooltip(_event: object = None) -> None:
        tip = tooltip.get("window")
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass
            tooltip["window"] = None
            tooltip["canvas"] = None
            tooltip["label"] = None

    def refresh_ratio_tooltip(*_args: object) -> None:
        if tooltip.get("window") is not None:
            draw_ratio_on_canvas(
                tooltip["canvas"], tooltip["label"], prop_var.get().strip()
            )

    prop_combo.bind("<Enter>", show_ratio_tooltip)
    prop_combo.bind("<Leave>", hide_ratio_tooltip)
    prop_combo.bind("<<ComboboxSelected>>", refresh_ratio_tooltip)

    # ---- prompt (Generate tab) ----
    prompt_frame = ttk.LabelFrame(tab_generate, text="Prompt", padding=8)
    prompt_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
    prompt_text = tk.Text(prompt_frame, height=8, wrap=tk.WORD)
    prompt_text.pack(fill=tk.BOTH, expand=True)
    if merged.get("prompt"):  # last prompt (config.json) or --gui --prompt
        prompt_text.insert("1.0", str(merged["prompt"]))

    # ---- Injection tab: per-generation values for {{variables}} ----
    def current_template_vars() -> list[str]:
        return extract_template_vars(prompt_text.get("1.0", tk.END))

    injection_names: list[str] = []

    def refresh_injection_tab(*_args: object) -> None:
        try:
            count = parse_count(count_var.get())
        except ValueError:
            count = 1
        names = current_template_vars()
        active = count > 1 and bool(names)
        # Preserve typed values across rebuilds (keyed by variable name).
        old_values = [[entry.get() for entry in row] for row in injection_entries]
        old_names = list(injection_names)
        for child in tab_injection.winfo_children():
            child.destroy()
        injection_entries.clear()
        injection_names[:] = names if active else []
        if not active:
            try:
                notebook.tab(tab_injection, text="Injection")
            except tk.TclError:
                pass
            notebook.hide(tab_injection)
            return
        ttk.Label(
            tab_injection,
            text="Fill one value per generation. An empty cell repeats the value "
            "from the row above; the first row falls back to the variable name.",
            wraplength=620, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))
        grid = ttk.Frame(tab_injection)
        grid.pack(fill=tk.BOTH, expand=True)
        for j, name in enumerate(names):
            ttk.Label(grid, text=name, font=("", 10, "bold")).grid(
                row=0, column=j, padx=4, pady=2, sticky=tk.EW)
        for i in range(count):
            row_entries: list[ttk.Entry] = []
            for j, name in enumerate(names):
                entry = ttk.Entry(grid, width=24)
                if name in old_names and i < len(old_values):
                    entry.insert(0, old_values[i][old_names.index(name)])
                entry.grid(row=i + 1, column=j, padx=4, pady=2, sticky=tk.EW)
                row_entries.append(entry)
            injection_entries.append(row_entries)
        for j in range(len(names)):
            grid.columnconfigure(j, weight=1)
        try:
            notebook.tab(tab_injection, foreground="#e6c600")
        except tk.TclError:
            notebook.tab(tab_injection, text="\U0001f7e1 Injection")
        notebook.add(tab_injection)

    prompt_text.bind("<KeyRelease>", refresh_injection_tab)
    count_var.trace_add("write", lambda *_a: refresh_injection_tab())

    # ---- collapsible log list (spoiler, hidden by default) ----
    log_frame = ttk.LabelFrame(tab_generate, text="Summary", padding=8)
    columns = tuple(LOG_FIELDS)
    list_container = ttk.Frame(log_frame)
    list_container.pack(fill=tk.BOTH, expand=True)
    tree = ttk.Treeview(list_container, columns=columns, show="headings", height=4)
    wide_columns = {"prompt_summary": 320, "prompt_full": 400, "image_file": 200,
                    "model": 220, "date": 160, "generation_timestamp": 160}
    for col in columns:
        tree.heading(col, text=col.replace("_", " "))
        tree.column(col, width=wide_columns.get(col, 90), stretch=False,
                    anchor=tk.W if col in ("prompt_summary", "prompt_full") else tk.CENTER)
    vscroll = ttk.Scrollbar(list_container, orient=tk.VERTICAL, command=tree.yview)
    hscroll = ttk.Scrollbar(list_container, orient=tk.HORIZONTAL, command=tree.xview)
    tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vscroll.grid(row=0, column=1, sticky="ns")
    hscroll.grid(row=1, column=0, sticky="ew")
    list_container.grid_rowconfigure(0, weight=1)
    list_container.grid_columnconfigure(0, weight=1)
    total_var = tk.StringVar(value="total: 0 ops / $0.000000")
    log_bottom = ttk.Frame(log_frame)
    log_bottom.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
    ttk.Label(log_bottom, textvariable=total_var).pack(side=tk.LEFT, anchor=tk.W)
    ttk.Button(log_bottom, text="Refresh log",
               command=lambda: refresh_log()).pack(side=tk.RIGHT)

    # ---- actions + clock (Generate tab) ----
    bar = ttk.Frame(tab_generate, padding=(4, 4))
    bar.pack(fill=tk.X)
    gen_btn = ttk.Button(bar, text="Generate", style="Generate.TButton")
    gen_btn.pack(side=tk.LEFT)
    ttk.Label(bar, text="×").pack(side=tk.LEFT, padx=(4, 0))
    count_entry = ttk.Entry(bar, textvariable=count_var, width=4,
                            validate="key",
                            validatecommand=(root.register(
                                lambda v: v == "" or v.isdigit()), "%P"))
    count_entry.pack(side=tk.LEFT)
    cancel_btn = ttk.Button(bar, text="Cancel", state=tk.DISABLED, style="Cancel.TButton")
    cancel_btn.pack(side=tk.LEFT, padx=4)
    clock_var = tk.StringVar(value="elapsed: 0.0s")
    clock_label = ttk.Label(bar, textvariable=clock_var, font=("TkDefaultFont", 11, "bold"))
    clock_label.pack(side=tk.LEFT, padx=16)
    spin = ttk.Progressbar(bar, mode="indeterminate", length=120)
    spin.pack(side=tk.LEFT, padx=4)
    spin.pack_forget()  # only visible while generating
    status_var = tk.StringVar(value="idle")
    status_label = ttk.Label(bar, textvariable=status_var)
    status_label.pack(side=tk.LEFT, padx=8)
    spoiler_state = {"visible": False}
    spoiler_btn = ttk.Button(bar, text="Show log \u25bc")
    spoiler_btn.pack(side=tk.RIGHT)
    muted_var = tk.BooleanVar(value=False)
    def toggle_mute() -> None:
        muted_var.set(not muted_var.get())
        if muted_var.get():
            mute_btn.configure(text="\U0001F515", style="MuteOn.TButton")
        else:
            mute_btn.configure(text="\U0001F514", style="MuteOff.TButton")
    mute_btn = ttk.Button(bar, text="\U0001F514", style="MuteOff.TButton", command=toggle_mute)
    mute_btn.pack(side=tk.RIGHT, padx=4)

    def toggle_log(force: bool | None = None) -> None:
        show = force if force is not None else not spoiler_state["visible"]
        spoiler_state["visible"] = show
        if show:
            log_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4, before=bar)
            refresh_log()
            spoiler_btn.configure(text="Hide log \u25b2")
        else:
            log_frame.pack_forget()
            spoiler_btn.configure(text="Show log \u25bc")

    spoiler_btn.configure(command=toggle_log)

    # ---- help tooltips for new users (hover to read) ----
    attach_help(dir_entries["Output dir:"],
                "Folder where generated images and log_image_generate.csv are saved. "
                f"Default: {default_output_dir()}.")
    attach_help(dir_entries["Context dir:"],
                "Folder with .md/.txt files automatically added to the prompt as context.")
    attach_help(dir_entries["Memory dir:"],
                "Folder with reference images sent along with the prompt to guide generation.")
    attach_help(dry_check,
                "Test run without spending anything: writes a local placeholder image "
                "instead of calling the paid API.")
    attach_help(count_entry,
                f"How many images to generate with the same prompt (natural number {MIN_COUNT}-{MAX_COUNT}). "
                "Above 1 asks for confirmation: each image may add costs.")
    attach_help(clock_label,
                "Time from sending the request until the image arrives.")
    attach_help(status_label,
                "Current state: idle (waiting), generating, done, cancelled or error.")
    attach_help(remember_check,
                "Encrypt and save this key so you don't type it again. Stored in "
                f"{VAULT_HELP}/<provider>_api_key.enc (secret in <provider>.fkey).")
    attach_help(forget_btn,
                "Delete the saved key and its local secret (<provider>_api_key.enc and "
                f"<provider>.fkey) from {VAULT_HELP}/.")

    def tick_clock() -> None:
        if state["running"]:
            state["elapsed"] = time.perf_counter() - state["start"]
            clock_var.set(f"elapsed: {state['elapsed']:.1f}s")
            state["after_id"] = root.after(100, tick_clock)

    log_rows_cache: list[dict] = []

    def refresh_log() -> None:
        """Fill the Summary list, newest request on top. Besides the Output
        dir log it merges the logs written by the last batch (Dynamic output
        subfolders), so live progress shows every new image."""
        for child in tree.get_children():
            tree.delete(child)
        log_path = Path(out_var.get() or default_output_dir()) / LOG_FILENAME
        extra = [Path(p) for p in state.get("batch_logs", [])]
        rows = collect_log_rows([log_path, *extra])
        total = 0.0
        for row in rows:
            try:
                total += float(row.get("cost_usd") or 0)
            except ValueError:
                pass
        log_rows_cache.clear()
        for row in rows[:500]:
            values = []
            for col in columns:
                raw = row.get(col, "") or ""
                if col == "date":
                    raw = raw[:19]
                elif col == "prompt_summary":
                    raw = raw[:100]
                elif col == "prompt_full":
                    raw = raw[:300]
                elif col == "model":
                    raw = raw[:60]
                values.append(raw)
            tree.insert("", tk.END, values=tuple(values))
            log_rows_cache.append(row)
        extra_logs = {q.resolve() for q in extra} - {log_path.expanduser().resolve()}
        where = f"{log_path}" + (f" + {len(extra_logs)} Dynamic log(s)" if extra_logs else "")
        total_var.set(f"total: {len(rows)} ops / ${total:.6f}  ({where})")

    def use_prompt_from_list() -> None:
        selection = tree.selection()
        if not selection:
            return
        try:
            index = tree.index(selection[0])
            row = log_rows_cache[index]
        except (tk.TclError, IndexError):
            return
        full = (row.get("prompt_full") or row.get("prompt_summary") or "").strip()
        if not full:
            return
        prompt_text.delete("1.0", tk.END)
        prompt_text.insert("1.0", full)
        refresh_injection_tab()
        notebook.select(tab_generate)
        status_var.set("prompt loaded from log")

    list_menu = tk.Menu(root, tearoff=0)
    list_menu.add_command(label="Use prompt", command=use_prompt_from_list)

    def show_list_menu(event: object) -> None:
        item = tree.identify_row(event.y)  # type: ignore[attr-defined]
        if item:
            tree.selection_set(item)
            # tk_popup (not post): grabs the pointer so a click outside or Esc
            # closes the menu. No grab_release(): on X11 tk_popup returns at once
            # and releasing the grab would leave the menu stuck on screen.
            list_menu.tk_popup(event.x_root, event.y_root)  # type: ignore[attr-defined]

    tree.bind("<Button-3>", show_list_menu)
    tree.bind("<Button-2>", show_list_menu)  # right-click on macOS

    def on_forget_key() -> None:
        try:
            current = normalize_provider(provider_var.get())
        except ValueError as exc:
            messagebox.showerror("Forget key", str(exc))
            return
        if forget_remembered_key(current):
            remember_var.set(False)
            status_var.set(f"forgot remembered key for {current}")
        else:
            status_var.set(f"no remembered key for {current}")

    def open_path(path: str) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess

                subprocess.Popen(["open", path])
            else:
                import subprocess

                subprocess.Popen(["xdg-open", path])
        except Exception as exc:  # noqa: BLE001 - report to user
            messagebox.showerror("Open failed", str(exc))

    def show_done_dialog(images: list[str]) -> None:
        dialog = tk.Toplevel(root)
        dialog.title("Done")
        dialog.transient(root)
        dialog.grab_set()
        ttk.Label(dialog, text="Imagem gerada com sucesso!",
                  font=("TkDefaultFont", 11, "bold")).pack(padx=16, pady=(14, 6))
        box = tk.Listbox(dialog, width=80, height=min(6, max(2, len(images))))
        for image in images:
            box.insert(tk.END, image)
        box.pack(padx=16, pady=4, fill=tk.BOTH, expand=True)
        buttons = ttk.Frame(dialog, padding=8)
        buttons.pack(fill=tk.X)

        def on_open() -> None:
            targets = [box.get(i) for i in box.curselection()] or images
            for target in targets:
                open_path(str(target))

        ttk.Button(buttons, text="Abrir", command=on_open).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="OK", command=dialog.destroy).pack(side=tk.RIGHT, padx=4)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        root.wait_window(dialog)

    def on_done(result: dict | None, error: str | None, cancelled: bool = False) -> None:
        state["running"] = False
        if state["after_id"]:
            try:
                root.after_cancel(state["after_id"])
            except tk.TclError:
                pass
            state["after_id"] = None
        gen_btn.configure(state=tk.NORMAL)
        cancel_btn.configure(state=tk.DISABLED)
        spin.stop()
        spin.pack_forget()
        if cancelled:
            clock_var.set(f"elapsed: {state['elapsed']:.1f}s (cancelled)")
            status_var.set("cancelled: partial files removed, nothing logged")
            return
        if error:
            status_var.set(f"error: {error}")
            if not muted_var.get():
                play_chime("error")
            messagebox.showerror("Generation failed", error)
        else:
            assert result is not None
            state["batch_logs"] = list(result.get("log_paths") or [result["log_path"]])
            clock_var.set(f"elapsed: {result['elapsed']:.1f}s (done)")
            status_var.set(f"saved {len(result['images'])} image(s) | ${result['cost']:.6f}")
            if not muted_var.get():
                play_chime("success")
            show_done_dialog(list(result["images"]))
        toggle_log(True)

    def worker(prompts: list[str], kwargs: dict) -> None:
        try:
            result = run_generation_batch(prompts, **kwargs)
        except GenerationCancelled:
            root.after(0, lambda: on_done(None, None, True))
        except Exception as exc:  # noqa: BLE001 - show any failure in GUI
            message = f"{type(exc).__name__}: {exc}"
            root.after(0, lambda: on_done(None, message))
        else:
            root.after(0, lambda: on_done(result, None))

    def on_progress(info: dict) -> None:
        """One generation of a batch finished (already logged): refresh Summary."""
        if not state["running"]:
            return
        state["batch_logs"] = list(info.get("log_paths") or [])
        status_var.set(f"generating... {info['done']}/{info['total']} done")
        toggle_log(True)

    def on_cancel() -> None:
        event = state.get("cancel_event")
        if state["running"] and event is not None:
            event.set()
            abort_all_http()
            status_var.set("cancelling... (aborting requests)")

    def on_generate() -> None:
        if state["running"]:
            return
        prompt = prompt_text.get("1.0", tk.END).strip()
        if not prompt:
            messagebox.showwarning("Missing prompt", "Type a prompt first.")
            return
        summary_model = summary_var.get().strip()
        if not summary_model:
            messagebox.showwarning("Missing summary model",
                                   "Fill in Summary model first (Model tab).")
            return
        seed_raw = seed_var.get().strip()
        seed = int(seed_raw) if seed_raw.lstrip("-").isdigit() else None
        try:
            count = parse_count(count_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid count", str(exc))
            return
        names = current_template_vars()
        injection_active = count > 1 and bool(names)
        prompts = [prompt]
        if injection_active:
            cells = [[entry.get() for entry in row] for row in injection_entries]
            if not any((cell or "").strip() for row in cells for cell in row):
                messagebox.showwarning(
                    "Injection table empty",
                    "The prompt has {{variables}} but the Injection table is "
                    "empty. Fill at least one cell or remove the variables.",
                )
                return
            rows = resolve_injection_rows(cells, names)
            prompts = [
                apply_template_values(prompt, dict(zip(names, row)))
                for row in rows
            ]
        dynamic_dirs: dict = {}
        for key, w in dynamic_widgets.items():
            if count > 1 and w["var"].get():
                spec = {"start": w["start"].get().strip(), "range": w["range"].get().strip(),
                        "batch": w["batch"].get().strip()}
                try:
                    parse_dynamic_spec(spec, f"{w['name']} Dynamic")
                except ValueError as exc:
                    messagebox.showerror("Invalid Dynamic start/range/batch", str(exc))
                    return
                dynamic_dirs[key] = spec
        if dynamic_dirs:
            try:
                check_dynamic_dirs(dynamic_dirs, {
                    "output_dir": out_var.get().strip() or default_output_dir(),
                    "context_dir": ctx_var.get().strip(),
                    "memory_dir": mem_var.get().strip(),
                }, count)
            except (ValueError, FileNotFoundError) as exc:
                messagebox.showerror("Invalid Dynamic dir", str(exc))
                return
        if count > 1 and not messagebox.askyesno(
            "Confirm multiple generations",
            f"Generate {count} images "
            + ("with different prompts (Injection tab)"
               if injection_active else "with the same prompt and settings")
            + "?\n\n"
            f"Each image counts as a separate generation and may incur "
            f"additional costs (total \u2248 {count}\u00d7 the single-image cost).\n\n"
            "Continue?",
        ):
            return
        try:
            current_provider = normalize_provider(provider_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid provider", str(exc))
            return
        typed_key = key_var.get().strip()
        api_key = typed_key or resolve_api_key(current_provider)[0]
        if remember_var.get() and typed_key:
            try:
                save_remembered_key(current_provider, typed_key)
                status_var.set(f"remembered key for {current_provider}")
            except RuntimeError as exc:
                messagebox.showwarning("Remember key", str(exc))
        kwargs = {
            "dynamic_dirs": dynamic_dirs,
            "on_progress": lambda info: root.after(0, lambda: on_progress(info)),
            "output_dir": out_var.get().strip() or default_output_dir(),
            "context_dir": ctx_var.get().strip() or None,
            "memory_dir": mem_var.get().strip() or None,
            "model": model_var.get().strip() or PROVIDERS[current_provider]["default_model"],
            "aspect_ratio": prop_var.get().strip() or "1:1",
            "resolution": res_var.get().strip() or "1K",
            "output_format": fmt_var.get().strip() or "png",
            "seed": seed,
            "count": count,
            "api_key": api_key,
            "dry_run": bool(dry_var.get()),
            "summary_model": summary_model,
            "provider": current_provider,
            "cancel_event": threading.Event(),
        }
        state["running"] = True
        state["batch_logs"] = []
        state["cancel_event"] = kwargs["cancel_event"]
        state["start"] = time.perf_counter()
        persist_gui_config()
        gen_btn.configure(state=tk.DISABLED)
        cancel_btn.configure(state=tk.NORMAL)
        spin.pack(side=tk.LEFT, padx=4)
        spin.start(50)
        status_var.set("generating...")
        tick_clock()
        threading.Thread(target=worker, args=(prompts, kwargs), daemon=True).start()

    def persist_gui_config() -> None:
        try:
            save_gui_config({
                "output_dir": out_var.get().strip(),
                "context_dir": ctx_var.get().strip(),
                "memory_dir": mem_var.get().strip(),
                "provider": provider_var.get().strip(),
                "model": model_var.get().strip(),
                "summary_model": summary_var.get().strip(),
                "prop": prop_var.get().strip(),
                "resolution": res_var.get().strip(),
                "output_format": fmt_var.get().strip(),
                "dry_run": bool(dry_var.get()),
                "analyse": list(analyse["folders"]),
                "chosen_dir": chosen_var.get().strip(),
                "prompt": prompt_text.get("1.0", "end-1c"),
            })
        except OSError:
            pass

    cancel_btn.configure(command=on_cancel)
    gen_btn.configure(command=on_generate)
    refresh_log()
    refresh_injection_tab()

    def on_close() -> None:
        if not state["running"]:
            persist_gui_config()
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    wants_cli_work = bool(
        args.list_log or args.forget_key or args.remember_key or args.analyse
        or resolve_prompt(args).strip() or args.dry_run and resolve_prompt(args).strip()
    )
    if args.gui or not wants_cli_work:
        # No prompt given -> open GUI (it can do everything the CLI can).
        if args.list_log:
            return main_cli(args)
        if resolve_prompt(args).strip() and not args.gui:
            return main_cli(args)
        # Only explicit CLI flags override config.json in the GUI.
        raw_argv = argv if argv is not None else sys.argv[1:]
        flag_map = {
            "--output-dir": "output_dir",
            "--context-dir": "context_dir",
            "--memory-dir": "memory_dir",
            "--model": "model",
            "--summary-model": "summary_model",
            "--provider": "provider",
            "--prop": "prop",
            "--aspect-ratio": "prop",
            "--resolution": "resolution",
            "--output-format": "output_format",
            "--analyse": "analyse",
            "--chosen-dir": "chosen_dir",
            "--prompt": "prompt",
        }
        gui_defaults = {}
        for flag, key in flag_map.items():
            if any(a == flag or a.startswith(flag + "=") for a in raw_argv):
                gui_defaults[key] = getattr(args, key)
        run_gui(gui_defaults)
        return 0
    return main_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
