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
import mimetypes
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
import urllib.parse
import zlib
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
}


def normalize_provider(name: str) -> str:
    """Lowercase provider slug restricted to safe chars (used in file names)."""
    slug = name.strip().lower()
    if not slug or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise ValueError(f"invalid provider name: {name!r}")
    if slug not in PROVIDERS:
        raise ValueError(f"unknown provider: {slug!r} (known: {sorted(PROVIDERS)})")
    return slug


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


def save_remembered_key(provider: str, api_key: str) -> Path:
    """Encrypt and store provider key. Returns the blob path."""
    _require_fernet()
    provider = normalize_provider(provider)
    fkey_path, blob_path = _vault_paths(provider)
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


def load_remembered_key(provider: str) -> str | None:
    """Decrypt and return the stored provider key, or None if absent."""
    _require_fernet()
    provider = normalize_provider(provider)
    fkey_path, blob_path = _vault_paths(provider)
    if not fkey_path.exists() or not blob_path.exists():
        return None
    assert Fernet is not None
    fernet_key = fkey_path.read_bytes().strip()
    token = blob_path.read_bytes().strip()
    try:
        return Fernet(fernet_key).decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(f"stored key for {provider} is corrupted or invalid") from exc


def forget_remembered_key(provider: str) -> bool:
    """Delete stored key material for provider. Returns True if anything removed."""
    provider = normalize_provider(provider)
    removed = False
    for path in _vault_paths(provider):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
    return removed


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
    return clean


# ---------------------------------------------------------------------------
# Context / memory helpers
# ---------------------------------------------------------------------------

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


def build_final_prompt(base_prompt: str, context_text: str, prop: str, resolution: str) -> str:
    """Append context and output-size instruction to the user prompt."""
    chunks = [base_prompt.strip()]
    if context_text:
        chunks.append(f"Context:\n{context_text}")
    if prop and prop != "auto":
        chunks.append(f"Generate the image with aspect ratio {prop}.")
    if resolution:
        chunks.append(f"Generate the image at resolution tier {resolution}.")
    return "\n\n".join(c for c in chunks if c)


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
    """
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
        r"^think",
        r"^thought\b",
        r"^analy[sz]",
        r"^step\s*\d",
        r"^process\s*:",
        r"^\d+\s*[.)]\s",
    )
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
    lowered = line.lower()
    if not line:
        raise ValueError("empty summary from model")
    if "thinking process" in lowered or any(pat.match(line) for pat in _SUMMARY_META_RES):
        raise ValueError("model returned meta commentary instead of a summary")
    one_line = " ".join(line.split())
    if len(one_line) > limit:
        cut = one_line[:limit].rsplit(" ", 1)[0] or one_line[:limit]
        one_line = cut.rstrip(".,;:") + "."
    return one_line


def summarize_prompt_remote(
    prompt: str,
    api_key: str,
    model: str,
    timeout_s: int,
    cancel_event: threading.Event | None = None,
) -> str:
    """Ask a free OpenRouter chat model for a 1-sentence summary of the prompt."""
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You summarize image prompts. Reply with EXACTLY ONE complete "
                    f"sentence of at most {MAX_SUMMARY_CHARS} characters, no line breaks, "
                    "in the same language as the image prompt. "
                    "Output ONLY that sentence and NOTHING else: no thinking process, "
                    "no reasoning, no analysis, no explanations, no preamble, "
                    "no labels, no numbering, no quotation marks. "
                    "Never cut the sentence off and never end with ellipsis."
                ),
            },
            {"role": "user",
             "content": f"{prompt}\n\nOutput only the summary sentence."},
        ],
        "max_tokens": 120,
        "temperature": 0.0,
    }
    status, raw = _post_json(CHAT_URL, body, _openrouter_headers(api_key), timeout_s, cancel_event)
    if status != 200:
        raise RuntimeError(f"OpenRouter HTTP {status}: {raw[:2000]}")
    payload = json.loads(raw)
    content = _extract_message_text(payload["choices"][0]["message"])
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


# ---------------------------------------------------------------------------
# Provider request functions (one per provider, same input/output contract)
# Each returns (payload, start_ts, elapsed_s) where payload has:
#   data: [{b64_json, media_type?}], usage: {cost?}, created
# ---------------------------------------------------------------------------

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
    """POST to OpenRouter images API. Returns (payload, start_ts, elapsed_s)."""
    body: dict = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "output_format": output_format,
        "n": count,
    }
    if references:
        body["input_references"] = references
    if seed is not None:
        body["seed"] = seed
    start = time.perf_counter()
    start_ts = time.time()
    status, raw = _post_json(API_URL, body, _openrouter_headers(api_key), timeout_s, cancel_event)
    if status != 200:
        raise RuntimeError(f"OpenRouter HTTP {status}: {raw[:2000]}")
    elapsed = time.perf_counter() - start
    _ = start_ts
    payload = json.loads(raw)
    return payload, start_ts, elapsed


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
    """Gemini image request (not implemented yet).

    Placeholder keeping the provider registry uniform: add the real call to
    the Generative Language API here and return the shared payload contract.
    """
    raise NotImplementedError(
        "gemini provider is not implemented yet: store the key with "
        "--provider gemini --remember-key, then implement request_gemini() "
        "for the Generative Language API image endpoint."
    )


REQUEST_FUNCS = {
    "openrouter": request_openrouter,
    "gemini": request_gemini,
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
    final_prompt = build_final_prompt(prompt, context_text, aspect_ratio, resolution)

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
        except Exception as exc:
            image_box["error"] = exc

    def fetch_summary() -> None:
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
    for path in paths:
        size_bytes, width, height = inspect_image(path)
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
    return parser


def resolve_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        text = Path(args.prompt_file).read_text(encoding="utf-8", errors="replace")
        if args.prompt:
            return f"{args.prompt}\n\n{text}"
        return text
    return args.prompt


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
        result = run_generation(
            prompt=prompt,
            output_dir=output_dir,
            context_dir=args.context_dir or None,
            memory_dir=args.memory_dir or None,
            model=model,
            aspect_ratio=args.prop,
            resolution=args.resolution,
            output_format=args.output_format,
            seed=args.seed,
            count=args.count,
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
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
    print(f"done in {result['elapsed']:.1f}s (cli measured {time.perf_counter() - t0:.1f}s)")
    for image in result["images"]:
        print(f"saved: {image}")
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
    root.geometry("860x720")

    # Brand logo (logo.png next to this file): window icon + header.
    # Missing/corrupt file -> plain text header, never blocks startup.
    state: dict = {"running": False, "start": 0.0, "elapsed": 0.0, "after_id": None,
                   "logo_img": None}
    try:
        _logo_path = Path(__file__).resolve().parent / "logo.png"
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

    ttk.Button(header, text="?", width=3, command=open_docs).pack(side=tk.RIGHT)

    def pick_dir(var: tk.StringVar) -> None:
        chosen = filedialog.askdirectory()
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
    drow = 0
    dir_entries: dict = {}
    for label, var in (("Output dir:", out_var), ("Context dir:", ctx_var), ("Memory dir:", mem_var)):
        ttk.Label(tab_dir, text=label).grid(row=drow, column=0, sticky=tk.W, padx=4, pady=2)
        entry = ttk.Entry(tab_dir, textvariable=var, width=60)
        entry.grid(row=drow, column=1, sticky=tk.EW, padx=4)
        dir_entries[label] = entry
        ttk.Button(tab_dir, text="Browse", command=lambda v=var: pick_dir(v)).grid(
            row=drow, column=2, padx=4
        )
        drow += 1
    tab_dir.columnconfigure(1, weight=1)

    # ---- Generate tab: per-generation options (not assigned to a tab
    # in the requested layout, kept here next to the prompt) ----
    opts = ttk.Frame(tab_generate)
    opts.pack(fill=tk.X, pady=4)
    ttk.Label(opts, text="Aspect (prop):").pack(side=tk.LEFT, padx=4)
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

    # ---- collapsible log list (spoiler, hidden by default) ----
    log_frame = ttk.LabelFrame(tab_generate, text="Summary", padding=8)
    columns = tuple(LOG_FIELDS)
    list_container = ttk.Frame(log_frame)
    list_container.pack(fill=tk.BOTH, expand=True)
    tree = ttk.Treeview(list_container, columns=columns, show="headings", height=8)
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
    gen_btn = ttk.Button(bar, text="Generate")
    gen_btn.pack(side=tk.LEFT)
    cancel_btn = ttk.Button(bar, text="Cancel", state=tk.DISABLED)
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
        for child in tree.get_children():
            tree.delete(child)
        log_path = Path(out_var.get() or default_output_dir()) / LOG_FILENAME
        rows = read_log_rows(log_path) if log_path.exists() else []
        total = 0.0
        log_rows_cache.clear()
        for row in rows[-500:]:
            try:
                total += float(row.get("cost_usd") or 0)
            except ValueError:
                pass
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
        total_var.set(f"total: {len(rows)} ops / ${total:.6f}  ({log_path})")

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
        notebook.select(tab_generate)
        status_var.set("prompt loaded from log")

    list_menu = tk.Menu(root, tearoff=0)
    list_menu.add_command(label="Use prompt", command=use_prompt_from_list)

    def show_list_menu(event: object) -> None:
        item = tree.identify_row(event.y)  # type: ignore[attr-defined]
        if item:
            tree.selection_set(item)
            list_menu.post(event.x_root, event.y_root)  # type: ignore[attr-defined]

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
            messagebox.showerror("Generation failed", error)
        else:
            assert result is not None
            clock_var.set(f"elapsed: {result['elapsed']:.1f}s (done)")
            status_var.set(f"saved {len(result['images'])} image(s) | ${result['cost']:.6f}")
            show_done_dialog(list(result["images"]))
        toggle_log(True)

    def worker(prompt: str, kwargs: dict) -> None:
        try:
            result = run_generation(prompt=prompt, **kwargs)
        except GenerationCancelled:
            root.after(0, lambda: on_done(None, None, True))
        except Exception as exc:  # noqa: BLE001 - show any failure in GUI
            message = f"{type(exc).__name__}: {exc}"
            root.after(0, lambda: on_done(None, message))
        else:
            root.after(0, lambda: on_done(result, None))

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
            "output_dir": out_var.get().strip() or default_output_dir(),
            "context_dir": ctx_var.get().strip() or None,
            "memory_dir": mem_var.get().strip() or None,
            "model": model_var.get().strip() or PROVIDERS[current_provider]["default_model"],
            "aspect_ratio": prop_var.get().strip() or "1:1",
            "resolution": res_var.get().strip() or "1K",
            "output_format": fmt_var.get().strip() or "png",
            "seed": seed,
            "count": 1,
            "api_key": api_key,
            "dry_run": bool(dry_var.get()),
            "summary_model": summary_model,
            "provider": current_provider,
            "cancel_event": threading.Event(),
        }
        state["running"] = True
        state["cancel_event"] = kwargs["cancel_event"]
        state["start"] = time.perf_counter()
        state["elapsed"] = 0.0
        persist_gui_config()
        gen_btn.configure(state=tk.DISABLED)
        cancel_btn.configure(state=tk.NORMAL)
        spin.pack(side=tk.LEFT, padx=4)
        spin.start(50)
        status_var.set("generating...")
        tick_clock()
        threading.Thread(target=worker, args=(prompt, kwargs), daemon=True).start()

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
            })
        except OSError:
            pass

    gen_btn.configure(command=on_generate)
    cancel_btn.configure(command=on_cancel)
    refresh_log()

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
        args.list_log or args.forget_key or args.remember_key
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
