#!/usr/bin/env python3
"""Offline regression suite for the web backend (web/server.py).

Stdlib unittest + FastAPI TestClient. No network, no API cost:
- generations use ``dry_run`` placeholders (no key needed);
- the vault/config are isolated per test via ``XDG_CONFIG_HOME``;
- provider env vars are saved/restored around each test.

Run:
    python3 -m unittest discover -s tests -v
Targeted:
    python3 -m unittest tests.test_web_server -v -k generate
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import unittest
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import image_generate as ig

server: Any = None
TestClient: Any = None
try:
    from fastapi.testclient import TestClient as _TestClient
    import web.server as _server
    TestClient = _TestClient
    server = _server
    HAS_WEB = True
except ImportError:  # fastapi/httpx not installed -> skip whole module
    HAS_WEB = False


@unittest.skipUnless(HAS_WEB, "fastapi/httpx not installed")
class WebBase(unittest.TestCase):

    def setUp(self):
        self._tmp_config = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_config.cleanup)
        self._had_xdg = "XDG_CONFIG_HOME" in os.environ
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self._tmp_config.name
        self._had_env: dict[str, bool] = {}
        self._saved_env: dict[str, str | None] = {}
        for var in ("OPENROUTER_API_KEY", "GEMINI_API_KEY"):
            self._had_env[var] = var in os.environ
            self._saved_env[var] = os.environ.get(var)
            os.environ.pop(var, None)
        self.addCleanup(self._restore_env)
        with server.JOBS_LOCK:
            server.JOBS.clear()
        self.addCleanup(self._clear_jobs)
        self.client = TestClient(server.app)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "out"
        self.out.mkdir()
        self._tmp_out = tmp

    def _restore_env(self):
        if self._had_xdg and self._old_xdg is not None:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg
        else:
            os.environ.pop("XDG_CONFIG_HOME", None)
        for var in ("OPENROUTER_API_KEY", "GEMINI_API_KEY"):
            if self._had_env[var] and self._saved_env[var] is not None:
                restored = self._saved_env[var]
                assert restored is not None
                os.environ[var] = restored
            else:
                os.environ.pop(var, None)

    def _clear_jobs(self):
        with server.JOBS_LOCK:
            server.JOBS.clear()

    def _generate(self, **over) -> dict:
        body = {"prompt": "a cat", "summary_model": "test-model",
                "output_dir": str(self.out), "dry_run": True}
        body.update(over)
        resp = self.client.post("/api/generate", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _wait_job(self, job_id: str, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with server.JOBS_LOCK:
                job = dict(server.JOBS.get(job_id, {}))
            if job.get("status") in ("done", "error", "cancelled"):
                return job
            time.sleep(0.05)
        self.fail(f"job {job_id} still running after {timeout}s")


@unittest.skipUnless(HAS_WEB, "fastapi/httpx not installed")
class GenerateValidationTest(WebBase):
    """POST /api/generate mirrors the CLI validation rules."""

    def test_empty_prompt_rejected(self):
        resp = self.client.post("/api/generate",
                                json={"prompt": "  ", "summary_model": "m"})
        self.assertEqual(resp.status_code, 400)

    def test_missing_summary_model_rejected(self):
        resp = self.client.post("/api/generate",
                                json={"prompt": "a cat", "summary_model": "  "})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_provider_rejected(self):
        resp = self.client.post("/api/generate",
                                json={"prompt": "a cat", "summary_model": "m",
                                      "provider": "nope"})
        self.assertEqual(resp.status_code, 400)

    def test_bad_count_rejected(self):
        resp = self.client.post("/api/generate",
                                json={"prompt": "a cat", "summary_model": "m",
                                      "count": 99})
        self.assertEqual(resp.status_code, 400)

    def test_injection_without_vars_rejected(self):
        resp = self.client.post("/api/generate",
                                json={"prompt": "plain", "summary_model": "m",
                                      "injection": [{"a": "b"}]})
        self.assertEqual(resp.status_code, 400)

    def test_empty_injection_rejected(self):
        resp = self.client.post("/api/generate",
                                json={"prompt": "a {{x}}", "summary_model": "m",
                                      "injection": [{"x": "  "}]})
        self.assertEqual(resp.status_code, 400)

    def test_count_gt1_with_vars_needs_injection(self):
        resp = self.client.post("/api/generate",
                                json={"prompt": "a {{x}}", "summary_model": "m",
                                      "count": 3, "dry_run": True})
        self.assertEqual(resp.status_code, 400)


@unittest.skipUnless(HAS_WEB, "fastapi/httpx not installed")
class GenerateFlowTest(WebBase):
    """Happy paths: dry-run jobs finish and are served over SSE."""

    def test_dry_run_job_completes(self):
        data = self._generate()
        self.assertIn("job_id", data)
        self.assertEqual(data["key_source"], "none")
        job = self._wait_job(data["job_id"])
        self.assertEqual(job["status"], "done")
        result = job["result"]
        self.assertEqual(len(result["images"]), 1)
        self.assertTrue(Path(result["images"][0]).exists())
        rows = ig.read_log_rows(self.out / ig.LOG_FILENAME)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["prompt_full"], "a cat")

    def test_injection_flow_templates_prompts(self):
        data = self._generate(prompt="a {{animal}}",
                              injection=[{"animal": "cat"}, {"animal": "dog"}])
        job = self._wait_job(data["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(len(job["result"]["images"]), 2)
        fulls = sorted(r["prompt_full"]
                       for r in ig.read_log_rows(self.out / ig.LOG_FILENAME))
        self.assertEqual(fulls, ["a cat", "a dog"])

    def test_events_stream_reports_done(self):
        data = self._generate()
        self._wait_job(data["job_id"])
        resp = self.client.get(f"/api/jobs/{data['job_id']}/events")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("done", resp.text)

    def test_unknown_job_events_404(self):
        self.assertEqual(self.client.get("/api/jobs/nope/events").status_code, 404)

    def test_remember_key_on_generate(self):
        if not ig.HAS_FERNET:
            self.skipTest("cryptography not installed")
        data = self._generate(api_key="web-key", remember_key=True)
        self._wait_job(data["job_id"])
        self.assertEqual(ig.load_remembered_key("openrouter"), "web-key")


@unittest.skipUnless(HAS_WEB, "fastapi/httpx not installed")
class JobCancelTest(WebBase):

    def test_cancel_unknown_job_404(self):
        self.assertEqual(self.client.post("/api/jobs/nope/cancel").status_code, 404)

    def test_cancel_running_job(self):
        event = threading.Event()
        job_id = "test-running"
        with server.JOBS_LOCK:
            server.JOBS[job_id] = {"status": "running", "start": time.perf_counter(),
                                   "cancel_event": event}
        resp = self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["cancelled"])
        self.assertTrue(event.is_set())

    def test_cancel_finished_job_reports_status(self):
        data = self._generate()
        self._wait_job(data["job_id"])
        resp = self.client.post(f"/api/jobs/{data['job_id']}/cancel")
        self.assertEqual(resp.json(), {"cancelled": False, "status": "done"})

    def test_run_job_error_path(self):
        job_id = "test-error"
        with server.JOBS_LOCK:
            server.JOBS[job_id] = {"status": "running"}
        with mock.patch.object(ig, "run_generation_batch",
                               side_effect=RuntimeError("boom")):
            server._run_job(job_id, ["p"], {})
        with server.JOBS_LOCK:
            job = server.JOBS[job_id]
        self.assertEqual(job["status"], "error")
        self.assertIn("boom", job["error"])

    def test_run_job_cancelled_path(self):
        job_id = "test-cancelled"
        with server.JOBS_LOCK:
            server.JOBS[job_id] = {"status": "running"}
        with mock.patch.object(ig, "run_generation_batch",
                               side_effect=ig.GenerationCancelled("stop")):
            server._run_job(job_id, ["p"], {})
        with server.JOBS_LOCK:
            self.assertEqual(server.JOBS[job_id]["status"], "cancelled")

    def test_run_job_success_path(self):
        job_id = "test-ok"
        with server.JOBS_LOCK:
            server.JOBS[job_id] = {"status": "running"}
        fake = {"images": ["i.png"], "elapsed": 1.0, "cost": 0.01,
                "log_path": "l.csv", "total_ops": 1, "total_cost": 0.01}
        with mock.patch.object(ig, "run_generation_batch", return_value=fake):
            server._run_job(job_id, ["p"], {})
        with server.JOBS_LOCK:
            job = server.JOBS[job_id]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["images"], ["i.png"])


@unittest.skipUnless(HAS_WEB, "fastapi/httpx not installed")
class ImagesLogTest(WebBase):

    def test_serve_image_and_traversal_blocked(self):
        ig.write_placeholder_png(self.out / "pic.png", 8, 8)
        resp = self.client.get("/api/images", params={"output_dir": str(self.out),
                                                      "name": "pic.png"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content[:8], b"\x89PNG\r\n\x1a\n")
        blocked = self.client.get("/api/images", params={"output_dir": str(self.out),
                                                         "name": "../server.py"})
        self.assertEqual(blocked.status_code, 404)
        missing = self.client.get("/api/images", params={"output_dir": str(self.out),
                                                         "name": "nope.png"})
        self.assertEqual(missing.status_code, 404)

    def test_log_empty_and_filled(self):
        resp = self.client.get("/api/log", params={"output_dir": str(self.out)})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual((body["rows"], body["total_ops"], body["total_cost"]),
                         ([], 0, 0.0))
        self.assertEqual(body["fields"], ig.LOG_FIELDS)
        self._generate()
        # wait for the row to be logged
        deadline = time.time() + 15
        rows: list = []
        while time.time() < deadline:
            rows = ig.read_log_rows(self.out / ig.LOG_FILENAME)
            if rows:
                break
            time.sleep(0.05)
        resp = self.client.get("/api/log", params={"output_dir": str(self.out)})
        body = resp.json()
        self.assertEqual(body["total_ops"], 1)
        self.assertEqual(len(body["rows"]), 1)


@unittest.skipUnless(HAS_WEB, "fastapi/httpx not installed")
class BrowseMkdirTest(WebBase):

    def test_browse_lists_dirs(self):
        (self.out / "sub").mkdir()
        (self.out / "file.txt").write_text("x", encoding="utf-8")
        resp = self.client.get("/api/browse", params={"path": str(self.out)})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("sub", body["dirs"])
        self.assertNotIn("file.txt", body["dirs"])

    def test_browse_walks_up_for_missing_leaf(self):
        resp = self.client.get("/api/browse",
                               params={"path": str(self.out / "nope" / "deeper")})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["path"], str(self.out.resolve()))

    def test_browse_not_a_folder(self):
        target = self.out / "f.txt"
        target.write_text("x", encoding="utf-8")
        resp = self.client.get("/api/browse", params={"path": str(target)})
        self.assertEqual(resp.status_code, 400)

    def test_mkdir_flow(self):
        resp = self.client.post("/api/browse/mkdir",
                                json={"path": str(self.out), "name": "newdir"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue((self.out / "newdir").is_dir())
        dup = self.client.post("/api/browse/mkdir",
                               json={"path": str(self.out), "name": "newdir"})
        self.assertEqual(dup.status_code, 409)

    def test_mkdir_invalid_and_missing_parent(self):
        for bad in ["", ".", "..", "a/b", "a\\b"]:
            resp = self.client.post("/api/browse/mkdir",
                                    json={"path": str(self.out), "name": bad})
            self.assertEqual(resp.status_code, 400, bad)
        resp = self.client.post("/api/browse/mkdir",
                                json={"path": str(self.out / "nope"), "name": "x"})
        self.assertEqual(resp.status_code, 404)


@unittest.skipUnless(HAS_WEB, "fastapi/httpx not installed")
class ConfigProvidersKeysTest(WebBase):

    def test_config_roundtrip_sanitizes(self):
        resp = self.client.put("/api/config", json={
            "output_dir": "", "context_dir": "", "memory_dir": "",
            "provider": "openrouter", "model": "m", "summary_model": "s",
            "prop": "bogus", "resolution": "1K", "output_format": "png",
            "dry_run": False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["prop"], "1:1")
        fetched = self.client.get("/api/config").json()
        self.assertEqual(fetched["prop"], "1:1")
        self.assertEqual(fetched["model"], "m")

    def test_providers_lists_registry(self):
        body = self.client.get("/api/providers").json()
        ids = {p["id"] for p in body["providers"]}
        self.assertEqual(ids, set(ig.PROVIDERS))
        self.assertIn("1:1", body["aspect_ratios"])
        self.assertIn("1K", body["resolutions"])
        self.assertIn("png", body["output_formats"])

    def test_keys_status_remember_forget(self):
        if not ig.HAS_FERNET:
            self.skipTest("cryptography not installed")
        status = self.client.get("/api/keys").json()
        self.assertIsNone(status["openrouter"]["source"])
        os.environ["OPENROUTER_API_KEY"] = "env-key"
        status = self.client.get("/api/keys").json()
        self.assertEqual(status["openrouter"]["source"], "env")
        del os.environ["OPENROUTER_API_KEY"]
        saved = self.client.put("/api/keys/openrouter", json={"api_key": "vault-key"})
        self.assertEqual(saved.status_code, 200)
        status = self.client.get("/api/keys").json()
        self.assertEqual(status["openrouter"]["source"], "vault")
        forgotten = self.client.delete("/api/keys/openrouter").json()
        self.assertTrue(forgotten["forgotten"])
        again = self.client.delete("/api/keys/openrouter").json()
        self.assertFalse(again["forgotten"])

    def test_keys_invalid_provider(self):
        self.assertEqual(
            self.client.put("/api/keys/nope", json={"api_key": "k"}).status_code, 400)
        self.assertEqual(self.client.delete("/api/keys/nope").status_code, 400)

    def test_safe_output_dir_creates_and_resolves(self):
        target = self.out / "nested" / "dir"
        resolved = server._safe_output_dir(str(target))
        self.assertTrue(resolved.is_dir())
        self.assertTrue(resolved.is_absolute())


if __name__ == "__main__":
    unittest.main()
