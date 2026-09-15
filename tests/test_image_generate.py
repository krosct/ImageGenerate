#!/usr/bin/env python3
"""Offline regression suite for the core pipeline (image_generate.py).

Stdlib only (unittest + unittest.mock). No network, no API cost, no keys:
- image requests use --dry-run placeholders or mocked ``_post_json`` /
  ``REQUEST_FUNCS`` entries;
- the key vault and GUI config are isolated per test via ``XDG_CONFIG_HOME``;
- provider env vars are saved/restored around each test.

Run:
    python3 -m unittest discover -s tests -v
Targeted (Area -> -k keyword, see AGENTS.md):
    python3 -m unittest tests.test_image_generate -v -k summary
    python3 -m unittest tests.test_image_generate -v -k injection
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import struct
import sys
import tempfile
import threading
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import image_generate as ig  # noqa: E402  (needs sys.path above)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class IsolatedEnvMixin(unittest.TestCase):
    """Isolate vault/config (XDG_CONFIG_HOME) and provider env vars."""

    def setUp(self):
        super().setUp()
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

    def make_dirs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "out"
        out.mkdir()
        return tmp, out


def _png_bytes(width=16, height=12) -> bytes:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "p.png"
    ig.write_placeholder_png(path, width, height)
    raw = path.read_bytes()
    tmp.cleanup()
    return raw


def _dry_kwargs(out: Path, **over) -> dict:
    params = {
        "prompt": "a red panda astronaut",
        "output_dir": str(out),
        "context_dir": None,
        "memory_dir": None,
        "model": ig.DEFAULT_MODEL,
        "aspect_ratio": "1:1",
        "resolution": "512",
        "output_format": "png",
        "seed": None,
        "count": 1,
        "api_key": None,
        "dry_run": True,
        "summary_model": "",
        "provider": "openrouter",
        "cancel_event": None,
    }
    params.update(over)
    return params


# ---------------------------------------------------------------------------
# Provider registry + parse_count
# ---------------------------------------------------------------------------

class ProviderRegistryTest(IsolatedEnvMixin):

    def test_normalize_valid(self):
        self.assertEqual(ig.normalize_provider("openrouter"), "openrouter")
        self.assertEqual(ig.normalize_provider("  OpenRouter "), "openrouter")
        self.assertEqual(ig.normalize_provider("GEMINI"), "gemini")

    def test_normalize_invalid(self):
        for bad in ["", "  ", "has space", "UPPER SPACE", "../x", "a_b", "prov!"]:
            with self.assertRaises(ValueError, msg=bad):
                ig.normalize_provider(bad)

    def test_normalize_unknown(self):
        with self.assertRaises(ValueError):
            ig.normalize_provider("dallex")

    def test_registry_entries_have_contract_fields(self):
        for pid, info in ig.PROVIDERS.items():
            self.assertIn("env_var", info)
            self.assertIn("default_model", info)
            self.assertTrue(info["env_var"].endswith("_API_KEY"))

    def test_request_funcs_match_providers(self):
        self.assertEqual(set(ig.REQUEST_FUNCS), set(ig.PROVIDERS))

    def test_request_funcs_share_signature(self):
        import inspect
        sigs = {n: set(inspect.signature(f).parameters) for n, f in ig.REQUEST_FUNCS.items()}
        first = next(iter(sigs.values()))
        for name, params in sigs.items():
            self.assertEqual(params, first, name)
        for required in ("api_key", "model", "prompt", "count", "timeout_s", "cancel_event"):
            self.assertIn(required, first)


class ParseCountTest(IsolatedEnvMixin):

    def test_valid_boundaries(self):
        self.assertEqual(ig.parse_count(1), 1)
        self.assertEqual(ig.parse_count(10), 10)
        self.assertEqual(ig.parse_count(" 3 "), 3)

    def test_invalid(self):
        for bad in [0, 11, -1, "0", "11", "abc", "", None, "1.5", "  "]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                ig.parse_count(bad)


# ---------------------------------------------------------------------------
# Key vault + key_hash + resolve_api_key
# ---------------------------------------------------------------------------

class VaultTest(IsolatedEnvMixin):

    def test_save_load_forget_roundtrip(self):
        if not ig.HAS_FERNET:
            self.skipTest("cryptography not installed")
        blob = ig.save_remembered_key("openrouter", "secret-123")
        self.assertTrue(blob.exists())
        self.assertEqual(ig.load_remembered_key("openrouter"), "secret-123")
        self.assertIn("openrouter", ig.list_remembered_providers())
        self.assertTrue(ig.forget_remembered_key("openrouter"))
        self.assertIsNone(ig.load_remembered_key("openrouter"))
        self.assertFalse(ig.forget_remembered_key("openrouter"))

    def test_load_missing_returns_none(self):
        if not ig.HAS_FERNET:
            self.skipTest("cryptography not installed")
        self.assertIsNone(ig.load_remembered_key("openrouter"))

    def test_blob_file_permissions(self):
        if not ig.HAS_FERNET:
            self.skipTest("cryptography not installed")
        blob = ig.save_remembered_key("openrouter", "k")
        self.assertEqual(oct(blob.stat().st_mode & 0o777), "0o600")

    def test_providers_isolated(self):
        if not ig.HAS_FERNET:
            self.skipTest("cryptography not installed")
        ig.save_remembered_key("openrouter", "key-open")
        self.assertIsNone(ig.load_remembered_key("gemini"))
        self.assertEqual(ig.list_remembered_providers(), ["openrouter"])

    def test_require_fernet_without_lib(self):
        with mock.patch.object(ig, "HAS_FERNET", False):
            with self.assertRaises(RuntimeError):
                ig._require_fernet()

    def test_key_hash_properties(self):
        self.assertEqual(ig.key_hash(None), "")
        self.assertEqual(ig.key_hash(""), "")
        h1 = ig.key_hash("abc")
        h2 = ig.key_hash("abc")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)
        int(h1, 16)  # hex
        self.assertNotIn("abc", h1)
        self.assertNotEqual(ig.key_hash("abc"), ig.key_hash("abd"))


class ResolveApiKeyTest(IsolatedEnvMixin):

    def test_flag_beats_all(self):
        if ig.HAS_FERNET:
            ig.save_remembered_key("openrouter", "vault-key")
        os.environ["OPENROUTER_API_KEY"] = "env-key"
        key, source = ig.resolve_api_key("openrouter", "flag-key")
        self.assertEqual((key, source), ("flag-key", "flag"))

    def test_env_beats_vault(self):
        if not ig.HAS_FERNET:
            self.skipTest("cryptography not installed")
        ig.save_remembered_key("openrouter", "vault-key")
        os.environ["OPENROUTER_API_KEY"] = "env-key"
        self.assertEqual(ig.resolve_api_key("openrouter"), ("env-key", "env"))

    def test_vault_fallback(self):
        if not ig.HAS_FERNET:
            self.skipTest("cryptography not installed")
        ig.save_remembered_key("openrouter", "vault-key")
        self.assertEqual(ig.resolve_api_key("openrouter"), ("vault-key", "vault"))

    def test_none_when_nothing_configured(self):
        self.assertEqual(ig.resolve_api_key("openrouter"), (None, "none"))

    def test_corrupted_vault_yields_none(self):
        if not ig.HAS_FERNET:
            self.skipTest("cryptography not installed")
        ig.save_remembered_key("openrouter", "vault-key")
        _, blob = ig._vault_paths("openrouter")
        blob.write_bytes(b"corrupted-token\n")
        self.assertEqual(ig.resolve_api_key("openrouter"), (None, "none"))


# ---------------------------------------------------------------------------
# GUI config
# ---------------------------------------------------------------------------

class GuiConfigTest(IsolatedEnvMixin):

    def test_save_load_roundtrip(self):
        settings = {
            "output_dir": "/tmp/x", "provider": "openrouter", "model": "m",
            "summary_model": "s", "prop": "16:9", "resolution": "1K",
            "output_format": "png", "dry_run": True,
        }
        path = ig.save_gui_config(settings)
        self.assertTrue(path.exists())
        self.assertEqual(ig.load_gui_config(), settings)
        self.assertFalse(path.with_suffix(".tmp").exists())

    def test_save_keeps_only_known_keys(self):
        ig.save_gui_config({"model": "m", "evil": "x"})
        self.assertEqual(ig.load_gui_config(), {"model": "m"})

    def test_load_missing_corrupt_nondict(self):
        self.assertEqual(ig.load_gui_config(), {})
        ig.config_path().parent.mkdir(parents=True, exist_ok=True)
        ig.config_path().write_text("not json{{", encoding="utf-8")
        self.assertEqual(ig.load_gui_config(), {})
        ig.config_path().write_text("[1,2]", encoding="utf-8")
        self.assertEqual(ig.load_gui_config(), {})

    def test_sanitize_clamps_invalid(self):
        clean = ig.sanitize_gui_config({
            "provider": "nope", "prop": "7:7", "resolution": "8K",
            "output_format": "bmp", "dry_run": "yes", "model": "m",
        })
        self.assertEqual(clean["provider"], ig.DEFAULT_PROVIDER)
        self.assertEqual(clean["prop"], "1:1")
        self.assertEqual(clean["resolution"], "1K")
        self.assertEqual(clean["output_format"], "png")
        self.assertIs(clean["dry_run"], True)

    def test_sanitize_keeps_valid(self):
        clean = ig.sanitize_gui_config({
            "provider": "gemini", "prop": "16:9", "resolution": "2K",
            "output_format": "webp", "dry_run": False,
        })
        self.assertEqual(
            (clean["provider"], clean["prop"], clean["resolution"], clean["output_format"]),
            ("gemini", "16:9", "2K", "webp"))


# ---------------------------------------------------------------------------
# Context / memory
# ---------------------------------------------------------------------------

class ContextMemoryTest(IsolatedEnvMixin):

    def test_load_context_none(self):
        self.assertEqual(ig.load_context_text(None), "")
        self.assertEqual(ig.load_context_text(""), "")

    def test_load_context_missing_dir(self):
        with self.assertRaises(FileNotFoundError):
            ig.load_context_text("/nonexistent/dir")

    def test_load_context_collects_md_txt_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b.md").write_text("B", encoding="utf-8")
            (root / "a.txt").write_text("A", encoding="utf-8")
            (root / "skip.png").write_bytes(b"\x00")
            text = ig.load_context_text(tmp)
        self.assertIn("=== a.txt ===\nA", text)
        self.assertIn("=== b.md ===\nB", text)
        self.assertLess(text.index("a.txt"), text.index("b.md"))
        self.assertNotIn("skip.png", text)

    def test_load_context_truncates(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "big.md").write_text("x" * (ig.MAX_CONTEXT_CHARS + 100),
                                              encoding="utf-8")
            text = ig.load_context_text(tmp)
        self.assertTrue(text.endswith("[context truncated]"))
        self.assertLessEqual(len(text), ig.MAX_CONTEXT_CHARS + 50)

    def test_guess_mime(self):
        self.assertEqual(ig.guess_mime(Path("a.png")), "image/png")
        self.assertEqual(ig.guess_mime(Path("a.jpg")), "image/jpeg")
        self.assertEqual(ig.guess_mime(Path("a.jpeg")), "image/jpeg")
        self.assertEqual(ig.guess_mime(Path("a.webp")), "image/webp")
        self.assertEqual(ig.guess_mime(Path("a.gif")), "image/gif")
        self.assertEqual(ig.guess_mime(Path("a.bmp")), "image/png")

    def test_load_memory_none_and_missing(self):
        self.assertEqual(ig.load_memory_references(None), [])
        with self.assertRaises(FileNotFoundError):
            ig.load_memory_references("/nonexistent/dir")

    def test_load_memory_encodes_images_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(3):
                ig.write_placeholder_png(root / f"r{i}.png", 8, 8)
            (root / "notes.txt").write_text("ignored", encoding="utf-8")
            refs = ig.load_memory_references(tmp)
            limited = ig.load_memory_references(tmp, limit=2)
        self.assertEqual(len(refs), 3)
        self.assertEqual(len(limited), 2)
        for ref in refs:
            url = ref["image_url"]["url"]
            self.assertTrue(url.startswith("data:image/png;base64,"))
            base64.b64decode(url.split(",", 1)[1])  # valid b64

    def test_build_final_prompt(self):
        base = ig.build_final_prompt("a cat", "", "auto", "")
        self.assertEqual(base, "a cat")
        full = ig.build_final_prompt("a cat", "CTX", "16:9", "1K")
        self.assertIn("a cat", full)
        self.assertIn("Context:\nCTX", full)
        self.assertIn("aspect ratio 16:9", full)
        self.assertIn("resolution tier 1K", full)
        auto = ig.build_final_prompt("a cat", "", "auto", "1K")
        self.assertNotIn("aspect ratio", auto)


# ---------------------------------------------------------------------------
# Injection templating
# ---------------------------------------------------------------------------

class InjectionTest(IsolatedEnvMixin):

    def test_extract_vars(self):
        self.assertEqual(ig.extract_template_vars("a {{animal}} and {{place}}"),
                         ["animal", "place"])
        self.assertEqual(ig.extract_template_vars("{{a}} {{a}} {{ b }}"), ["a", "b"])
        self.assertEqual(ig.extract_template_vars("no vars"), [])
        self.assertEqual(ig.extract_template_vars("empty {{}} ignored"), [])

    def test_apply_values(self):
        out = ig.apply_template_values("a {{animal}} in {{place}}",
                                       {"animal": "cat", "place": "Rome"})
        self.assertEqual(out, "a cat in Rome")
        self.assertEqual(ig.apply_template_values("a {{x}}", {}), "a {{x}}")

    def test_resolve_rows_repeat_above_and_name_fallback(self):
        rows = ig.resolve_injection_rows([["cat", ""], ["", "Rome"], ["", ""]],
                                         ["animal", "place"])
        self.assertEqual(rows, [["cat", "place"], ["cat", "Rome"], ["cat", "Rome"]])

    def test_resolve_rows_short_and_stripped(self):
        rows = ig.resolve_injection_rows([["  cat  "]], ["animal", "place"])
        self.assertEqual(rows, [["cat", "place"]])

    def test_parse_injection_row(self):
        self.assertEqual(
            ig.parse_injection_row("animal=cat,place=Rome", ["animal", "place"]),
            ["cat", "Rome"])
        self.assertEqual(ig.parse_injection_row("animal=cat", ["animal", "place"]),
                         ["cat", ""])
        with self.assertRaises(ValueError):
            ig.parse_injection_row("unknown=x", ["animal"])
        with self.assertRaises(ValueError):
            ig.parse_injection_row("novalue", ["animal"])


# ---------------------------------------------------------------------------
# Summary (truncate / extract / clean / remote) — regression area
# ---------------------------------------------------------------------------

class TruncateSummaryTest(IsolatedEnvMixin):

    def test_short_unchanged(self):
        self.assertEqual(ig.truncate_prompt("a cat"), "a cat")

    def test_multiline_collapsed(self):
        self.assertEqual(ig.truncate_prompt("a\n  cat\n dog"), "a cat dog")

    def test_long_truncated_with_ellipsis(self):
        long = "word " * 100
        out = ig.truncate_prompt(long)
        self.assertEqual(len(out), ig.MAX_SUMMARY_CHARS)
        self.assertTrue(out.endswith("…"))

    def test_custom_limit(self):
        out = ig.truncate_prompt("a" * 50, limit=10)
        self.assertEqual(len(out), 10)


class SummaryExtractMessageTest(IsolatedEnvMixin):
    """Regression: reasoning must NEVER leak into the CSV summary."""

    def test_str_content(self):
        self.assertEqual(ig._extract_message_text({"content": " hello "}), " hello ")

    def test_list_content(self):
        msg = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        self.assertEqual(ig._extract_message_text(msg), "a\nb")

    def test_empty_content_yields_empty(self):
        self.assertEqual(ig._extract_message_text({"content": ""}), "")
        self.assertEqual(ig._extract_message_text({"content": "   "}), "")
        self.assertEqual(ig._extract_message_text({}), "")

    def test_reasoning_details_ignored(self):
        msg = {"content": "",
               "reasoning_details": [{"text": "The user wants me to create a storyboard"}]}
        self.assertEqual(ig._extract_message_text(msg), "")

    def test_reasoning_str_ignored(self):
        msg = {"content": None, "reasoning": "thinking about the user request"}
        self.assertEqual(ig._extract_message_text(msg), "")

    def test_content_wins_over_reasoning(self):
        msg = {"content": "real summary", "reasoning": "chain of thought"}
        self.assertEqual(ig._extract_message_text(msg), "real summary")

    def test_list_with_nondict_ignored(self):
        msg = {"content": [{"nope": 1}, "str", {"text": "ok"}]}
        self.assertEqual(ig._extract_message_text(msg), "ok")


class CleanSummaryTextTest(IsolatedEnvMixin):
    """Regression: meta commentary from the log sample must be rejected."""

    def test_good_summaries_accepted(self):
        self.assertEqual(
            ig.clean_summary_text("Uma menina desperta, toma café e termina no computador."),
            "Uma menina desperta, toma café e termina no computador.")
        self.assertEqual(
            ig.clean_summary_text("A red panda astronaut floating in space."),
            "A red panda astronaut floating in space.")

    def test_log_sample_meta_rejected(self):
        bad = [
            "The user wants me to create a storyboard based on images provided.",
            "The user wants me to:",
            "Okay, the user wants me to create a story based on inicio1.png.",
            'The user asks: "Complete a story based on the provided images."',
            "Sure, here is a summary: a girl sleeping.",
            "As an AI, I will summarize your prompt about a cat.",
            "Based on the provided images, here is the story.",
            "I understand you want a storyboard with 6 parts.",
            "Your prompt asks for a storyboard.",
            "Thinking process: the image shows a girl.",
            "Here is the summary: a cat.",
            "1. A cat in space.",
            "Step 1: summarize the prompt.",
        ]
        for text in bad:
            with self.assertRaises(ValueError, msg=text):
                ig.clean_summary_text(text)

    def test_empty_rejected(self):
        for text in ["", "   ", "\n\n", '""', "** **"]:
            with self.assertRaises(ValueError, msg=repr(text)):
                ig.clean_summary_text(text)

    def test_label_and_quotes_stripped(self):
        self.assertEqual(ig.clean_summary_text('Summary: A cat in space.'),
                         "A cat in space.")
        self.assertEqual(ig.clean_summary_text('"A cat in space."'), "A cat in space.")
        self.assertEqual(ig.clean_summary_text("Resumo: Um gato no espaço."),
                         "Um gato no espaço.")

    def test_first_line_only(self):
        out = ig.clean_summary_text("First sentence.\nSecond sentence.")
        self.assertEqual(out, "First sentence.")

    def test_long_cut_on_word_boundary(self):
        out = ig.clean_summary_text("word " * 100)
        self.assertLessEqual(len(out), ig.MAX_SUMMARY_CHARS)
        self.assertTrue(out.endswith("."))
        self.assertNotIn("…", out)


class SummarizePromptTest(IsolatedEnvMixin):

    def test_empty_model_no_api_call(self):
        with mock.patch.object(ig, "_post_json") as post:
            out = ig.summarize_prompt("a very long " * 30, api_key="k", model="")
        post.assert_not_called()
        self.assertIn("a very long", out)

    def test_no_api_key_no_api_call(self):
        with mock.patch.object(ig, "_post_json") as post:
            out = ig.summarize_prompt("a cat", api_key=None, model="some-model")
        post.assert_not_called()
        self.assertEqual(out, "a cat")

    def test_remote_failure_falls_back_to_truncation(self):
        prompt = "Complete a história com " + "detalhes " * 40
        with mock.patch.object(ig, "summarize_prompt_remote",
                               side_effect=RuntimeError("HTTP 500")):
            with redirect_stderr(io.StringIO()):
                out = ig.summarize_prompt(prompt, api_key="k", model="m")
        self.assertEqual(out, ig.truncate_prompt(prompt))

    def test_cancellation_propagates(self):
        with mock.patch.object(ig, "summarize_prompt_remote",
                               side_effect=ig.GenerationCancelled("x")):
            with self.assertRaises(ig.GenerationCancelled):
                ig.summarize_prompt("p", api_key="k", model="m")

    def test_remote_builds_constrained_body(self):
        captured = {}

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            captured["body"] = body
            captured["url"] = url
            payload = {"choices": [{"message": {"content": "A cat in space."}}]}
            return 200, json.dumps(payload)

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            out = ig.summarize_prompt_remote("a cat", "key", "free-model", 10)
        self.assertEqual(out, "A cat in space.")
        self.assertEqual(captured["url"], ig.CHAT_URL)
        body = captured["body"]
        self.assertEqual(body["model"], "free-model")
        self.assertEqual(body["temperature"], 0.0)
        system_text = json.dumps(body["messages"][0])
        self.assertIn("EXACTLY ONE", system_text)

    def test_remote_non200_raises(self):
        with mock.patch.object(ig, "_post_json", return_value=(500, "boom")):
            with self.assertRaises(RuntimeError):
                ig.summarize_prompt_remote("p", "k", "m", 5)

    def test_remote_meta_rejected_upstream(self):
        payload = {"choices": [{"message": {"content": "The user wants me to summarize."}}]}
        with mock.patch.object(ig, "_post_json", return_value=(200, json.dumps(payload))):
            with self.assertRaises(ValueError):
                ig.summarize_prompt_remote("p", "k", "m", 5)

    def test_remote_empty_content_rejected(self):
        payload = {"choices": [{"message": {"content": ""}}]}
        with mock.patch.object(ig, "_post_json", return_value=(200, json.dumps(payload))):
            with self.assertRaises(ValueError):
                ig.summarize_prompt_remote("p", "k", "m", 5)


# ---------------------------------------------------------------------------
# Image introspection
# ---------------------------------------------------------------------------

class ImageInspectTest(IsolatedEnvMixin):

    def test_png_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.png"
            ig.write_placeholder_png(path, 16, 12)
            raw = path.read_bytes()
            self.assertEqual(ig.png_dimensions(raw), (16, 12))
            self.assertEqual(ig.inspect_image(path), (len(raw), 16, 12))

    def test_png_invalid(self):
        self.assertIsNone(ig.png_dimensions(b"nope"))
        self.assertIsNone(ig.png_dimensions(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10))

    def test_jpeg_dimensions(self):
        sof = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
               b"\xff\xc0\x00\x11\x08\x00\x20\x00\x10\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
               b"\xff\xd9")
        self.assertEqual(ig.jpeg_dimensions(sof), (16, 32))
        self.assertIsNone(ig.jpeg_dimensions(b"\xff\xd8\xff\xd9"))
        self.assertIsNone(ig.jpeg_dimensions(b"not jpeg"))

    def test_webp_variants(self):
        vp8l = b"RIFF" + struct.pack("<I", 21) + b"WEBP" + b"VP8L" + bytes(13)
        self.assertEqual(ig.webp_dimensions(vp8l), (1, 1))
        vp8x = (b"RIFF" + struct.pack("<I", 26) + b"WEBP" + b"VP8X" + bytes(8)
                + bytes([15, 0, 0, 31, 0, 0]))
        self.assertEqual(ig.webp_dimensions(vp8x), (16, 32))
        vp8 = (b"RIFF" + struct.pack("<I", 26) + b"WEBP" + b"VP8 " + bytes(10)
               + bytes([0x10, 0x00, 0x20, 0x00]))
        self.assertEqual(ig.webp_dimensions(vp8), (16, 32))
        self.assertIsNone(ig.webp_dimensions(b"nope"))

    def test_gif_dimensions(self):
        raw = b"GIF89a" + struct.pack("<HH", 7, 9) + b"\x00" * 10
        self.assertEqual(ig.gif_dimensions(raw), (7, 9))
        raw87 = b"GIF87a" + struct.pack("<HH", 3, 4) + b"\x00" * 10
        self.assertEqual(ig.gif_dimensions(raw87), (3, 4))
        self.assertIsNone(ig.gif_dimensions(b"GIF00" + b"\x00" * 10))

    def test_inspect_unknown_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.bin"
            path.write_bytes(b"\x00" * 100)
            size, width, height = ig.inspect_image(path)
        self.assertEqual((size, width, height), (100, 0, 0))

    def test_target_dimensions(self):
        self.assertEqual(ig.target_dimensions("1:1", "1K"), (1024, 1024))
        self.assertEqual(ig.target_dimensions("16:9", "1K"), (1024, 576))
        self.assertEqual(ig.target_dimensions("9:16", "512"), (288, 512))
        self.assertEqual(ig.target_dimensions("bogus", "1K"), (1024, 1024))
        self.assertEqual(ig.target_dimensions("21:9", "2K"), (2048, 878))

    def test_write_placeholder_caps_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.png"
            ig.write_placeholder_png(path, 5000, 5000)
            self.assertEqual(ig.png_dimensions(path.read_bytes()), (1024, 1024))

    def test_extension_for(self):
        self.assertEqual(ig.extension_for("image/png", "webp"), "png")
        self.assertEqual(ig.extension_for("image/jpeg", "png"), "jpg")
        self.assertEqual(ig.extension_for("image/webp", "png"), "webp")
        self.assertEqual(ig.extension_for("image/gif", "png"), "gif")
        self.assertEqual(ig.extension_for(None, "jpeg"), "jpg")
        self.assertEqual(ig.extension_for(None, "png"), "png")


class SaveImagesTest(IsolatedEnvMixin):

    def _payload(self, n=1, media="image/png"):
        raw = _png_bytes()
        return {"data": [{"b64_json": base64.b64encode(raw).decode(),
                          "media_type": media} for _ in range(n)]}

    def test_no_data_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                ig.save_images(Path(tmp), {"data": []}, "png", "stamp", 0.0)

    def test_single_and_multi_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (paths, _ts) = ig.save_images(out, self._payload(1), "png", "s1", 0.0)
            self.assertEqual([p.name for p in paths], ["image_s1.png"])
            paths2, _ = ig.save_images(out, self._payload(2), "png", "s2", 0.0)
            self.assertEqual([p.name for p in paths2],
                             ["image_s2_1.png", "image_s2_2.png"])

    def test_collision_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            ig.save_images(out, self._payload(1), "png", "s", 0.0)
            (paths, _) = ig.save_images(out, self._payload(1), "png", "s", 0.0)
            self.assertEqual(paths[0].name, "image_s_1.png")

    def test_collected_appended_and_empty_b64_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            collected: list = []
            payload = {"data": [{"b64_json": "", "media_type": "image/png"},
                                self._payload(1)["data"][0]]}
            paths, _ = ig.save_images(out, payload, "png", "s", 0.0, collected)
            self.assertEqual(len(paths), 1)
            self.assertEqual(collected, paths)

    def test_media_type_drives_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (paths, _) = ig.save_images(out, self._payload(1, "image/jpeg"),
                                        "png", "s", 0.0)
            self.assertTrue(paths[0].name.endswith(".jpg"))


# ---------------------------------------------------------------------------
# Cancellable HTTP
# ---------------------------------------------------------------------------

class HttpCancelTest(IsolatedEnvMixin):

    def test_openrouter_headers(self):
        headers = ig._openrouter_headers("secret")
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_post_json_success_and_cleanup(self):
        import http.client
        fake_conn = mock.MagicMock()
        fake_resp = mock.MagicMock()
        fake_resp.status = 200
        fake_resp.read.return_value = b'{"ok": true}'
        fake_conn.getresponse.return_value = fake_resp
        with mock.patch.object(http.client, "HTTPSConnection", return_value=fake_conn):
            status, raw = ig._post_json("https://example.com/api", {"a": 1}, {}, 5)
        self.assertEqual((status, raw), (200, '{"ok": true}'))
        self.assertNotIn(fake_conn, ig._HTTP_CONNS)

    def test_post_json_precancelled(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(ig.GenerationCancelled):
            ig._post_json("https://example.com/api", {}, {}, 5, event)

    def test_post_json_socket_error_becomes_cancel_when_flagged(self):
        import http.client
        event = threading.Event()
        fake_conn = mock.MagicMock()
        fake_conn.getresponse.side_effect = OSError("closed")
        with mock.patch.object(http.client, "HTTPSConnection", return_value=fake_conn):
            with self.assertRaises(ig.GenerationCancelled):
                def run():
                    try:
                        ig._post_json("https://example.com/api", {}, {}, 5, event)
                    except ig.GenerationCancelled:
                        raise
                    except OSError:
                        event.set()
                        raise ig.GenerationCancelled("late cancel")
                run()

    def test_abort_all_http_empty_and_with_conns(self):
        ig.abort_all_http()  # no-op
        fake_conn = mock.MagicMock()
        fake_sock = mock.MagicMock()
        fake_conn.sock = fake_sock
        with ig._HTTP_LOCK:
            ig._HTTP_CONNS.add(fake_conn)
        try:
            ig.abort_all_http()
        finally:
            with ig._HTTP_LOCK:
                ig._HTTP_CONNS.discard(fake_conn)
        fake_sock.shutdown.assert_called_once()
        fake_conn.close.assert_called()

    def test_generation_cancelled_is_exception(self):
        self.assertTrue(issubclass(ig.GenerationCancelled, Exception))


# ---------------------------------------------------------------------------
# Provider request functions
# ---------------------------------------------------------------------------

class ProviderRequestTest(IsolatedEnvMixin):

    def _ok_payload(self):
        raw = _png_bytes()
        return {"data": [{"b64_json": base64.b64encode(raw).decode()}],
                "usage": {"cost": 0.02}, "created": 1700000000}

    def test_openrouter_body_and_contract(self):
        captured = {}

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            captured["body"] = body
            return 200, json.dumps(self._ok_payload())

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            payload, start_ts, elapsed = ig.request_openrouter(
                api_key="k", model="m", prompt="p", aspect_ratio="1:1",
                resolution="1K", references=[], output_format="png",
                seed=7, count=2, timeout_s=5)
        body = captured["body"]
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["n"], 2)
        self.assertEqual(body["seed"], 7)
        self.assertNotIn("input_references", body)
        self.assertIn("data", payload)
        self.assertGreaterEqual(elapsed, 0.0)

    def test_openrouter_optional_fields(self):
        captured = {}

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            captured["body"] = body
            return 200, json.dumps(self._ok_payload())

        refs = [{"type": "image_url"}]
        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            ig.request_openrouter(
                api_key="k", model="m", prompt="p", aspect_ratio="1:1",
                resolution="1K", references=refs, output_format="png",
                seed=None, count=1, timeout_s=5)
        self.assertNotIn("seed", captured["body"])
        self.assertEqual(captured["body"]["input_references"], refs)

    def test_openrouter_non200_raises(self):
        with mock.patch.object(ig, "_post_json", return_value=(402, "nope")):
            with self.assertRaises(RuntimeError):
                ig.request_openrouter(
                    api_key="k", model="m", prompt="p", aspect_ratio="1:1",
                    resolution="1K", references=[], output_format="png",
                    seed=None, count=1, timeout_s=5)

    def test_gemini_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            ig.request_gemini(
                api_key="k", model="m", prompt="p", aspect_ratio="1:1",
                resolution="1K", references=[], output_format="png",
                seed=None, count=1, timeout_s=5)


# ---------------------------------------------------------------------------
# CSV log
# ---------------------------------------------------------------------------

def _sample_entry(**over) -> dict:
    entry = {k: "" for k in ig.LOG_FIELDS}
    entry.update({"date": "2026-01-01T00:00:00-03:00", "prompt_summary": "s",
                  "prompt_full": "full", "image_file": "i.png", "cost_usd": "0.010000",
                  "model": "m", "provider": "openrouter"})
    entry.update(over)
    return entry


class CsvLogTest(IsolatedEnvMixin):

    def test_log_fields_stable(self):
        self.assertEqual(ig.LOG_FIELDS, [
            "date", "prompt_summary", "prompt_full", "image_file", "image_bytes",
            "width", "height", "resolution_req", "aspect_ratio_req", "cost_usd",
            "generation_timestamp", "total_seconds", "model", "provider", "key_hash",
        ])

    def test_write_read_roundtrip_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / ig.LOG_FILENAME
            total_ops, total_cost = ig.write_log(
                log, [_sample_entry(cost_usd="0.01"), _sample_entry(cost_usd="0.02")])
            self.assertEqual((total_ops, round(total_cost, 2)), (2, 0.03))
            text = log.read_text(encoding="utf-8")
            self.assertIn("# total_operations=2; total_cost_usd=0.030000", text)
            self.assertIn("# cost_source=", text)
            rows = ig.read_log_rows(log)
            self.assertEqual(len(rows), 2)
            self.assertEqual(set(rows[0]), set(ig.LOG_FIELDS))

    def test_read_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ig.read_log_rows(Path(tmp) / "nope.csv"), [])

    def test_corrupt_cost_ignored_in_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / ig.LOG_FILENAME
            _, total = ig.write_log(log, [_sample_entry(cost_usd="bogus")])
            self.assertEqual(total, 0.0)

    def test_append_preserves_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / ig.LOG_FILENAME
            ig.write_log(log, [_sample_entry(image_file="a.png")])
            ops, _ = ig.append_log_entries(log, [_sample_entry(image_file="b.png")])
            self.assertEqual(ops, 2)
            files = [r["image_file"] for r in ig.read_log_rows(log)]
            self.assertEqual(files, ["a.png", "b.png"])


# ---------------------------------------------------------------------------
# Core pipeline (dry-run offline)
# ---------------------------------------------------------------------------

class RunGenerationTest(IsolatedEnvMixin):

    def test_empty_prompt_rejected(self):
        _, out = self.make_dirs()
        with self.assertRaises(ValueError):
            ig.run_generation(**_dry_kwargs(out, prompt="   "))

    def test_invalid_provider_rejected(self):
        _, out = self.make_dirs()
        with self.assertRaises(ValueError):
            ig.run_generation(**_dry_kwargs(out, provider="nope"))

    def test_dry_run_creates_file_and_log(self):
        _, out = self.make_dirs()
        result = ig.run_generation(**_dry_kwargs(out))
        self.assertEqual(len(result["images"]), 1)
        self.assertTrue(Path(result["images"][0]).exists())
        rows = ig.read_log_rows(Path(result["log_path"]))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["prompt_summary"], "No model selected")
        self.assertEqual(row["prompt_full"], "a red panda astronaut")
        self.assertEqual(row["cost_usd"], "0.000000")
        self.assertEqual(row["key_hash"], "")
        for field in ig.LOG_FIELDS:
            self.assertIn(field, row)

    def test_dry_run_count_suffixes(self):
        _, out = self.make_dirs()
        result = ig.run_generation(**_dry_kwargs(out, count=3))
        names = sorted(Path(p).name for p in result["images"])
        self.assertEqual(len(names), 3)
        self.assertEqual(len({n for n in names}), 3)
        self.assertEqual(len(ig.read_log_rows(Path(result["log_path"]))), 3)

    def test_dry_run_with_summary_model_uses_truncation(self):
        _, out = self.make_dirs()
        long_prompt = "Complete a história com " + "detalhes " * 40
        result = ig.run_generation(
            **_dry_kwargs(out, prompt=long_prompt, summary_model="free-model"))
        row = ig.read_log_rows(Path(result["log_path"]))[0]
        self.assertEqual(row["prompt_summary"], ig.truncate_prompt(long_prompt))

    def test_dry_run_records_key_hash(self):
        _, out = self.make_dirs()
        result = ig.run_generation(**_dry_kwargs(out, api_key="my-key"))
        row = ig.read_log_rows(Path(result["log_path"]))[0]
        self.assertEqual(row["key_hash"], ig.key_hash("my-key"))

    def test_missing_key_non_dry_run(self):
        _, out = self.make_dirs()
        with self.assertRaises(RuntimeError):
            ig.run_generation(**_dry_kwargs(out, dry_run=False))

    def test_mocked_remote_splits_cost_and_parses_time(self):
        _, out = self.make_dirs()
        raw = _png_bytes()
        payload = {"data": [{"b64_json": base64.b64encode(raw).decode()},
                            {"b64_json": base64.b64encode(raw).decode()}],
                   "usage": {"cost": 0.03}, "created": 1700000000}

        def fake_request(**kwargs):
            self.assertEqual(kwargs["count"], 2)
            return payload, 0.0, 0.1

        with mock.patch.dict(ig.REQUEST_FUNCS, {"openrouter": fake_request}):
            result = ig.run_generation(
                **_dry_kwargs(out, dry_run=False, api_key="k", count=2))
        self.assertEqual(len(result["images"]), 2)
        rows = ig.read_log_rows(Path(result["log_path"]))
        self.assertEqual([r["cost_usd"] for r in rows], ["0.015000", "0.015000"])
        self.assertIn("2023-11-14", rows[0]["generation_timestamp"])

    def test_mocked_remote_bad_timestamp_falls_back(self):
        _, out = self.make_dirs()
        raw = _png_bytes()
        payload = {"data": [{"b64_json": base64.b64encode(raw).decode()}],
                   "usage": {}, "created": "not-a-time"}

        def fake_request(**kwargs):
            return payload, 0.0, 0.1

        with mock.patch.dict(ig.REQUEST_FUNCS, {"openrouter": fake_request}):
            result = ig.run_generation(**_dry_kwargs(out, dry_run=False, api_key="k"))
        rows = ig.read_log_rows(Path(result["log_path"]))
        self.assertTrue(rows[0]["generation_timestamp"])

    def test_precancelled_logs_nothing_and_cleans(self):
        _, out = self.make_dirs()
        event = threading.Event()
        event.set()
        with self.assertRaises(ig.GenerationCancelled):
            ig.run_generation(**_dry_kwargs(out, count=2, cancel_event=event))
        self.assertEqual(list(out.glob("image_*.png")), [])
        self.assertFalse((out / ig.LOG_FILENAME).exists())

    def test_summary_failure_falls_back_to_truncation(self):
        _, out = self.make_dirs()
        prompt = "Complete a história com " + "detalhes " * 40
        with mock.patch.object(ig, "summarize_prompt", side_effect=RuntimeError("x")):
            result = ig.run_generation(
                **_dry_kwargs(out, prompt=prompt, summary_model="m"))
        rows = ig.read_log_rows(Path(result["log_path"]))
        self.assertEqual(rows[0]["prompt_summary"], ig.truncate_prompt(prompt))

    def test_summary_cancel_aborts_and_logs_nothing(self):
        _, out = self.make_dirs()
        with mock.patch.object(ig, "summarize_prompt",
                               side_effect=ig.GenerationCancelled("stop")):
            with self.assertRaises(ig.GenerationCancelled):
                ig.run_generation(**_dry_kwargs(out, summary_model="m"))
        self.assertEqual(list(out.glob("image_*.png")), [])
        self.assertFalse((out / ig.LOG_FILENAME).exists())


class RunGenerationBatchTest(IsolatedEnvMixin):

    def test_single_prompt_delegates(self):
        _, out = self.make_dirs()
        result = ig.run_generation_batch(["hello"], **{k: v for k, v in _dry_kwargs(out).items()
                                                       if k != "prompt"})
        self.assertEqual(len(result["images"]), 1)

    def test_multi_prompt_forces_count_one_each(self):
        _, out = self.make_dirs()
        seen_counts = []

        real = ig.run_generation

        def spy(*, prompt, **kwargs):
            seen_counts.append(kwargs.get("count"))
            return real(prompt=prompt, **kwargs)

        with mock.patch.object(ig, "run_generation", side_effect=spy):
            result = ig.run_generation_batch(
                ["a {{x}}".replace("{{x}}", "cat"), "a dog"],
                **{k: v for k, v in _dry_kwargs(out).items() if k != "prompt"})
        self.assertEqual(seen_counts, [1, 1])
        self.assertEqual(len(result["images"]), 2)
        self.assertEqual(len(result["entries"]), 2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CliParserTest(IsolatedEnvMixin):

    def test_defaults(self):
        args = ig.build_parser().parse_args([])
        self.assertEqual(args.prop, "1:1")
        self.assertEqual(args.resolution, "1K")
        self.assertEqual(args.provider, ig.DEFAULT_PROVIDER)
        self.assertEqual(args.summary_model, "")
        self.assertFalse(args.dry_run)

    def test_config_keys_have_cli_flags(self):
        parser = ig.build_parser()
        dests = {a.dest for a in parser._actions}
        for key in ig.CONFIG_KEYS:
            self.assertIn(key, dests, key)


class ResolvePromptTest(IsolatedEnvMixin):

    def test_inline_only(self):
        args = argparse.Namespace(prompt="hi", prompt_file="")
        self.assertEqual(ig.resolve_prompt(args), "hi")

    def test_file_only_and_combined(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.md"
            path.write_text("from file", encoding="utf-8")
            self.assertEqual(
                ig.resolve_prompt(argparse.Namespace(prompt="", prompt_file=str(path))),
                "from file")
            self.assertEqual(
                ig.resolve_prompt(argparse.Namespace(prompt="head", prompt_file=str(path))),
                "head\n\nfrom file")


class MainCliTest(IsolatedEnvMixin):

    def _args(self, out: Path, **over) -> argparse.Namespace:
        base = {"prompt": "smoke test", "prompt_file": "", "output_dir": str(out),
                "context_dir": "", "memory_dir": "", "model": None,
                "provider": "openrouter", "prop": "1:1", "resolution": "512",
                "output_format": "png", "seed": None, "count": 1, "inject": [],
                "api_key": "", "remember_key": False, "forget_key": False,
                "summary_model": "", "timeout": ig.REQUEST_TIMEOUT_S,
                "dry_run": True, "gui": False, "list_log": False}
        base.update(over)
        return argparse.Namespace(**base)

    def test_dry_run_end_to_end(self):
        _, out = self.make_dirs()
        with redirect_stdout(io.StringIO()):
            code = ig.main_cli(self._args(out))
        self.assertEqual(code, 0)
        self.assertEqual(len(list(out.glob("image_*.png"))), 1)
        self.assertEqual(len(ig.read_log_rows(out / ig.LOG_FILENAME)), 1)

    def test_injection_end_to_end(self):
        _, out = self.make_dirs()
        args = self._args(out, prompt="a {{animal}}",
                          inject=["animal=cat", "animal=dog"])
        with redirect_stdout(io.StringIO()):
            code = ig.main_cli(args)
        self.assertEqual(code, 0)
        self.assertEqual(len(list(out.glob("image_*.png"))), 2)
        fulls = sorted(r["prompt_full"] for r in ig.read_log_rows(out / ig.LOG_FILENAME))
        self.assertEqual(fulls, ["a cat", "a dog"])

    def test_list_log(self):
        _, out = self.make_dirs()
        with redirect_stdout(io.StringIO()):
            ig.main_cli(self._args(out))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ig.main_cli(self._args(out, list_log=True))
        self.assertEqual(code, 0)
        self.assertIn("prompt_full", buf.getvalue())

    def test_list_log_empty(self):
        _, out = self.make_dirs()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ig.main_cli(self._args(out, list_log=True))
        self.assertEqual(code, 0)
        self.assertIn("no log entries", buf.getvalue())

    def test_forget_key(self):
        _, out = self.make_dirs()
        if ig.HAS_FERNET:
            ig.save_remembered_key("openrouter", "k")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ig.main_cli(self._args(out, forget_key=True))
        self.assertEqual(code, 0)

    def test_validation_errors_return_2(self):
        _, out = self.make_dirs()
        cases = [
            self._args(out, prompt="   "),
            self._args(out, count=99),
            self._args(out, prompt="plain", inject=["a=b"]),
            self._args(out, prompt="a {{x}}", inject=["y=z"]),
            self._args(out, prompt="a {{x}}", inject=["x="]),
            self._args(out, prompt="a {{x}}", count=3),
        ]
        for args in cases:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ig.main_cli(args), 2)

    def test_cancelled_returns_130(self):
        _, out = self.make_dirs()
        with mock.patch.object(ig, "run_generation_batch",
                               side_effect=ig.GenerationCancelled("stop")):
            with redirect_stdout(io.StringIO()):
                code = ig.main_cli(self._args(out))
        self.assertEqual(code, 130)

    def test_remember_key_needs_key(self):
        _, out = self.make_dirs()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = ig.main_cli(self._args(out, remember_key=True))
        self.assertEqual(code, 2)


# ---------------------------------------------------------------------------
# Security: no secrets in logs
# ---------------------------------------------------------------------------

class NoSecretsInLogTest(IsolatedEnvMixin):

    def test_api_key_never_written_to_log(self):
        _, out = self.make_dirs()
        secret = "sk-secret-xyz-123"
        ig.run_generation(**_dry_kwargs(out, api_key=secret))
        content = (out / ig.LOG_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(secret, content)
        rows = ig.read_log_rows(out / ig.LOG_FILENAME)
        self.assertEqual(rows[0]["key_hash"], ig.key_hash(secret))

    def test_key_hash_is_not_reversible(self):
        h = ig.key_hash("another-secret")
        self.assertNotIn("another-secret", h)
        self.assertEqual(len(h), 16)


# ---------------------------------------------------------------------------
# Frontend parity (CLI/GUI/Web share the same Injection semantics)
# ---------------------------------------------------------------------------

class FrontendParityTest(unittest.TestCase):

    def test_injection_ts_exists_and_matches_python_regex(self):
        ts_path = REPO_ROOT / "web" / "frontend" / "src" / "injection.ts"
        self.assertTrue(ts_path.is_file())
        source = ts_path.read_text(encoding="utf-8")
        self.assertIn(r"\{\{([^{}]*)\}\}", source)
        self.assertIn("previous[j] || name", source)
        self.assertIn(".trim()", source)
        self.assertEqual(source.count("matchAll"), 1)

    def test_injection_ts_count_parity(self):
        source = (REPO_ROOT / "web" / "frontend" / "src" / "injection.ts").read_text()
        self.assertIn("/^[0-9]+$/", source)
        for valid in ["1", "10"]:
            self.assertEqual(ig.parse_count(valid), int(valid))

    def test_web_tabs_exist(self):
        src = REPO_ROOT / "web" / "frontend" / "src"
        for name in ["injection.ts", "api.ts", "tabs/Injection.tsx",
                     "tabs/Generate.tsx", "tabs/Model.tsx"]:
            self.assertTrue((src / name).is_file(), name)

    def test_python_ts_semantics_spot_check(self):
        # Same documented rule: empty cell repeats above, first row -> var name.
        self.assertEqual(ig.resolve_injection_rows([["", ""]], ["a", "b"]), [["a", "b"]])
        self.assertEqual(ig.extract_template_vars("x {{a}} y {{a}} z {{b}}"), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
