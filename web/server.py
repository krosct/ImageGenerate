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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import image_generate as ig

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


def _run_job(job_id: str, kwargs: dict) -> None:
    job = _job_or_404(job_id)
    try:
        result = ig.run_generation(**kwargs)
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
    api_key, source = ig.resolve_api_key(provider, req.api_key or None)
    if req.remember_key and (req.api_key or "").strip():
        try:
            ig.save_remembered_key(provider, req.api_key.strip())
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
    out = _safe_output_dir(req.output_dir)
    cancel_event = threading.Event()
    job_id = uuid.uuid4().hex
    kwargs = {
        "prompt": req.prompt,
        "output_dir": str(out),
        "context_dir": req.context_dir or None,
        "memory_dir": req.memory_dir or None,
        "model": req.model or ig.PROVIDERS[provider]["default_model"],
        "summary_model": req.summary_model.strip(),
        "aspect_ratio": req.prop,
        "resolution": req.resolution,
        "output_format": req.output_format,
        "seed": req.seed,
        "count": req.count,
        "api_key": api_key,
        "dry_run": req.dry_run,
        "provider": provider,
        "cancel_event": cancel_event,
    }
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "start": time.perf_counter(),
                        "cancel_event": cancel_event}
    threading.Thread(target=_run_job, args=(job_id, kwargs), daemon=True).start()
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
def api_log(output_dir: str | None = None) -> dict:
    log_path = _safe_output_dir(output_dir) / ig.LOG_FILENAME
    rows = ig.read_log_rows(log_path) if log_path.exists() else []
    total = 0.0
    for row in rows:
        try:
            total += float(row.get("cost_usd") or 0)
        except ValueError:
            continue
    return {"rows": rows[-500:], "total_ops": len(rows),
            "total_cost": round(total, 6), "fields": ig.LOG_FIELDS}


@app.get("/api/browse")
def api_browse(path: str | None = None) -> dict:
    """List subdirectories of a server-side folder (localhost folder picker)."""
    base = Path(path or str(Path.home())).expanduser()
    try:
        current = base.resolve()
    except OSError as exc:
        raise HTTPException(400, f"invalid path: {exc}") from exc
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
    data = ig.sanitize_gui_config(cfg.model_dump())
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
