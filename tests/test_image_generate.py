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
        for var in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
            self._had_env[var] = var in os.environ
            self._saved_env[var] = os.environ.get(var)
            os.environ.pop(var, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._had_xdg and self._old_xdg is not None:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg
        else:
            os.environ.pop("XDG_CONFIG_HOME", None)
        for var in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
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
        self.assertEqual(ig.normalize_provider("OpenAI"), "openai")

    def test_normalize_invalid(self):
        for bad in ["", "  ", "has space", "UPPER SPACE", "../x", "a_b", "prov!"]:
            with self.assertRaises(ValueError, msg=bad):
                ig.normalize_provider(bad)

    def test_normalize_unknown(self):
        with self.assertRaises(ValueError):
            ig.normalize_provider("dallex")

    def test_normalize_unknown_lists_supported(self):
        with self.assertRaises(ValueError) as ctx:
            ig.normalize_provider("anthropic")
        self.assertIn("suportados", str(ctx.exception))

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
        self.assertEqual(ig.parse_count(30), 30)
        self.assertEqual(ig.parse_count(" 3 "), 3)

    def test_invalid(self):
        for bad in [0, 31, -1, "0", "31", "abc", "", None, "1.5", "  "]:
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
            # Live leak from nvidia/nemotron-3-super (thinking in content field):
            "We need to output a single sentence, max 150 characters, in same "
            "language as image prompt (Portuguese). Summarize the story.",
            "Let me summarize this image prompt for the log.",
            "The task is to summarize the prompt above.",
            "The prompt describes a storyboard with 6 parts.",
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
        # User-only shape: no system role (free shared-pool providers
        # return null content when a system message is present).
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertIn("a cat", body["messages"][0]["content"])
        # Thinking budget: reasoning models share one token budget between
        # thinking and answer; uncapped thinking eats the whole max_tokens
        # and the API returns null/thinking-only content (proven live vs
        # nvidia/nemotron-3-super with budget 64 -> clean answer).
        self.assertEqual(body["max_tokens"], ig._SUMMARY_MAX_TOKENS)
        self.assertEqual(body["reasoning"], {"max_tokens": ig._SUMMARY_REASONING_BUDGET})

    def test_summary_instruction_matches_prompt_language(self):
        pt = ig._summary_instruction("Complete a história da menina dormindo.")
        self.assertIn("Sem conversação", pt)
        self.assertIn("Complete a história", pt)
        en = ig._summary_instruction("A red panda astronaut floating in space.")
        self.assertIn("No conversation", en)
        self.assertIn("red panda", en)

    def test_summary_instruction_is_single_user_message(self):
        for prompt in ["a cat", "uma gata dormindo no sofá"]:
            text = ig._summary_instruction(prompt)
            self.assertIn(prompt, text)
            self.assertNotIn("system", text.lower())

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

    def test_remote_null_content_rejected(self):
        payload = {"choices": [{"message": {"content": None}}]}
        with mock.patch.object(ig, "_post_json", return_value=(200, json.dumps(payload))):
            with self.assertRaises(ValueError):
                ig.summarize_prompt_remote("p", "k", "m", 5)

    def test_remote_refusal_rejected(self):
        payload = {"choices": [{"message": {"content": "A cat.", "refusal": "policy"}}]}
        with mock.patch.object(ig, "_post_json", return_value=(200, json.dumps(payload))):
            with self.assertRaises(ValueError):
                ig.summarize_prompt_remote("p", "k", "m", 5)

    def test_remote_thinking_in_content_rejected(self):
        # Live shape from nvidia/nemotron-3-super: thinking leaked into content.
        payload = {"choices": [{"message": {
            "content": "We need to output a single sentence about the storyboard.",
            "reasoning": "plan...", "refusal": None}}]}
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

    def setUp(self):
        super().setUp()
        # Keep capability discovery offline: unknown model -> caps None.
        self._caps_fetch = mock.patch.object(
            ig, "_fetch_json", return_value=(404, "not found"))
        self._caps_fetch.start()
        self.addCleanup(self._caps_fetch.stop)
        ig._CAPS_CACHE.clear()
        self.addCleanup(ig._CAPS_CACHE.clear)

    def _ok_payload(self, n=1):
        raw = _png_bytes()
        return {"data": [{"b64_json": base64.b64encode(raw).decode()}
                         for _ in range(n)],
                "usage": {"cost": 0.02}, "created": 1700000000}

    def test_openrouter_body_and_contract(self):
        captured = {}

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            captured["body"] = body
            captured["calls"] = captured.get("calls", 0) + 1
            return 200, json.dumps(self._ok_payload(n=body.get("n", 1)))

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            payload, start_ts, elapsed = ig.request_openrouter(
                api_key="k", model="m", prompt="p", aspect_ratio="1:1",
                resolution="1K", references=[], output_format="png",
                seed=7, count=2, timeout_s=5)
        body = captured["body"]
        self.assertEqual(captured["calls"], 1)  # provider honored n=2
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["n"], 2)
        self.assertEqual(body["seed"], 7)
        self.assertNotIn("input_references", body)
        self.assertIn("data", payload)
        self.assertEqual(payload["seeds"], [7, 7])
        self.assertGreaterEqual(elapsed, 0.0)

    def _zod_n_too_big(self, maximum=10) -> str:
        issues = json.dumps([{"origin": "number", "code": "too_big", "maximum": maximum,
                              "inclusive": True, "path": ["n"],
                              "message": f"Too big: expected number to be <={maximum}"}],
                            indent=2)
        return json.dumps({"success": False,
                           "error": {"name": "ZodError", "message": issues}})

    def test_n_limit_from_error(self):
        self.assertEqual(ig._n_limit_from_error(
            "OpenRouter HTTP 400: " + self._zod_n_too_big(10)), 10)
        self.assertEqual(ig._n_limit_from_error(self._zod_n_too_big(4)), 4)
        self.assertIsNone(ig._n_limit_from_error("OpenRouter HTTP 400: bad seed"))
        self.assertIsNone(ig._n_limit_from_error(
            self._zod_n_too_big().replace('\\"n\\"', '\\"seed\\"')))

    def test_openrouter_count_above_api_max_is_split(self):
        # n=24 with unknown caps: calls of <= 10 (10 + 10 + 4), seeds per call.
        ns = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            ns.append(body["n"])
            if body["n"] > ig.OPENROUTER_MAX_N:
                return 400, self._zod_n_too_big()
            return 200, json.dumps(self._ok_payload(n=body["n"]))

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            payload, _ts, _el = ig.request_openrouter(
                api_key="k", model="m", prompt="p", aspect_ratio="1:1",
                resolution="1K", references=[], output_format="png",
                seed=5, count=24, timeout_s=5)
        self.assertEqual(ns, [10, 10, 4])
        self.assertEqual(len(payload["data"]), 24)
        self.assertEqual(payload["seeds"], [5] * 10 + [6] * 10 + [7] * 4)

    def test_openrouter_retries_with_router_n_limit(self):
        # Router lowers its limit below our constant: parse it and retry once.
        ns = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            ns.append(body["n"])
            if body["n"] > 4:
                return 400, self._zod_n_too_big(4)
            return 200, json.dumps(self._ok_payload(n=body["n"]))

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            payload, _ts, _el = ig.request_openrouter(
                api_key="k", model="m", prompt="p", aspect_ratio="1:1",
                resolution="1K", references=[], output_format="png",
                seed=None, count=9, timeout_s=5)
        self.assertEqual(ns, [9, 4, 4, 1])
        self.assertEqual(len(payload["data"]), 9)

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

    def test_openrouter_content_policy_raises_friendly(self):
        raw = json.dumps({"error": {"message": "The response was filtered due to "
                                      "the prompt triggering our content "
                                      "management policy.", "code": 400,
                                     "metadata": {"provider_name": "Meta"}}})
        with mock.patch.object(ig, "_post_json", return_value=(400, raw)):
            with self.assertRaises(ig.ContentPolicyError) as ctx:
                ig.request_openrouter(
                    api_key="k", model="meta/muse-image", prompt="um gato",
                    aspect_ratio="1:1", resolution="1K", references=[],
                    output_format="png", seed=None, count=1, timeout_s=5)
        message = str(ctx.exception)
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertIn("filtro de conteudo", message)
        self.assertIn("um gato", message)
        self.assertIn("Context dir", message)
        self.assertIn("Meta", message)

    def test_openrouter_caps_fetch_failure_keeps_body(self):
        # _fetch_json patched in setUp returns 404 -> caps None -> body unchanged.
        with mock.patch.object(ig, "_post_json",
                               return_value=(200, json.dumps(self._ok_payload()))):
            payload, _ts, _el = ig.request_openrouter(
                api_key="k", model="m", prompt="p", aspect_ratio="21:9",
                resolution="1K", references=[], output_format="webp",
                seed=7, count=2, timeout_s=5)
        self.assertIn("data", payload)

    def test_openrouter_adapts_body_to_model_capabilities(self):
        # Regression: flux.2-klein-4b rejects resolution/n>1/webp (HTTP 400).
        endpoints = {"endpoints": [{
            "provider_name": "Black Forest Labs",
            "supported_parameters": {
                "aspect_ratio": {"type": "enum",
                                 "values": ["1:1", "4:3", "16:9", "21:9", "auto"]},
                "output_format": {"type": "enum", "values": ["png", "jpeg"]},
                "n": {"type": "range", "min": 1, "max": 1},
                "input_references": {"type": "range", "min": 0, "max": 4},
                "seed": {"type": "boolean"},
            },
        }]}
        bodies: list[dict] = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            bodies.append(body)
            return 200, json.dumps(self._ok_payload())

        with mock.patch.object(ig, "_fetch_json",
                               return_value=(200, json.dumps(endpoints))):
            with mock.patch.object(ig, "_post_json", side_effect=fake_post):
                payload, _ts, _el = ig.request_openrouter(
                    api_key="k", model="black-forest-labs/flux.2-klein-4b",
                    prompt="p", aspect_ratio="21:9", resolution="1K",
                    references=[{"type": "image_url"}] * 6,
                    output_format="webp", seed=7, count=3, timeout_s=5)
        # n=1-only provider: one request per image, payloads merged.
        self.assertEqual(len(bodies), 3)
        for index, body in enumerate(bodies):
            self.assertNotIn("resolution", body)  # unsupported -> dropped
            self.assertEqual(body["aspect_ratio"], "21:9")  # supported -> kept
            self.assertEqual(body["output_format"], "png")  # webp -> first enum
            self.assertEqual(body["n"], 1)
            self.assertEqual(len(body["input_references"]), 4)  # clamped to max
            self.assertEqual(body["seed"], 7 + index)  # seed varied per call
        self.assertEqual(len(payload["data"]), 3)
        self.assertEqual(payload["seeds"], [7, 8, 9])  # effective seeds logged

    def test_openrouter_caps_cached_across_calls(self):
        endpoints = {"endpoints": [{"supported_parameters": {
            "n": {"type": "range", "min": 1, "max": 2}}}]}

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            return 200, json.dumps(self._ok_payload())

        with mock.patch.object(ig, "_fetch_json",
                               return_value=(200, json.dumps(endpoints))) as fetch:
            with mock.patch.object(ig, "_post_json", side_effect=fake_post):
                ig.request_openrouter(
                    api_key="k", model="cached/model", prompt="p",
                    aspect_ratio="1:1", resolution="1K", references=[],
                    output_format="png", seed=None, count=2, timeout_s=5)
                ig.request_openrouter(
                    api_key="k", model="cached/model", prompt="p",
                    aspect_ratio="1:1", resolution="1K", references=[],
                    output_format="png", seed=None, count=2, timeout_s=5)
        self.assertEqual(fetch.call_count, 1)

    def test_closest_ratio_mapping(self):
        values = ["1:1", "4:3", "16:9", "21:9", "auto"]
        self.assertEqual(ig._closest_ratio("16:9", values), "16:9")
        self.assertEqual(ig._closest_ratio("9:16", values), "1:1")  # log-closest
        self.assertIsNone(ig._closest_ratio("auto", ["1:1", "16:9"]))

    def test_openrouter_resolution_value_validated_against_enum(self):
        endpoints = {"endpoints": [{"supported_parameters": {
            "resolution": {"type": "enum", "values": ["1K", "2K", "4K"]},
            "n": {"type": "range", "min": 1, "max": 10}}}]}
        bodies: list[dict] = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            bodies.append(body)
            return 200, json.dumps(self._ok_payload())

        with mock.patch.object(ig, "_fetch_json",
                               return_value=(200, json.dumps(endpoints))):
            with mock.patch.object(ig, "_post_json", side_effect=fake_post):
                ig.request_openrouter(
                    api_key="k", model="tiered/model", prompt="p",
                    aspect_ratio="1:1", resolution="512", references=[],
                    output_format="png", seed=None, count=1, timeout_s=5)
        self.assertNotIn("resolution", bodies[0])  # 512 not in enum -> dropped

    def test_openrouter_capabilities_intersect_across_endpoints(self):
        endpoints = {"endpoints": [
            {"supported_parameters": {
                "resolution": {"type": "enum", "values": ["1K", "2K"]},
                "n": {"type": "range", "min": 1, "max": 4},
                "seed": {"type": "boolean"}}},
            {"supported_parameters": {
                "resolution": {"type": "enum", "values": ["2K", "4K"]},
                "n": {"type": "range", "min": 1, "max": 2}}},
        ]}
        with mock.patch.object(ig, "_fetch_json",
                               return_value=(200, json.dumps(endpoints))):
            caps = ig._model_capabilities("multi/ep", "k")
        self.assertIsNotNone(caps)
        assert caps is not None
        self.assertEqual(caps["resolution"], {"type": "enum", "values": ["2K"]})
        self.assertEqual(caps["n"], {"type": "range", "min": 1, "max": 2})
        self.assertNotIn("seed", caps)

    def test_openrouter_reactive_retry_on_capability_mismatch(self):
        mismatch = json.dumps({"error": {
            "message": "No provider for m supports the requested parameter(s): "
                       'resolution "1K"',
            "code": 400,
            "metadata": {"failed_routing_step": "Filter by Image Capabilities"}}})
        endpoints = {"endpoints": [{"supported_parameters": {
            "aspect_ratio": {"type": "enum", "values": ["1:1"]},
            "n": {"type": "range", "min": 1, "max": 1}}}]}
        bodies: list[dict] = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            bodies.append(body)
            if len(bodies) == 1:
                return 400, mismatch
            return 200, json.dumps(self._ok_payload())

        fetches = [(404, "not found"), (200, json.dumps(endpoints))]
        with mock.patch.object(ig, "_fetch_json", side_effect=fetches):
            with mock.patch.object(ig, "_post_json", side_effect=fake_post):
                payload, _ts, _el = ig.request_openrouter(
                    api_key="k", model="flaky/model", prompt="p",
                    aspect_ratio="1:1", resolution="1K", references=[],
                    output_format="png", seed=None, count=1, timeout_s=5)
        self.assertEqual(len(bodies), 2)
        self.assertIn("resolution", bodies[0])  # first attempt: full body
        self.assertNotIn("resolution", bodies[1])  # retry: adapted body
        self.assertIn("data", payload)

    def test_openrouter_retry_gives_up_when_caps_missing(self):
        mismatch = json.dumps({"error": {
            "message": "No provider for m supports the requested parameter(s)",
            "code": 400}})

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            return 400, mismatch

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            with self.assertRaises(RuntimeError):
                ig.request_openrouter(
                    api_key="k", model="m", prompt="p", aspect_ratio="1:1",
                    resolution="1K", references=[], output_format="png",
                    seed=None, count=1, timeout_s=5)

    def test_build_final_prompt_skips_supported_params(self):
        caps = {"aspect_ratio": {"type": "enum", "values": ["1:1"]},
                "resolution": {"type": "enum", "values": ["1K"]}}
        # Supported params travel via API: no redundant text hint.
        out = ig.build_final_prompt("a cat", "", "1:1", "1K", caps)
        self.assertEqual(out, "a cat")
        out = ig.build_final_prompt("a cat", "", "16:9", "2K", caps)
        self.assertEqual(out, "a cat")
        # Unsupported/unknown: hint kept as fallback.
        out = ig.build_final_prompt("a cat", "", "16:9", "2K", {})
        self.assertIn("aspect ratio 16:9", out)
        self.assertIn("resolution tier 2K", out)
        out = ig.build_final_prompt("a cat", "", "1:1", "1K")
        self.assertIn("aspect ratio 1:1", out)


class OpenaiRequestTest(IsolatedEnvMixin):

    def setUp(self):
        super().setUp()
        ig._CAPS_CACHE.clear()
        self.addCleanup(ig._CAPS_CACHE.clear)

    def _b64_payload(self, n=1, created=1700000000):
        raw = _png_bytes()
        return {"created": created,
                "data": [{"b64_json": base64.b64encode(raw).decode()}
                         for _ in range(n)]}

    def _call(self, **over):
        params = {"api_key": "k", "model": "gpt-image-1", "prompt": "p",
                  "aspect_ratio": "16:9", "resolution": "1K", "references": [],
                  "output_format": "png", "seed": None, "count": 1,
                  "timeout_s": 5}
        params.update(over)
        return ig.request_openai(**params)

    def test_count_above_api_max_is_split(self):
        ns = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            ns.append(body["n"])
            return 200, json.dumps(self._b64_payload(n=body["n"]))

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            payload, _ts, _el = self._call(count=24)
        self.assertEqual(ns, [10, 10, 4])
        self.assertEqual(len(payload["data"]), 24)

    def test_gpt_image_body(self):
        bodies: list[dict] = []
        urls: list[str] = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            bodies.append(body)
            urls.append(url)
            return 200, json.dumps(self._b64_payload(n=2))

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            payload, _ts, _el = self._call(count=2)
        self.assertTrue(urls[0].endswith("/v1/images/generations"))
        self.assertEqual(bodies[0]["size"], "1536x1024")  # 16:9 landscape
        self.assertEqual(bodies[0]["quality"], "medium")  # 1K tier
        self.assertEqual(bodies[0]["n"], 2)
        self.assertEqual(bodies[0]["output_format"], "png")
        self.assertNotIn("response_format", bodies[0])
        self.assertEqual(len(payload["data"]), 2)
        self.assertEqual(payload["seeds"], [None, None])

    def test_dalle3_single_only_loops(self):
        bodies: list[dict] = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            bodies.append(body)
            return 200, json.dumps(self._b64_payload())

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            payload, _ts, _el = self._call(model="dall-e-3", count=3,
                                           aspect_ratio="9:16")
        self.assertEqual(len(bodies), 3)
        for body in bodies:
            self.assertEqual(body["n"], 1)
            self.assertEqual(body["size"], "1024x1792")  # portrait
            self.assertEqual(body["response_format"], "b64_json")
        self.assertEqual(len(payload["data"]), 3)

    def test_dalle2_size_and_quality_defaults(self):
        bodies: list[dict] = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            bodies.append(body)
            return 200, json.dumps(self._b64_payload())

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            self._call(model="dall-e-2", aspect_ratio="16:9")
        self.assertEqual(bodies[0]["size"], "1024x1024")  # square only
        self.assertNotIn("quality", bodies[0])

    def test_references_rejected_informatively(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._call(references=[{"type": "image_url"}])
        self.assertIn("não aceita imagens de referência", str(ctx.exception))

    def test_unknown_model_prefix_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._call(model="flux-fake-1")
        self.assertIn("não parece ser do provider", str(ctx.exception))

    def test_moderation_block_becomes_content_policy_error(self):
        raw = json.dumps({"error": {"code": "moderation_blocked",
                                    "message": "blocked", "type": "error"}})
        with mock.patch.object(ig, "_post_json", return_value=(400, raw)):
            with self.assertRaises(ig.ContentPolicyError) as ctx:
                self._call()
        self.assertIn("OpenAI", str(ctx.exception))


class GeminiRequestTest(IsolatedEnvMixin):

    def setUp(self):
        super().setUp()
        ig._CAPS_CACHE.clear()
        self.addCleanup(ig._CAPS_CACHE.clear)

    def _b64_payload(self):
        raw = _png_bytes()
        return {"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png",
                            "data": base64.b64encode(raw).decode()}}]}}]}

    def _call(self, **over):
        params = {"api_key": "k", "model": "gemini-2.5-flash-image",
                  "prompt": "p", "aspect_ratio": "21:9", "resolution": "2K",
                  "references": [], "output_format": "png", "seed": None,
                  "count": 1, "timeout_s": 5}
        params.update(over)
        return ig.request_gemini(**params)

    def test_generate_content_body_and_parse(self):
        bodies: list[dict] = []
        urls: list[str] = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            bodies.append(body)
            urls.append(url)
            return 200, json.dumps(self._b64_payload())

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            payload, _ts, _el = self._call(count=2)
        self.assertIn(":generateContent", urls[0])
        # generateContent yields one image per call -> fan-out.
        self.assertEqual(len(bodies), 2)
        config = bodies[0]["generationConfig"]
        self.assertEqual(config["responseModalities"], ["TEXT", "IMAGE"])
        self.assertEqual(config["imageConfig"]["aspectRatio"], "16:9")  # closest
        self.assertEqual(config["imageConfig"]["imageSize"], "2K")
        self.assertEqual(payload["data"][0]["media_type"], "image/png")
        self.assertEqual(len(payload["data"]), 2)

    def test_references_become_inline_data(self):
        raw = _png_bytes()
        url = f"data:image/png;base64,{base64.b64encode(raw).decode()}"
        bodies: list[dict] = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            bodies.append(body)
            return 200, json.dumps(self._b64_payload())

        refs = [{"type": "image_url", "image_url": {"url": url}}]
        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            self._call(references=refs)
        parts = bodies[0]["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": "p"})
        self.assertEqual(parts[1]["inlineData"]["mimeType"], "image/png")

    def test_imagen_predict_uses_seed_and_count(self):
        bodies: list[dict] = []
        urls: list[str] = []
        raw = _png_bytes()

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            bodies.append(body)
            urls.append(url)
            n = body["parameters"]["sampleCount"]
            return 200, json.dumps({"predictions": [
                {"bytesBase64Encoded": base64.b64encode(raw).decode(),
                 "mimeType": "image/png"} for _ in range(n)]})

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            payload, _ts, _el = self._call(model="imagen-4.0-generate-001",
                                           seed=5, count=2)
        self.assertIn(":predict", urls[0])
        self.assertEqual(len(bodies), 1)  # sampleCount covers count=2
        self.assertEqual(bodies[0]["parameters"]["sampleCount"], 2)
        self.assertEqual(bodies[0]["parameters"]["seed"], 5)
        self.assertEqual(len(payload["data"]), 2)

    def test_imagen_references_rejected_informatively(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._call(model="imagen-4.0-generate-001",
                       references=[{"type": "image_url"}])
        self.assertIn("não aceita imagens de referência", str(ctx.exception))

    def test_safety_block_becomes_content_policy_error(self):
        raw = json.dumps({"promptFeedback": {"blockReason": "SAFETY"}})
        with mock.patch.object(ig, "_post_json", return_value=(200, raw)):
            with self.assertRaises(ig.ContentPolicyError) as ctx:
                self._call()
        self.assertIn("Google", str(ctx.exception))

    def test_unknown_family_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._call(model="something-else-1")
        self.assertIn("não parece ser do provider", str(ctx.exception))


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
            "width", "height", "resolution_req", "aspect_ratio_req", "seed", "cost_usd",
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

    def test_dry_run_records_requested_seed(self):
        _, out = self.make_dirs()
        result = ig.run_generation(**_dry_kwargs(out, seed=42, count=2))
        rows = ig.read_log_rows(Path(result["log_path"]))
        self.assertEqual([r["seed"] for r in rows], ["42", "42"])

    def test_dry_run_empty_seed_logged_blank(self):
        _, out = self.make_dirs()
        result = ig.run_generation(**_dry_kwargs(out, seed=None))
        row = ig.read_log_rows(Path(result["log_path"]))[0]
        self.assertEqual(row["seed"], "")

    def test_mocked_remote_logs_effective_seeds_per_image(self):
        _, out = self.make_dirs()
        raw = _png_bytes()
        payload = {"data": [{"b64_json": base64.b64encode(raw).decode()}
                            for _ in range(3)],
                   "usage": {"cost": 0.03}, "created": 1700000000,
                   "seeds": [7, 8, 9]}

        def fake_request(**kwargs):
            return payload, 0.0, 0.1

        with mock.patch.dict(ig.REQUEST_FUNCS, {"openrouter": fake_request}):
            result = ig.run_generation(
                **_dry_kwargs(out, dry_run=False, api_key="k", count=3,
                              seed=7))
        rows = ig.read_log_rows(Path(result["log_path"]))
        self.assertEqual([r["seed"] for r in rows], ["7", "8", "9"])

    def test_mocked_remote_without_seeds_falls_back_to_base(self):
        _, out = self.make_dirs()
        raw = _png_bytes()
        payload = {"data": [{"b64_json": base64.b64encode(raw).decode()}],
                   "usage": {}, "created": 1700000000}

        def fake_request(**kwargs):
            return payload, 0.0, 0.1

        with mock.patch.dict(ig.REQUEST_FUNCS, {"openrouter": fake_request}):
            result = ig.run_generation(
                **_dry_kwargs(out, dry_run=False, api_key="k", seed=11))
        rows = ig.read_log_rows(Path(result["log_path"]))
        self.assertEqual(rows[0]["seed"], "11")

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


class LogOrderProgressTest(IsolatedEnvMixin):

    def _row(self, date: str, name: str) -> dict:
        row = {k: "" for k in ig.LOG_FIELDS}
        row.update(date=date, image_file=name)
        return row

    def test_sort_newest_first(self):
        rows = [self._row("2026-09-28T10:00:00-03:00", "a"),
                self._row("2026-09-28T12:00:00-03:00", "b"),
                self._row("2026-09-28T11:00:00-03:00", "c"),
                self._row("2026-09-28T12:00:00-03:00", "d"),  # tie: later row on top
                self._row("garbage", "e")]
        got = [r["image_file"] for r in ig.sort_log_rows_newest_first(rows)]
        self.assertEqual(got, ["d", "b", "c", "a", "e"])

    def test_sort_mixed_offsets(self):
        rows = [self._row("2026-09-28T12:00:00+00:00", "utc_noon"),
                self._row("2026-09-28T10:00:00-03:00", "brt_10")]  # = 13:00 UTC
        got = [r["image_file"] for r in ig.sort_log_rows_newest_first(rows)]
        self.assertEqual(got, ["brt_10", "utc_noon"])

    def test_collect_merges_and_dedups(self):
        tmp, out = self.make_dirs()
        sub = out / "1"
        sub.mkdir()
        ig.write_log(out / ig.LOG_FILENAME, [self._row("2026-09-28T10:00:00-03:00", "old")])
        ig.write_log(sub / ig.LOG_FILENAME, [self._row("2026-09-28T11:00:00-03:00", "new")])
        got = ig.collect_log_rows([out / ig.LOG_FILENAME, sub / ig.LOG_FILENAME,
                                   out / ig.LOG_FILENAME, out / "missing" / ig.LOG_FILENAME])
        self.assertEqual([r["image_file"] for r in got], ["new", "old"])

    def test_on_progress_called_per_generation_after_logging(self):
        _, out = self.make_dirs()
        calls = []

        def on_progress(info):
            rows = ig.collect_log_rows([Path(p) for p in info["log_paths"]])
            calls.append((info["done"], info["total"], len(info["images"]), len(rows)))

        kwargs = {k: v for k, v in _dry_kwargs(out, count=3).items() if k != "prompt"}
        ig.run_generation_batch(["hi"], dynamic_dirs={"output_dir": "3"},
                                on_progress=on_progress, **kwargs)
        self.assertEqual(calls, [(1, 3, 1, 1), (2, 3, 2, 2), (3, 3, 3, 3)])
        calls.clear()
        ig.run_generation_batch(["a cat", "a dog"], on_progress=on_progress, **kwargs)
        self.assertEqual([c[:2] for c in calls], [(1, 2), (2, 2)])

    def test_on_progress_not_forwarded_to_single_run(self):
        _, out = self.make_dirs()
        kwargs = {k: v for k, v in _dry_kwargs(out).items() if k != "prompt"}
        result = ig.run_generation_batch(["hi"], on_progress=lambda info: None, **kwargs)
        self.assertEqual(len(result["images"]), 1)


class DynamicDirsTest(IsolatedEnvMixin):

    def _base(self, root: Path, *names: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        for name in names:
            (root / name).mkdir(parents=True)
        return root

    def test_parse_dynamic_range(self):
        self.assertEqual(ig.parse_dynamic_range("2"), 2)
        self.assertEqual(ig.parse_dynamic_range(" 10 "), 10)
        with self.assertRaisesRegex(ValueError, "empty"):
            ig.parse_dynamic_range("")
        for bad in ["0", "1", "-3", "abc", "1.5", None]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                ig.parse_dynamic_range(bad)

    def test_dynamic_dir_for_cycles(self):
        got = [Path(ig.dynamic_dir_for("/a/b", 5, i)).name for i in range(10)]
        self.assertEqual(got, ["1", "2", "3", "4", "5", "1", "2", "3", "4", "5"])

    def test_dynamic_start(self):
        self.assertEqual(ig.parse_dynamic_start(""), 1)
        self.assertEqual(ig.parse_dynamic_start(None), 1)
        self.assertEqual(ig.parse_dynamic_start(" 4 "), 4)
        for bad in ["0", "-1", "x", "1.5"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                ig.parse_dynamic_start(bad)
        self.assertEqual(ig.parse_dynamic_spec("5"), (1, 5, 1))
        self.assertEqual(ig.parse_dynamic_spec({"start": "", "range": "5"}), (1, 5, 1))
        self.assertEqual(ig.parse_dynamic_spec({"start": "2", "range": "12"}), (2, 12, 1))
        for bad in ({"start": "5", "range": "5"}, {"start": "6", "range": "5"},
                    {"start": "2", "range": ""}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ig.parse_dynamic_spec(bad)
        got = [Path(ig.dynamic_dir_for("/a", 5, i, 2)).name for i in range(6)]
        self.assertEqual(got, ["2", "3", "4", "5", "2", "3"])

    def test_batch_resume_from_start(self):
        # memory Start 2, Range 12, n=11: folders 2..12, memory/1 untouched.
        tmp, out = self.make_dirs()
        mem = self._base(Path(tmp.name) / "mem", *[str(i) for i in range(2, 13)])
        seen = []
        real = ig.run_generation

        def spy(*, prompt, **kwargs):
            seen.append(Path(kwargs["memory_dir"]).name)
            return real(prompt=prompt, **kwargs)

        kwargs = {k: v for k, v in _dry_kwargs(out, count=11, memory_dir=str(mem)).items()
                  if k != "prompt"}
        with mock.patch.object(ig, "run_generation", side_effect=spy):
            ig.run_generation_batch(["hi"], dynamic_dirs={
                "memory_dir": {"start": "2", "range": "12"}}, **kwargs)
        self.assertEqual(seen, [str(i) for i in range(2, 13)])

    def test_check_requires_count_and_base(self):
        tmp, out = self.make_dirs()
        with self.assertRaisesRegex(ValueError, "count > 1"):
            ig.check_dynamic_dirs({"output_dir": "3"}, {"output_dir": str(out)}, 1)
        with self.assertRaisesRegex(ValueError, "base folder"):
            ig.check_dynamic_dirs({"context_dir": "3"}, {"context_dir": ""}, 3)
        with self.assertRaisesRegex(ValueError, "unknown"):
            ig.check_dynamic_dirs({"foo": "3"}, {}, 3)

    def test_batch_user_example_folders(self):
        # batch 3, start 2, range 12: 1-3 -> 2, 4-6 -> 3, 7-9 -> 4, ...
        got = [Path(ig.dynamic_dir_for("/a", 12, i, 2, 3)).name for i in range(12)]
        self.assertEqual(got, ["2"] * 3 + ["3"] * 3 + ["4"] * 3 + ["5"] * 3)
        # past range the cycle restarts at start (batch 2, folders 1..3)
        got = [Path(ig.dynamic_dir_for("/a", 3, i, 1, 2)).name for i in range(8)]
        self.assertEqual(got, ["1", "1", "2", "2", "3", "3", "1", "1"])
        # batch 1 == previous behaviour
        self.assertEqual([Path(ig.dynamic_dir_for("/a", 5, i, 2)).name for i in range(5)],
                         ["2", "3", "4", "5", "2"])

    def test_parse_dynamic_batch(self):
        self.assertEqual(ig.parse_dynamic_batch(""), 1)
        self.assertEqual(ig.parse_dynamic_batch(None), 1)
        self.assertEqual(ig.parse_dynamic_batch(" 3 "), 3)
        for bad in ["0", "-1", "x", "1.5"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                ig.parse_dynamic_batch(bad)
        self.assertEqual(ig.parse_dynamic_spec({"start": "2", "range": "12", "batch": "3"}),
                         (2, 12, 3))
        with self.assertRaisesRegex(ValueError, "batch"):
            ig.parse_dynamic_spec({"range": "5", "batch": "0"})

    def test_batch_needs_only_the_folders_it_uses(self):
        tmp, _ = self.make_dirs()
        ctx = self._base(Path(tmp.name) / "ctx", "2", "3")
        # 6 generations, batch 3 -> folders 2 and 3 only (range 12 not required)
        self.assertEqual(ig.check_dynamic_dirs({"context_dir": {"start": "2", "range": "12",
                                                                "batch": "3"}},
                                               {"context_dir": str(ctx)}, 6),
                         {"context_dir": (2, 12, 3)})
        with self.assertRaisesRegex(FileNotFoundError, r"ctx/4"):
            ig.check_dynamic_dirs({"context_dir": {"start": "2", "range": "12", "batch": "3"}},
                                  {"context_dir": str(ctx)}, 7)

    def test_batch_run_and_cli(self):
        _, out = self.make_dirs()
        kwargs = {k: v for k, v in _dry_kwargs(out, count=7).items() if k != "prompt"}
        result = ig.run_generation_batch(
            ["hi"], dynamic_dirs={"output_dir": {"start": "2", "range": "12", "batch": "3"}},
            **kwargs)
        self.assertEqual([Path(p).parent.name for p in result["images"]],
                         ["2", "2", "2", "3", "3", "3", "4"])
        _, out2 = self.make_dirs()
        args = MainCliTest._args(self, out2, count=4, dynamic_output="5",
                                 dynamic_output_batch="2")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ig.main_cli(args), 0)
        self.assertEqual(sorted(p.name for p in out2.iterdir() if p.is_dir()), ["1", "2"])
        parsed = ig.build_parser().parse_args(["--dynamic-memory", "9",
                                               "--dynamic-memory-batch", "4"])
        self.assertEqual(parsed.dynamic_memory_batch, "4")

    def test_check_missing_context_subfolders(self):
        tmp, _ = self.make_dirs()
        ctx = self._base(Path(tmp.name) / "ctx", "1", "2")
        # count 2 only needs 1..2 -> ok even with range 10
        self.assertEqual(ig.check_dynamic_dirs({"context_dir": "10"},
                                               {"context_dir": str(ctx)}, 2),
                         {"context_dir": (1, 10, 1)})
        with self.assertRaisesRegex(FileNotFoundError, "3"):
            ig.check_dynamic_dirs({"context_dir": "10"}, {"context_dir": str(ctx)}, 3)

    def test_batch_user_example(self):
        # n=10, context Dynamic range 10, output Dynamic range 5, memory fixed.
        tmp, out = self.make_dirs()
        ctx = self._base(Path(tmp.name) / "ctx", *[str(i) for i in range(1, 11)])
        for i in range(1, 11):
            (ctx / str(i) / "c.md").write_text(f"ctx {i}", encoding="utf-8")
        mem = self._base(Path(tmp.name) / "mem")
        seen = []
        real = ig.run_generation

        def spy(*, prompt, **kwargs):
            seen.append((kwargs["count"], kwargs["seed"], Path(kwargs["context_dir"]).name,
                         Path(kwargs["output_dir"]).name, kwargs["memory_dir"]))
            return real(prompt=prompt, **kwargs)

        kwargs = {k: v for k, v in _dry_kwargs(out, count=10, seed=7, context_dir=str(ctx),
                                               memory_dir=str(mem)).items() if k != "prompt"}
        with mock.patch.object(ig, "run_generation", side_effect=spy):
            result = ig.run_generation_batch(
                ["hello"], dynamic_dirs={"context_dir": "10", "output_dir": "5"}, **kwargs)
        self.assertEqual([s[2] for s in seen], [str(i) for i in range(1, 11)])
        self.assertEqual([s[3] for s in seen], ["1", "2", "3", "4", "5"] * 2)
        self.assertEqual({s[4] for s in seen}, {str(mem)})
        self.assertEqual({s[0] for s in seen}, {1})
        self.assertEqual([s[1] for s in seen], list(range(7, 17)))
        self.assertEqual(len(result["images"]), 10)
        self.assertEqual(len(result["log_paths"]), 5)
        for i in range(1, 6):
            self.assertEqual(len(list((out / str(i)).glob("image_*.png"))), 2)
            self.assertEqual(len(ig.read_log_rows(out / str(i) / ig.LOG_FILENAME)), 2)

    def test_batch_missing_subfolder_runs_nothing(self):
        tmp, out = self.make_dirs()
        ctx = self._base(Path(tmp.name) / "ctx", "1")
        kwargs = {k: v for k, v in _dry_kwargs(out, count=3, context_dir=str(ctx)).items()
                  if k != "prompt"}
        with mock.patch.object(ig, "run_generation") as run:
            with self.assertRaises(FileNotFoundError):
                ig.run_generation_batch(["hi"], dynamic_dirs={"context_dir": "3"}, **kwargs)
        run.assert_not_called()

    def test_nearest_existing_dir(self):
        tmp, out = self.make_dirs()
        self.assertEqual(ig.nearest_existing_dir(str(out)), out)
        self.assertEqual(ig.nearest_existing_dir(str(out / "x" / "y")), out)
        self.assertIsNone(ig.nearest_existing_dir(""))
        self.assertIsNone(ig.nearest_existing_dir(None))

    def test_cli_dynamic_output(self):
        _, out = self.make_dirs()
        args = MainCliTest._args(self, out, count=3, dynamic_output="2")
        with redirect_stdout(io.StringIO()):
            code = ig.main_cli(args)
        self.assertEqual(code, 0)
        self.assertEqual(len(list((out / "1").glob("image_*.png"))), 2)
        self.assertEqual(len(list((out / "2").glob("image_*.png"))), 1)

    def test_cli_dynamic_errors(self):
        _, out = self.make_dirs()
        for over in ({"count": 1, "dynamic_output": "2"},
                     {"count": 3, "dynamic_output": ""},
                     {"count": 3, "dynamic_context": "2"}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = ig.main_cli(MainCliTest._args(self, out, **over))
            self.assertEqual(code, 2, over)

    def test_parser_flags(self):
        args = ig.build_parser().parse_args(["--dynamic-output", "5", "--dynamic-memory", "3",
                                             "--dynamic-memory-start", "2"])
        self.assertEqual((args.dynamic_output, args.dynamic_context, args.dynamic_memory),
                         ("5", None, "3"))
        self.assertEqual((args.dynamic_output_start, args.dynamic_memory_start), (None, "2"))

    def test_cli_dynamic_output_start(self):
        _, out = self.make_dirs()
        args = MainCliTest._args(self, out, count=3, dynamic_output="4",
                                 dynamic_output_start="3")
        with redirect_stdout(io.StringIO()):
            code = ig.main_cli(args)
        self.assertEqual(code, 0)
        self.assertEqual(len(list((out / "3").glob("image_*.png"))), 2)
        self.assertEqual(len(list((out / "4").glob("image_*.png"))), 1)
        self.assertFalse((out / "1").exists())
        # start without range -> range empty error
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = ig.main_cli(MainCliTest._args(self, out, count=3, dynamic_output_start="2"))
        self.assertEqual(code, 2)


class AnalyseTest(IsolatedEnvMixin):
    """Analyse tab core: aligned rows, one pick per row, Choose copy + report."""

    def folder(self, name: str, count: int, start: float = 1_700_000_000) -> Path:
        tmp, _ = self.make_dirs()
        folder = Path(tmp.name) / name
        folder.mkdir()
        for i in range(count):  # file i is newer than file i-1
            path = folder / f"img_{i}.png"
            ig.write_placeholder_png(path, 20 + i, 10)
            os.utime(path, (start + i * 10, start + i * 10))
        (folder / "notes.txt").write_text("not an image")
        return folder

    def test_rows_newest_and_oldest_first(self):
        a, b = self.folder("A", 3), self.folder("B", 2)
        rows = ig.build_analysis_rows([a, b])
        self.assertEqual([[c.name if c else None for c in r] for r in rows],
                         [["img_2.png", "img_1.png"], ["img_1.png", "img_0.png"],
                          ["img_0.png", None]])
        rows = ig.build_analysis_rows([a, b], newest_first=False)
        self.assertEqual([[c.name if c else None for c in r] for r in rows],
                         [["img_0.png", "img_0.png"], ["img_1.png", "img_1.png"],
                          ["img_2.png", None]])
        with self.assertRaises(FileNotFoundError):
            ig.build_analysis_rows([a, a / "missing"])

    def test_choose_copies_and_reports(self):
        a, b = self.folder("A", 3), self.folder("B", 2)
        ig.write_log(b / ig.LOG_FILENAME, [{**{k: "" for k in ig.LOG_FIELDS},
                                            "image_file": "img_1.png", "prompt_full": "a cat",
                                            "model": "m/x", "seed": "7", "date": "2026-09-28"}])
        tmp, _ = self.make_dirs()
        result = ig.choose_images([a, b], {0: 1, 2: 0}, Path(tmp.name) / "chosen")
        dest = Path(result["folder"])
        self.assertEqual(dest.parent, Path(tmp.name) / "chosen")
        self.assertEqual(sorted(p.name for p in dest.iterdir()),
                         ["1_B_img_1.png", "3_A_img_0.png", "report.csv", "report.md"])
        self.assertTrue((b / "img_1.png").is_file())  # originals untouched
        rows = list(csv.DictReader((dest / "report.csv").open(encoding="utf-8")))
        self.assertEqual([r["row"] for r in rows], ["1", "3"])
        self.assertEqual(rows[0]["prompt_full"], "a cat")
        self.assertEqual(rows[0]["seed"], "7")
        self.assertEqual(rows[0]["alternatives"], "A/img_2.png")
        self.assertEqual(rows[1]["alternatives"], "")  # B has no 3rd image
        md = (dest / "report.md").read_text(encoding="utf-8")
        self.assertIn("Rows compared: 3; chosen: 2; rows without a choice: 1", md)
        self.assertIn("| 2 | `" + str(b) + "` | 2 | 1 | 50% |", md)
        self.assertIn("- Prompt: a cat", md)
        self.assertIn("No generation info", md)  # A has no log
        again = ig.choose_images([a, b], {0: 0}, Path(tmp.name) / "chosen")
        self.assertNotEqual(again["folder"], result["folder"])  # never overwrites

    def test_report_finds_log_in_parent_folder(self):
        tmp, _ = self.make_dirs()
        sub = Path(tmp.name) / "generated" / "muse 1"
        sub.mkdir(parents=True)
        ig.write_placeholder_png(sub / "image_1.png", 10, 10)
        ig.write_log(sub.parent / ig.LOG_FILENAME, [{**{k: "" for k in ig.LOG_FIELDS},
                                                    "image_file": "image_1.png",
                                                    "prompt_full": "from parent log"}])
        result = ig.choose_images([sub], {0: 0}, Path(tmp.name) / "chosen")
        self.assertEqual(result["rows"][0]["prompt_full"], "from parent log")

    def test_choose_validation(self):
        a, b = self.folder("A", 2), self.folder("B", 1)
        tmp, _ = self.make_dirs()
        for picks, msg in (({}, "no image selected"), ({1: 1}, "no image in column 2"),
                           ({5: 0}, "out of range"), ({0: 3}, "out of range")):
            with self.assertRaisesRegex(ValueError, msg):
                ig.choose_images([a, b], picks, tmp.name)

    def test_parse_pick(self):
        self.assertEqual(ig.parse_pick("3:2", 2), (2, 1))
        for bad in ("3", "0:1", "1:3", "a:b", "1:0"):
            with self.assertRaises(ValueError, msg=bad):
                ig.parse_pick(bad, 2)

    def test_cli_analyse_and_choose(self):
        a, b = self.folder("A", 2), self.folder("B", 2)
        tmp, _ = self.make_dirs()
        with redirect_stdout(io.StringIO()) as out:
            code = ig.main(["--analyse", str(a), "--analyse", str(b)])
        self.assertEqual(code, 0)
        self.assertIn("  1  img_1.png  |  img_1.png", out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            code = ig.main(["--analyse", str(a), "--analyse", str(b), "--choose", "1:2",
                            "--choose", "2:1", "--oldest-first", "--chosen-dir", tmp.name])
        self.assertEqual(code, 0)
        dest = Path(out.getvalue().split("to ", 1)[1].splitlines()[0])
        self.assertEqual(sorted(p.name for p in dest.glob("*.png")),
                         ["1_B_img_0.png", "2_A_img_1.png"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(ig.main(["--analyse", str(a), "--choose", "1:1",
                                      "--choose", "1:1"]), 2)  # same row twice

    def test_thumbnail_cache(self):
        a = self.folder("A", 1)
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(a / "cache")}):
            thumb = ig.make_thumbnail(a / "img_0.png", size=16)
            if thumb is None:
                self.skipTest("no convert/ffmpeg available")
            self.assertTrue(thumb.is_file())
            self.assertEqual(ig.make_thumbnail(a / "img_0.png", size=16), thumb)  # cached

    def test_preview_geometry(self):
        # 1920x1080 screen, 1920x1280 image: big near the edges, on the wider side
        self.assertEqual(ig.preview_geometry(1920, 1280, 200, 1920, 1080), (1200, "right"))
        self.assertEqual(ig.preview_geometry(1920, 1280, 1700, 1920, 1080), (1200, "left"))
        size, side = ig.preview_geometry(1920, 1280, 960, 1920, 1080)
        self.assertEqual(side, "right")
        self.assertLessEqual(size, 1920 - 960 - ig.PREVIEW_GAP)  # never over the pointer
        self.assertEqual(size % ig.PREVIEW_STEP, 0)
        # never upscaled past the image itself
        self.assertEqual(ig.preview_geometry(300, 200, 100, 1920, 1080), (300, "right"))
        # tall image limited by the screen height minus the caption
        size, _ = ig.preview_geometry(1000, 3000, 100, 1920, 1080)
        self.assertLessEqual(size, 1080 * 0.85 - ig.PREVIEW_CAPTION_H)

    def test_default_chosen_dir_is_next_to_output_dir(self):
        self.assertEqual(ig.default_chosen_dir("/p/generated/muse 2"), "/p/generated/chosen")
        self.assertEqual(Path(ig.default_chosen_dir()).parent,
                         Path(ig.default_output_dir()).parent)

    def test_config_keeps_prompt(self):
        ig.save_gui_config({"prompt": "um {{animal}}\nsegunda linha", "dry_run": True})
        self.assertEqual(ig.sanitize_gui_config(ig.load_gui_config())["prompt"],
                         "um {{animal}}\nsegunda linha")
        self.assertEqual(ig.sanitize_gui_config({"prompt": 3})["prompt"], "")

    def test_config_keeps_analyse_folders(self):
        clean = ig.sanitize_gui_config({"analyse": ["/a", "", 3, "/b"], "chosen_dir": "/c"})
        self.assertEqual((clean["analyse"], clean["chosen_dir"]), (["/a", "/b"], "/c"))
        self.assertEqual(ig.sanitize_gui_config({"analyse": "x"})["analyse"], [])


class TableViewTest(IsolatedEnvMixin):
    """Per-column sort + spreadsheet filters shared by both GUIs and --list-log."""

    ROWS = [{"model": "b/x", "cost_usd": "0.5", "date": "2026-09-28T10:00:00"},
            {"model": "a/y", "cost_usd": "10", "date": "2026-09-27T09:00:00"},
            {"model": "B/z", "cost_usd": "", "date": "2026-09-29T08:00:00"},
            {"model": "a/y", "cost_usd": "2", "date": ""}]

    def test_column_kind(self):
        self.assertEqual(ig.column_kind(["1", "2.5", "", "$3"]), "num")
        self.assertEqual(ig.column_kind(["2026-09-28T10:00", "2026-01-01"]), "date")
        self.assertEqual(ig.column_kind(["abc", "1"]), "text")
        self.assertEqual(ig.column_kind(["", " "]), "text")

    def test_sort_numeric_text_date_and_empties_last(self):
        cost = [r["cost_usd"] for r in ig.apply_table_view(self.ROWS, sort=("cost_usd", False))]
        self.assertEqual(cost, ["0.5", "2", "10", ""])       # numeric, not "10" < "2"
        cost = [r["cost_usd"] for r in ig.apply_table_view(self.ROWS, sort=("cost_usd", True))]
        self.assertEqual(cost, ["10", "2", "0.5", ""])       # empty stays last
        models = [r["model"] for r in ig.apply_table_view(self.ROWS, sort=("model", False))]
        self.assertEqual(models, ["a/y", "a/y", "b/x", "B/z"])  # case-insensitive, stable
        dates = [r["date"][:10] for r in ig.apply_table_view(self.ROWS, sort=("date", True))]
        self.assertEqual(dates, ["2026-09-29", "2026-09-28", "2026-09-27", ""])

    def test_filters_values_contains_and_combined(self):
        only = ig.apply_table_view(self.ROWS, {"model": {"values": {"a/y"}, "contains": ""}})
        self.assertEqual(len(only), 2)
        contains = ig.apply_table_view(self.ROWS, {"model": {"values": None, "contains": "B/"}})
        self.assertEqual([r["model"] for r in contains], ["b/x", "B/z"])
        both = ig.apply_table_view(self.ROWS, {"model": {"values": {"a/y"}, "contains": ""},
                                               "cost_usd": {"values": None, "contains": "2"}})
        self.assertEqual([r["cost_usd"] for r in both], ["2"])
        self.assertEqual(ig.apply_table_view(self.ROWS, {"model": {"values": set(),
                                                                   "contains": ""}}), [])

    def test_parse_sort_and_filters(self):
        self.assertEqual(ig.parse_log_sort("cost_usd:desc"), ("cost_usd", True))
        self.assertEqual(ig.parse_log_sort("model"), ("model", False))
        self.assertIsNone(ig.parse_log_sort(""))
        with self.assertRaises(ValueError):
            ig.parse_log_sort("model:sideways")
        self.assertEqual(ig.parse_log_filters(["model=muse"], ig.LOG_FIELDS),
                         {"model": {"values": None, "contains": "muse"}})
        with self.assertRaisesRegex(ValueError, "unknown column"):
            ig.parse_log_filters(["nope=1"], ig.LOG_FIELDS)
        with self.assertRaisesRegex(ValueError, "COLUMN=TEXT"):
            ig.parse_log_filters(["model"], ig.LOG_FIELDS)

    def test_cli_list_log_filter_sort(self):
        _, out = self.make_dirs()
        rows = [{**{k: "" for k in ig.LOG_FIELDS}, "image_file": f"i{i}.png", "model": m,
                 "cost_usd": c} for i, (m, c) in enumerate([("a", "3"), ("b", "10"), ("a", "1")])]
        ig.write_log(out / ig.LOG_FILENAME, rows)
        with redirect_stdout(io.StringIO()) as buf:
            code = ig.main(["--list-log", "--output-dir", str(out), "--log-filter", "model=a",
                            "--log-sort", "cost_usd:desc"])
        self.assertEqual(code, 0)
        listed = list(csv.DictReader(io.StringIO(buf.getvalue())))
        self.assertEqual([r["image_file"] for r in listed], ["i0.png", "i2.png"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(ig.main(["--list-log", "--output-dir", str(out),
                                      "--log-sort", "x:bad"]), 2)

    def test_config_keeps_log_sort(self):
        self.assertEqual(ig.sanitize_gui_config({"log_sort": "cost_usd:desc"})["log_sort"],
                         "cost_usd:desc")
        self.assertEqual(ig.sanitize_gui_config({"log_sort": "a:sideways"})["log_sort"], "")


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

    def test_summary_tooltips_share_example_and_fallback_note(self):
        # GUI (tk) and web (React) must document the same summary-model
        # guidance: a valid :free example plus the truncation-fallback note.
        core = (REPO_ROOT / "image_generate.py").read_text(encoding="utf-8")
        web = (REPO_ROOT / "web" / "frontend" / "src" / "tabs" / "Model.tsx").read_text(
            encoding="utf-8")
        for source, name in ((core, "tk GUI"), (web, "web Model.tsx")):
            self.assertIn("openrouter/free", source, name)
            self.assertIn("falls back to local truncation", source, name)


if __name__ == "__main__":
    unittest.main()
