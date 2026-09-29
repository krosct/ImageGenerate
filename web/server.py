#!/usr/bin/env python3
"""ImageGenerate web backend (FastAPI).

Serves the React frontend (web/frontend/dist, if built) and exposes the
core pipeline from image_generate.py over HTTP. Listens on 127.0.0.1 only.

Run:
    python3 web/server.py [--port 8000]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import image_generate as ig
import story_generate as sg

app = FastAPI(title="ImageGenerate")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    prompt: str
    output_dir: str | None = None
    context_dir: str | None = None
    memory_dir: str | None = None
    model: str | None = None
    summary_model: str = ""
    provider: str = "openrouter"
    prop: str = "1:1"
    resolution: str = "1K"
    output_format: str = "png"
    seed: int | None = None
    count: int = 1
    injection: list[dict[str, str]] = []
    # {"output_dir"|"context_dir"|"memory_dir": range | {"start", "range"}};
    # see ig.check_dynamic_dirs
    dynamic_dirs: dict[str, str | int | dict[str, str | int | None]] = {}
    dry_run: bool = False
    api_key: str | None = None
    remember_key: bool = False


class ConfigUpdate(BaseModel):
    output_dir: str = ""
    context_dir: str = ""
    memory_dir: str = ""
    provider: str = "openrouter"
    model: str = ""
    summary_model: str = ""
    prop: str = "1:1"
    resolution: str = "1K"
    output_format: str = "png"
    dry_run: bool = False
    prompt: str | None = None  # same "prompt" key the desktop GUI remembers
    analyse: list[str] | None = None
    chosen_dir: str | None = None
    log_sort: str | None = None


class RememberKeyRequest(BaseModel):
    api_key: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_output_dir(value: str | None) -> Path:
    out = Path(value or ig.default_output_dir()).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    return out.resolve()


def _job_or_404(job_id: str) -> dict:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return job


def _run_job(job_id: str, prompts: list[str], kwargs: dict) -> None:
    job = _job_or_404(job_id)

    def on_progress(info: dict) -> None:
        # Streamed over SSE so the frontend refreshes the log per image.
        with JOBS_LOCK:
            job["progress"] = {"done": info["done"], "total": info["total"],
                               "log_dirs": [str(Path(p).parent) for p in info["log_paths"]]}

    try:
        result = ig.run_generation_batch(prompts, on_progress=on_progress, **kwargs)
    except ig.GenerationCancelled:
        with JOBS_LOCK:
            job["status"] = "cancelled"
    except Exception as exc:  # noqa: BLE001 - reported via SSE
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"
    else:
        with JOBS_LOCK:
            job["status"] = "done"
            job["result"] = {
                "images": result["images"],
                "elapsed": result["elapsed"],
                "cost": result["cost"],
                "log_path": result["log_path"],
                "log_dirs": [str(Path(p).parent)
                             for p in result.get("log_paths") or [result["log_path"]]],
                "total_ops": result["total_ops"],
                "total_cost": result["total_cost"],
            }


# ---------------------------------------------------------------------------
# Generate / jobs
# ---------------------------------------------------------------------------

@app.post("/api/generate")
def api_generate(req: GenerateRequest) -> dict:
    try:
        provider = ig.normalize_provider(req.provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not req.prompt.strip():
        raise HTTPException(400, "prompt is empty")
    if not req.summary_model.strip():
        raise HTTPException(400, "summary_model is required")
    try:
        count = ig.parse_count(req.count)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    api_key, source = ig.resolve_api_key(provider, req.api_key or None)
    if req.remember_key and (req.api_key or "").strip():
        try:
            ig.save_remembered_key(provider, req.api_key.strip())
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
    out = _safe_output_dir(req.output_dir)
    var_names = ig.extract_template_vars(req.prompt)
    prompts = [req.prompt]
    if req.injection:
        if not var_names:
            raise HTTPException(400, "injection given but the prompt has no {{variables}}")
        cells = [[str(row.get(name, "") or "") for name in var_names]
                 for row in req.injection]
        if not any(cell.strip() for row in cells for cell in row):
            raise HTTPException(400, "injection table is completely empty")
        rows = ig.resolve_injection_rows(cells, var_names)
        prompts = [ig.apply_template_values(req.prompt, dict(zip(var_names, row)))
                   for row in rows]
        count = len(prompts)
    elif count > 1 and var_names:
        raise HTTPException(
            400, "prompt has {{variables}} and count > 1: send the injection table "
                 "(one row per generation)")
    try:
        ig.check_dynamic_dirs(req.dynamic_dirs, {"output_dir": str(out),
                                                 "context_dir": req.context_dir,
                                                 "memory_dir": req.memory_dir}, count)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    cancel_event = threading.Event()
    job_id = uuid.uuid4().hex
    kwargs = {
        "dynamic_dirs": dict(req.dynamic_dirs),
        "output_dir": str(out),
        "context_dir": req.context_dir or None,
        "memory_dir": req.memory_dir or None,
        "model": req.model or ig.PROVIDERS[provider]["default_model"],
        "summary_model": req.summary_model.strip(),
        "aspect_ratio": req.prop,
        "resolution": req.resolution,
        "output_format": req.output_format,
        "seed": req.seed,
        "count": count,
        "api_key": api_key,
        "dry_run": req.dry_run,
        "provider": provider,
        "cancel_event": cancel_event,
    }
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "start": time.perf_counter(),
                        "cancel_event": cancel_event}
    threading.Thread(target=_run_job, args=(job_id, prompts, kwargs),
                     daemon=True).start()
    return {"job_id": job_id, "key_source": source}


@app.get("/api/jobs/{job_id}/events")
async def api_job_events(job_id: str):
    job = _job_or_404(job_id)

    async def stream():
        while True:
            with JOBS_LOCK:
                status = job["status"]
                snapshot = {k: v for k, v in job.items() if k != "cancel_event"}
            if status == "running":
                snapshot["elapsed"] = time.perf_counter() - job["start"]
                if "step_start" in job:
                    snapshot["step_seconds"] = time.perf_counter() - job["step_start"]
                yield f"data: {json.dumps(snapshot)}\n\n"
                await asyncio.sleep(0.15)
            else:
                yield f"data: {json.dumps(snapshot)}\n\n"
                return

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel(job_id: str) -> dict:
    job = _job_or_404(job_id)
    with JOBS_LOCK:
        event = job.get("cancel_event")
        running = job["status"] == "running"
    if running and event is not None:
        event.set()
        ig.abort_all_http()
        return {"cancelled": True}
    return {"cancelled": False, "status": job["status"]}


# ---------------------------------------------------------------------------
# Images / log / config / providers / keys
# ---------------------------------------------------------------------------

@app.get("/api/images")
def api_image(output_dir: str, name: str):
    out = _safe_output_dir(output_dir)
    path = (out / name).resolve()
    if path.parent != out or not path.is_file():
        raise HTTPException(404, "image not found")
    return FileResponse(path)


@app.get("/api/log")
def api_log(output_dir: str | None = None,
            extra_dir: list[str] = Query(default=[])) -> dict:
    """Log rows newest first; extra_dir adds the logs of a batch's Dynamic
    output subfolders to the same list."""
    log_paths = [_safe_output_dir(output_dir) / ig.LOG_FILENAME]
    log_paths += [Path(d).expanduser() / ig.LOG_FILENAME for d in extra_dir]
    rows = ig.collect_log_rows(log_paths)
    total = 0.0
    for row in rows:
        try:
            total += float(row.get("cost_usd") or 0)
        except ValueError:
            continue
    return {"rows": rows[:5000], "total_ops": len(rows),
            "total_cost": round(total, 6), "fields": ig.LOG_FIELDS}


@app.get("/api/browse")
def api_browse(path: str | None = None) -> dict:
    """List subdirectories of a server-side folder (localhost folder picker).
    If the path does not exist, walks up to the nearest existing parent."""
    base = Path(path or str(Path.home())).expanduser()
    try:
        current = base.resolve()
    except OSError as exc:
        raise HTTPException(400, f"invalid path: {exc}") from exc
    # Walk up until we find an existing directory (handles missing leaf dirs)
    while current != current.parent and not current.exists():
        current = current.parent
    if not current.exists():
        raise HTTPException(404, "folder not found")
    if not current.is_dir():
        raise HTTPException(400, "not a folder")
    try:
        dirs = sorted(
            (d.name for d in current.iterdir() if d.is_dir()),
            key=str.casefold,
        )
    except PermissionError as exc:
        raise HTTPException(403, "permission denied") from exc
    return {"path": str(current),
            "missing": current != base.resolve(),
            "parent": str(current.parent),
            "home": str(Path.home()),
            "dirs": dirs}


class MkdirRequest(BaseModel):
    path: str
    name: str


@app.post("/api/browse/mkdir")
def api_mkdir(req: MkdirRequest) -> dict:
    """Create a subfolder (used by the folder picker)."""
    name = req.name.strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(400, "invalid folder name")
    try:
        base = Path(req.path).expanduser().resolve()
    except OSError as exc:
        raise HTTPException(400, f"invalid path: {exc}") from exc
    if not base.is_dir():
        raise HTTPException(404, "parent folder not found")
    target = base / name
    try:
        target.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise HTTPException(409, "folder already exists") from exc
    except PermissionError as exc:
        raise HTTPException(403, "permission denied") from exc
    return {"path": str(target)}


@app.get("/api/config")
def api_get_config() -> dict:
    return ig.sanitize_gui_config(ig.load_gui_config())


@app.put("/api/config")
def api_put_config(cfg: ConfigUpdate) -> dict:
    """Save the web settings MERGED into config.json: the desktop GUI keeps
    keys this form does not have (prompt, Analyse folders, chosen dir, list
    sort...), and a web save must not erase them."""
    current = ig.load_gui_config()
    data = ig.sanitize_gui_config({**current, **cfg.model_dump(exclude_unset=True)})
    ig.save_gui_config(data)
    return data


@app.get("/api/providers")
def api_providers() -> dict:
    return {
        "providers": [
            {"id": pid,
             "label": info["label"],
             "env_var": info["env_var"],
             "default_model": info["default_model"]}
            for pid, info in sorted(ig.PROVIDERS.items())
        ],
        "default_provider": ig.DEFAULT_PROVIDER,
        "aspect_ratios": ig.ASPECT_RATIOS,
        "resolutions": ig.RESOLUTIONS,
        "output_formats": ig.OUTPUT_FORMATS,
    }


@app.get("/api/keys")
def api_keys_status() -> dict:
    status = {}
    for pid, info in ig.PROVIDERS.items():
        env_set = bool(os.environ.get(info["env_var"], "").strip())
        vault_set = (ig.vault_dir() / f"{pid}_api_key.enc").exists() if ig.HAS_FERNET else False
        if env_set:
            source: str | None = "env"
        elif vault_set:
            source = "vault"
        else:
            source = None
        status[pid] = {"configured": source is not None, "source": source,
                       "vault_dir": str(ig.vault_dir())}
    return status


@app.put("/api/keys/{provider}")
def api_remember_key(provider: str, req: RememberKeyRequest) -> dict:
    try:
        pid = ig.normalize_provider(provider)
        path = ig.save_remembered_key(pid, req.api_key)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"saved": str(path)}


@app.delete("/api/keys/{provider}")
def api_forget_key(provider: str) -> dict:
    try:
        pid = ig.normalize_provider(provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"forgotten": ig.forget_remembered_key(pid)}


# ---------------------------------------------------------------------------
# Local files (the server only listens on 127.0.0.1): images, thumbnails,
# story audio, reports; open a folder in the desktop file manager
# ---------------------------------------------------------------------------

LOCAL_FILE_EXTS = set(ig.IMAGE_EXTS) | {".wav", ".md", ".csv"}


def _local_file(path: str) -> Path:
    target = Path(path).expanduser()
    try:
        target = target.resolve()
    except OSError as exc:
        raise HTTPException(400, f"invalid path: {exc}") from exc
    if target.suffix.lower() not in LOCAL_FILE_EXTS or not target.is_file():
        raise HTTPException(404, "file not found (images, .wav, .md, .csv only)")
    return target


@app.get("/api/file")
def api_file(path: str):
    """A local image / story audio / report (audio supports seeking: ranges)."""
    return FileResponse(_local_file(path))


@app.get("/api/thumb")
def api_thumb(path: str, size: int = ig.THUMB_SIZE):
    """Small cached PNG of a local image (Analyse grid, previews)."""
    source = _local_file(path)
    thumb = ig.make_thumbnail(source, max(40, min(size, 4000)))
    return FileResponse(thumb if thumb is not None else source)


class OpenRequest(BaseModel):
    path: str


@app.post("/api/open")
def api_open(req: OpenRequest) -> dict:
    """Open a local folder/file with the desktop default app (xdg-open)."""
    target = Path(req.path).expanduser()
    if not target.exists():
        raise HTTPException(404, "path not found")
    import subprocess

    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, str(target)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise HTTPException(500, f"cannot open: {exc}") from exc
    return {"opened": str(target)}


# ---------------------------------------------------------------------------
# Analyse (ImageGenerate tab): folders side by side, Choose copies + report
# ---------------------------------------------------------------------------

@app.get("/api/analyse/rows")
def api_analyse_rows(folder: list[str] = Query(default=[]), newest_first: bool = True) -> dict:
    folders = [Path(f).expanduser() for f in folder]
    try:
        rows = ig.build_analysis_rows(folders, newest_first)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    def cell(path: Path | None) -> dict | None:
        if path is None:
            return None
        stat = path.stat()
        _bytes, width, height = ig.inspect_image(path)
        return {"path": str(path), "name": path.name, "size": stat.st_size,
                "width": width, "height": height,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))}

    return {"folders": [{"path": str(f), "name": f.name,
                         "count": sum(1 for r in rows if r[i] is not None)}
                        for i, f in enumerate(folders)],
            "rows": [[cell(c) for c in row] for row in rows]}


class ChooseRequest(BaseModel):
    folders: list[str]
    picks: dict[str, int]          # {"row index": folder index}
    chosen_dir: str = ""
    output_dir: str = ""
    newest_first: bool = True


@app.post("/api/analyse/choose")
def api_analyse_choose(req: ChooseRequest) -> dict:
    target = req.chosen_dir.strip() or ig.default_chosen_dir(req.output_dir or None)
    try:
        picks = {int(row): int(col) for row, col in req.picks.items()}
        return ig.choose_images(req.folders, picks, target, req.newest_first)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/analyse/default-chosen")
def api_default_chosen(output_dir: str = "") -> dict:
    return {"chosen_dir": ig.default_chosen_dir(output_dir or None)}


# ---------------------------------------------------------------------------
# StoryGenerate (storyboards -> written + narrated stories), same core as
# story_generate.py; jobs reuse /api/jobs/{id}/events and /cancel
# ---------------------------------------------------------------------------

class StoryConfigUpdate(BaseModel):
    input_dir: str | None = None
    output_dir: str | None = None
    writer_model: str | None = None
    tts_model: str | None = None
    language: str | None = None
    voices: list[dict] | None = None
    dry_run: bool | None = None
    force: bool | None = None
    style: str | None = None
    duration: str | None = None
    log_sort: str | None = None


def _story_config() -> dict:
    data = sg.sanitize_config(sg.load_config())
    data.setdefault("output_dir", "")
    if not data.get("output_dir"):
        data["output_dir"] = sg.default_output_dir()
    data.setdefault("writer_model", sg.WRITER_DEFAULT_MODEL)
    data.setdefault("language", sg.DEFAULT_LANGUAGE)
    data.setdefault("input_dir", "")
    return data


@app.get("/api/story/meta")
def api_story_meta() -> dict:
    return {"tts_models": sg.TTS_MODELS, "default_tts_model": sg.DEFAULT_TTS_MODEL,
            "writer_default_model": sg.WRITER_DEFAULT_MODEL, "languages": sg.LANGUAGES,
            "styles": [{"id": k, "label": v["label"], "short": sg.style_short_label(k)}
                       for k, v in sg.WRITER_STYLES.items()],
            "default_style": sg.DEFAULT_STYLE, "max_voices": sg.MAX_VOICES,
            "log_fields": sg.LOG_FIELDS, "default_output_dir": sg.default_output_dir()}


@app.get("/api/story/config")
def api_story_config() -> dict:
    return _story_config()


@app.put("/api/story/config")
def api_story_put_config(cfg: StoryConfigUpdate) -> dict:
    """Merged into story_config.json (the desktop StoryGenerate shares it)."""
    data = sg.sanitize_config({**sg.load_config(), **cfg.model_dump(exclude_unset=True)})
    sg.save_config(data)
    return _story_config()


class VoicesRequest(BaseModel):
    voices: list[dict]


@app.post("/api/story/voices/check")
def api_story_check_voices(req: VoicesRequest) -> dict:
    try:
        voices = sg.validate_voices(req.voices)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"voices": sg.describe_voices(voices)}


def _story_log(output_dir: str) -> list[dict]:
    return list(reversed(sg.read_log_rows(Path(output_dir).expanduser() / sg.LOG_FILENAME)))


@app.get("/api/story/log")
def api_story_log(output_dir: str = "", style: str = "") -> dict:
    out = output_dir or _story_config()["output_dir"]
    rows = _story_log(out)
    failed = sg.failed_storyboards(out, style or None) if Path(out).expanduser().is_dir() else []
    return {"rows": rows, "fields": sg.LOG_FIELDS, "output_dir": str(Path(out).expanduser()),
            "home": str(Path.home()),
            "failed": [str(f) for f in failed],
            "total_cost": round(sum(sg.row_cost(r) for r in rows), 6)}


@app.get("/api/story/stories")
def api_story_stories(output_dir: str = "") -> dict:
    out = output_dir or _story_config()["output_dir"]
    stories = []
    for folder, meta in sg.list_stories(out):
        image = sg.find_storyboard_image(folder, meta)
        audio = folder / str(meta.get("audio") or sg.AUDIO_WAV)
        stories.append({
            "folder": str(folder), "label": sg.story_list_label(folder, meta),
            "title": meta.get("title", folder.name), "style": sg.story_style(meta),
            "language": meta.get("language", ""), "logline": meta.get("logline", ""),
            "scenes": meta.get("scenes", []), "voice_label": meta.get("voice_label", ""),
            "voice_id": meta.get("voice_id", ""), "voice_reason": meta.get("voice_reason", ""),
            "audio_seconds": meta.get("audio_seconds", 0),
            "image": str(image) if image else "",
            "audio": str(audio) if audio.is_file() else "",
            "audio_deleted": bool(meta.get("audio_deleted")),
            "scene_word": sg.scene_word(meta.get("language", "")),
        })
    return {"stories": stories}


class StoryDeleteRequest(BaseModel):
    folder: str
    audio_only: bool = False


@app.post("/api/story/delete")
def api_story_delete(req: StoryDeleteRequest) -> dict:
    try:
        method = sg.delete_story(req.folder, audio_only=req.audio_only)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"method": method}


class StoryGenerateRequest(BaseModel):
    input_dir: str = ""
    images: list[str] = []
    retry_failed: bool = False
    output_dir: str = ""
    writer_model: str = ""
    tts_model: str = ""
    language: str = ""
    style: str = ""
    duration: str = ""
    voices: list[dict] = []
    dry_run: bool = False
    force: bool = False
    api_key: str | None = None


def _run_story_job(job_id: str, images: list[Path], kwargs: dict) -> None:
    job = _job_or_404(job_id)

    def on_progress(info: dict) -> None:
        with JOBS_LOCK:
            job["progress"] = {"done": info["done"], "total": info["total"],
                               "failed": len(info["errors"])}

    def on_status(message: str) -> None:
        with JOBS_LOCK:
            job["step"] = message
            job["step_start"] = time.perf_counter()

    try:
        batch = sg.run_story_batch(images, on_progress=on_progress, on_status=on_status,
                                   **kwargs)
    except ig.GenerationCancelled:
        with JOBS_LOCK:
            job["status"] = "cancelled"
    except Exception as exc:  # noqa: BLE001 - reported via SSE
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"
    else:
        with JOBS_LOCK:
            job["status"] = "done"
            job["result"] = {
                "created": [r["folder"] for r in batch["created"]],
                "skipped": [r["folder"] for r in batch["skipped"]],
                "errors": batch["errors"], "cost": round(batch["cost"], 6),
                "elapsed": time.perf_counter() - job["start"],
            }


@app.post("/api/story/generate")
def api_story_generate(req: StoryGenerateRequest) -> dict:
    cfg = _story_config()
    output_dir = req.output_dir or cfg["output_dir"]
    style = req.style or cfg.get("style") or sg.DEFAULT_STYLE
    try:
        if req.retry_failed:
            images = sg.failed_storyboards(output_dir, style)
        elif req.images:
            images = [Path(p).expanduser() for p in req.images]
        else:
            images = sg.list_storyboards(req.input_dir or cfg.get("input_dir") or "")
        if not images:
            raise ValueError("no storyboard to process (folder empty or nothing failed)")
        voices = sg.validate_voices(req.voices, allow_empty=req.dry_run)
        target_s = sg.parse_duration(req.duration)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    api_key, _source = sg.resolve_openrouter_key(req.api_key or None)
    cancel_event = threading.Event()
    job_id = uuid.uuid4().hex
    kwargs = {"voices": voices, "dry_run": req.dry_run, "cancel_event": cancel_event,
              "output_dir": output_dir,
              "writer_model": req.writer_model or cfg.get("writer_model") or sg.WRITER_DEFAULT_MODEL,
              "tts_model": req.tts_model or cfg.get("tts_model") or sg.DEFAULT_TTS_MODEL,
              "language": req.language or cfg.get("language") or sg.DEFAULT_LANGUAGE,
              "style": style, "target_s": target_s, "openrouter_key": api_key,
              "force": req.force and not req.retry_failed}
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "start": time.perf_counter(),
                        "cancel_event": cancel_event, "kind": "story",
                        "progress": {"done": 0, "total": len(images), "failed": 0},
                        "step": "starting", "step_start": time.perf_counter()}
    threading.Thread(target=_run_story_job, args=(job_id, images, kwargs),
                     daemon=True).start()
    return {"job_id": job_id, "total": len(images)}


# ---------------------------------------------------------------------------
# Docs page (repo-root docs.html, single file, no dependencies)
# ---------------------------------------------------------------------------

DOCS = Path(__file__).resolve().parent.parent / "docs.html"


@app.get("/help", include_in_schema=False)
def api_docs():
    if not DOCS.is_file():
        raise HTTPException(404, "docs.html not found (see repo root)")
    return FileResponse(DOCS, media_type="text/html")


# ---------------------------------------------------------------------------
# Frontend static (Vite build output)
# ---------------------------------------------------------------------------

DIST = Path(__file__).resolve().parent / "frontend" / "dist"
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
else:
    @app.get("/")
    def api_no_frontend() -> dict:
        return {"ok": True,
                "hint": "Build the frontend: cd web/frontend && npm install && npm run build"}


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="ImageGenerate web server (localhost only).")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
