"""Offline regression tests for story_generate.py (no network, no audio device).

Run: python3 -m unittest tests.test_story_generate -v
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import image_generate as ig  # noqa: E402
import story_generate as sg  # noqa: E402

VOICE_A = "a" * 32
VOICE_B = "b" * 32


def _wav_bytes(seconds: float = 0.5, rate: int = 8000, channels: int = 1,
               width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(b"\x01\x00" * int(rate * seconds) * channels)
    return buffer.getvalue()


def _chat_payload(content: str, cost: float = 0.001) -> str:
    return json.dumps({"choices": [{"message": {"content": content}}],
                       "usage": {"cost": cost}})


def _story_json(voice: str = VOICE_B, scenes: int = 3) -> str:
    return json.dumps({"title": "Um dia comum", "logline": "Uma manhã.",
                       "scenes": [{"heading": f"h{i}", "narration": f"Cena {i} texto."}
                                  for i in range(scenes)],
                       "voice_id": voice, "voice_reason": "calma"})


class IsolatedMixin(unittest.TestCase):
    """Isolate the vault/config dir and key env vars."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        env["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # never hit the public model catalog from tests (unknown model -> generic)
        catalog = mock.patch.object(sg, "openrouter_models", return_value={})
        catalog.start()
        self.addCleanup(catalog.stop)

    def storyboard(self, name: str = "sb.png") -> Path:
        path = self.tmp / "in" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # size varies with the name so every test image has its own hash
        ig.write_placeholder_png(path, 60 + sum(map(ord, name)) % 50, 40)
        return path


class VoicesTest(IsolatedMixin):

    def test_parse_voice_spec(self):
        self.assertEqual(sg.parse_voice_spec(f"{VOICE_A}=narradora"),
                         {"id": VOICE_A, "label": "narradora"})
        self.assertEqual(sg.parse_voice_spec(VOICE_A)["label"], "")
        for bad in ["", "x", "has space inside id", "../etc"]:
            with self.assertRaises(ValueError, msg=bad):
                sg.parse_voice_spec(bad)

    def test_validate_voices(self):
        self.assertEqual(len(sg.validate_voices([{"id": VOICE_A}, {"id": ""}])), 1)
        with self.assertRaisesRegex(ValueError, "no voice"):
            sg.validate_voices([])
        self.assertEqual(sg.validate_voices([], allow_empty=True), [])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            sg.validate_voices([{"id": VOICE_A}, {"id": VOICE_A}])
        many = [{"id": c * 32} for c in "abcdef"]
        with self.assertRaisesRegex(ValueError, "at most 5"):
            sg.validate_voices(many)

    def test_fetch_voice_info(self):
        body = json.dumps({"title": "Narradora", "description": " calm \n voice ",
                           "tags": ["calm"], "languages": ["pt"]})
        with mock.patch.object(ig, "_fetch_json", return_value=(200, body)) as fetch:
            info = sg.fetch_voice_info(VOICE_A)
        self.assertEqual(info, {"title": "Narradora", "description": "calm voice",
                                "tags": ["calm"], "languages": ["pt"]})
        self.assertIn(VOICE_A, fetch.call_args[0][0])
        self.assertNotIn("Authorization", fetch.call_args[0][1])  # public, no key sent
        with mock.patch.object(ig, "_fetch_json", return_value=(404, "no")):
            self.assertEqual(sg.fetch_voice_info(VOICE_A), {})
        with mock.patch.object(ig, "_fetch_json", side_effect=OSError("down")):
            self.assertEqual(sg.fetch_voice_info(VOICE_A), {})


class KeysTest(IsolatedMixin):

    def test_single_openrouter_key_shared_with_image_generate(self):
        self.assertEqual(sg.resolve_openrouter_key(), (None, "none"))
        os.environ["OPENROUTER_API_KEY"] = "env-key"
        self.assertEqual(sg.resolve_openrouter_key(), ("env-key", "env"))
        self.assertEqual(sg.resolve_openrouter_key("flag"), ("flag", "flag"))

    @unittest.skipUnless(ig.HAS_FERNET, "cryptography not installed")
    def test_vault_key_from_image_generate(self):
        ig.save_remembered_key("openrouter", "or-key")
        self.assertEqual(sg.resolve_openrouter_key(), ("or-key", "vault"))


class WriterTest(IsolatedMixin):

    VOICES = [{"id": VOICE_A, "label": "menino"}, {"id": VOICE_B, "label": "narradora"}]

    def test_extract_json_object(self):
        self.assertEqual(sg.extract_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(sg.extract_json_object('Aqui: {"a": {"b": 2}} fim'), {"a": {"b": 2}})
        with self.assertRaises(ValueError):
            sg.extract_json_object("no json")

    def test_normalize_story_and_voice_resolution(self):
        story = sg.normalize_story(json.loads(_story_json(VOICE_B)), self.VOICES)
        self.assertEqual(story["voice_id"], VOICE_B)
        self.assertEqual(story["voice_label"], "narradora")
        self.assertEqual([s["number"] for s in story["scenes"]], [1, 2, 3])
        by_name = sg.normalize_story({**json.loads(_story_json()), "voice_id": "Menino"},
                                     self.VOICES)
        self.assertEqual(by_name["voice_id"], VOICE_A)
        unknown = sg.normalize_story({**json.loads(_story_json()), "voice_id": "zzz"},
                                     self.VOICES)
        self.assertEqual(unknown["voice_id"], VOICE_A)
        self.assertIn("fallback", unknown["voice_reason"])
        for bad in ({"scenes": [{"narration": "x"}]}, {"title": "t", "scenes": []},
                    {"title": "t", "scenes": [{"heading": "h"}]}):
            with self.assertRaises(ValueError, msg=bad):
                sg.normalize_story(bad, self.VOICES)

    def test_instruction_lists_voices_and_language(self):
        text = sg.writer_instruction([{**self.VOICES[0], "title": "Kid",
                                       "languages": ["pt"]}], "pt-BR")
        self.assertIn(VOICE_A, text)
        self.assertIn("Kid", text)
        self.assertIn("pt-BR", text)
        self.assertIn("left to right, top to bottom", text)

    def test_write_story_request_and_cost(self):
        image = self.storyboard()
        bodies = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            bodies.append(body)
            return 200, _chat_payload(_story_json())

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            story, cost = sg.write_story(image, api_key="k", model="m",
                                         voices=self.VOICES, language="pt-BR")
        content = bodies[0]["messages"][0]["content"]
        self.assertEqual(bodies[0]["model"], "m")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(story["title"], "Um dia comum")
        self.assertAlmostEqual(cost, 0.001)

    def test_write_story_retries_once_on_bad_json(self):
        replies = iter([_chat_payload("sorry, no json", 0.001), _chat_payload(_story_json())])
        with mock.patch.object(ig, "_post_json", side_effect=lambda *a, **k: (200, next(replies))):
            story, cost = sg.write_story(self.storyboard(), api_key="k", model="m",
                                         voices=self.VOICES, language="pt-BR")
        self.assertEqual(story["title"], "Um dia comum")
        self.assertAlmostEqual(cost, 0.002)
        with mock.patch.object(ig, "_post_json", return_value=(200, _chat_payload("x"))):
            with self.assertRaisesRegex(RuntimeError, "invalid story twice"):
                sg.write_story(self.storyboard(), api_key="k", model="m",
                               voices=self.VOICES, language="pt-BR")

    def test_write_story_errors(self):
        with self.assertRaisesRegex(RuntimeError, "missing OpenRouter"):
            sg.write_story(self.storyboard(), api_key=None, model="m", voices=[],
                           language="pt-BR")
        raw = json.dumps({"error": {"message": "content management policy"}})
        with mock.patch.object(ig, "_post_json", return_value=(400, raw)):
            with self.assertRaises(ig.ContentPolicyError):
                sg.write_story(self.storyboard(), api_key="k", model="m", voices=[],
                               language="pt-BR")

    CATALOG = {
        "text/only:free": {"id": "text/only:free", "architecture": {
            "input_modalities": ["text"], "output_modalities": ["text"]}},
        "vis/free:free": {"id": "vis/free:free", "architecture": {
            "input_modalities": ["text", "image"], "output_modalities": ["text"]}},
        "vis/paid": {"id": "vis/paid", "pricing": {"prompt": "0.000001"}, "architecture": {
            "input_modalities": ["text", "image"], "output_modalities": ["text"]}},
    }

    def test_check_writer_model(self):
        with mock.patch.object(sg, "openrouter_models", return_value=self.CATALOG):
            sg.check_writer_model("vis/paid")
            with self.assertRaisesRegex(ValueError, "não aceita imagem.*vis/free:free"):
                sg.check_writer_model("text/only:free")
            with self.assertRaisesRegex(ValueError, "não existe.*vis/paid"):
                sg.check_writer_model("typo/model")
        with mock.patch.object(sg, "openrouter_models", return_value={}):
            sg.check_writer_model("text/only:free")  # catalog unavailable -> skip

    def test_batch_stops_before_any_story_when_writer_is_blind(self):
        with mock.patch.object(sg, "openrouter_models", return_value=self.CATALOG), \
                mock.patch.object(sg, "generate_story") as generate:
            with self.assertRaisesRegex(ValueError, "não aceita imagem"):
                sg.run_story_batch([self.storyboard()], voices=[{"id": VOICE_A}],
                                   writer_model="text/only:free",
                                   output_dir=str(self.tmp / "out"))
        generate.assert_not_called()

    def test_rate_limit_retry(self):
        replies = iter([(429, "busy"), (429, "busy"), (200, _chat_payload(_story_json()))])
        with mock.patch.object(ig, "_post_json", side_effect=lambda *a, **k: next(replies)), \
                mock.patch.object(sg.time, "sleep") as sleep:
            story, _cost = sg.write_story(self.storyboard(), api_key="k", model="m",
                                          voices=self.VOICES, language="pt-BR")
        self.assertEqual(story["title"], "Um dia comum")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 15])
        with mock.patch.object(ig, "_post_json", return_value=(429, "busy")), \
                mock.patch.object(sg.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "limite de uso"):
                sg.write_story(self.storyboard(), api_key="k", model="m", voices=[],
                               language="pt-BR")
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(ig, "_post_json", return_value=(429, "busy")):
            with self.assertRaises(ig.GenerationCancelled):
                sg.write_story(self.storyboard(), api_key="k", model="m", voices=[],
                               language="pt-BR", cancel_event=cancel)

    def test_write_story_image_404_message(self):
        raw = json.dumps({"error": {"message": "No endpoints found that support image input",
                                    "code": 404}})
        with mock.patch.object(ig, "_post_json", return_value=(404, raw)):
            with self.assertRaisesRegex(RuntimeError, "não aceita imagem"):
                sg.write_story(self.storyboard(), api_key="k", model="m", voices=[],
                               language="pt-BR")

    def test_script_markdown(self):
        story = sg.normalize_story(json.loads(_story_json()), self.VOICES)
        story["scenes"][1]["start"] = 65.0
        text = sg.script_markdown(story, "pt-BR")
        self.assertTrue(text.startswith("# Um dia comum"))
        self.assertIn("## Cena 2 — h1 (1:05)", text)
        self.assertIn("Voz: narradora", text)
        self.assertIn("## Scene 1", sg.script_markdown(story, "en-US"))


class AudioTest(IsolatedMixin):

    def test_parse_wav_regular_and_streamed(self):
        raw = _wav_bytes(0.25, rate=8000)
        channels, width, rate, pcm = sg.parse_wav(raw)
        self.assertEqual((channels, width, rate, len(pcm)), (1, 2, 8000, 4000))
        streamed = bytearray(raw)
        data_at = streamed.index(b"data")
        streamed[data_at + 4:data_at + 8] = struct.pack("<I", 0xFFFFFFFF)
        self.assertEqual(len(sg.parse_wav(bytes(streamed))[3]), 4000)
        self.assertEqual(sg.parse_wav(b"\x00\x01\x02", 22050), (1, 2, 22050, b"\x00\x01"))
        with self.assertRaises(ValueError):
            sg.parse_wav(b"RIFF\x00\x00\x00\x00WAVEjunk")

    def test_join_scene_audio_timings(self):
        clips = [_wav_bytes(1.0), _wav_bytes(0.5), _wav_bytes(2.0)]
        params, pcm, times = sg.join_scene_audio(clips, gap_s=0.5)
        self.assertEqual(params, (1, 2, 8000))
        self.assertEqual(times, [(0.0, 1.0), (1.5, 2.0), (2.5, 4.5)])
        self.assertEqual(len(pcm), int(4.5 * 8000) * 2)
        with self.assertRaisesRegex(ValueError, "differs"):
            sg.join_scene_audio([_wav_bytes(rate=8000), _wav_bytes(rate=16000)])

    def test_pcm_params(self):
        self.assertEqual(sg.pcm_params("audio/pcm;rate=24000;channels=2"), (24000, 2))
        self.assertEqual(sg.pcm_params("audio/pcm"), (sg.TTS_SAMPLE_RATE, 1))
        self.assertEqual(sg.pcm_params(""), (sg.TTS_SAMPLE_RATE, 1))

    def test_synthesize_via_openrouter_pcm(self):
        captured = {}
        pcm = b"\x01\x00" * 22050  # 0.5 s at 44.1 kHz mono

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            captured.update(url=url, body=body, headers=headers)
            return 200, pcm, {"content-type": "audio/pcm;rate=44100;channels=1",
                              "x-generation-id": "gen-tts-1"}

        with mock.patch.object(sg, "_post_bytes", side_effect=fake_post):
            wav_bytes, gen_id = sg.synthesize("olá", api_key="or", voice_id=VOICE_A,
                                              tts_model="fish-audio/s1")
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/audio/speech")
        self.assertEqual(captured["body"], {"model": "fish-audio/s1", "input": "olá",
                                            "voice": VOICE_A, "response_format": "pcm"})
        self.assertEqual(captured["headers"]["Authorization"], "Bearer or")
        self.assertEqual(gen_id, "gen-tts-1")
        self.assertEqual(sg.parse_wav(wav_bytes)[:3], (1, 2, 44100))
        self.assertEqual(len(sg.parse_wav(wav_bytes)[3]), len(pcm))

    def test_synthesize_other_replies(self):
        wav = _wav_bytes()
        with mock.patch.object(sg, "_post_bytes",
                               return_value=(200, wav, {"content-type": "audio/wav"})):
            self.assertEqual(sg.synthesize("x", api_key="k", voice_id=VOICE_A,
                                           tts_model="m"), (wav, ""))
        with mock.patch.object(sg, "_post_bytes",
                               return_value=(200, b"\xff\xfb", {"content-type": "audio/mpeg"})):
            with self.assertRaisesRegex(RuntimeError, "expected pcm"):
                sg.synthesize("x", api_key="k", voice_id=VOICE_A, tts_model="m")
        with mock.patch.object(sg, "_post_bytes", return_value=(402, b"no credit", {})):
            with self.assertRaisesRegex(RuntimeError, "402"):
                sg.synthesize("x", api_key="k", voice_id=VOICE_A, tts_model="m")
        blocked = json.dumps({"error": {"message": "content management policy"}}).encode()
        with mock.patch.object(sg, "_post_bytes", return_value=(400, blocked, {})):
            with self.assertRaises(ig.ContentPolicyError):
                sg.synthesize("x", api_key="k", voice_id=VOICE_A, tts_model="m")
        with self.assertRaisesRegex(RuntimeError, "missing OpenRouter"):
            sg.synthesize("x", api_key=None, voice_id=VOICE_A, tts_model="m")
        self.assertEqual(sg.synthesize("x", api_key=None, voice_id="", tts_model="m",
                                       dry_run=True)[1], "")

    def test_lookup_generation_cost(self):
        replies = {"g1": (200, json.dumps({"data": {"total_cost": 0.0012}})),
                   "g2": (200, json.dumps({"data": {"total_cost": 0.0008}}))}

        def fake_fetch(url, headers, timeout_s, cancel_event=None):
            return replies[url.rsplit("=", 1)[1]]

        with mock.patch.object(ig, "_fetch_json", side_effect=fake_fetch):
            self.assertAlmostEqual(sg.lookup_generation_cost(["g1", "g2"], "k"), 0.002)
        self.assertEqual(sg.lookup_generation_cost([], "k"), 0.0)
        self.assertIsNone(sg.lookup_generation_cost(["g1"], None))
        # stats lag: 404 twice, then available
        lagging = iter([(404, ""), (404, ""), replies["g1"]])
        with mock.patch.object(ig, "_fetch_json", side_effect=lambda *a, **k: next(lagging)):
            self.assertAlmostEqual(sg.lookup_generation_cost(["g1"], "k", poll_s=0.01), 0.0012)
        with mock.patch.object(ig, "_fetch_json", return_value=(404, "")):
            self.assertIsNone(sg.lookup_generation_cost(["g1"], "k", deadline_s=0.05,
                                                        poll_s=0.01))
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(ig, "_fetch_json", return_value=(404, "")):
            self.assertIsNone(sg.lookup_generation_cost(["g1"], "k", cancel, poll_s=5))

    def test_scene_at_and_clock(self):
        scenes = [{"start": 0.0}, {"start": 3.0}, {"start": 7.5}]
        self.assertEqual([sg.scene_at(scenes, t) for t in (0, 2.9, 3.0, 7.0, 99)],
                         [0, 0, 1, 1, 2])
        self.assertEqual(sg.format_clock(125.9), "2:05")


class PlayerTest(IsolatedMixin):
    """WavPlayer with a silent sink (cat > /dev/null) instead of a sound card."""

    def make_player(self, seconds=3.0, on_end=None):
        path = self.tmp / "a.wav"
        path.write_bytes(_wav_bytes(seconds, rate=8000))
        return sg.WavPlayer(path, on_end=on_end, command=["sh", "-c", "cat > /dev/null"])

    def test_play_pause_seek(self):
        player = self.make_player()
        self.addCleanup(player.stop)
        self.assertAlmostEqual(player.duration, 3.0)
        player.play()
        time.sleep(0.3)
        self.assertTrue(player.playing)
        player.pause()
        paused_at = player.position()
        self.assertGreater(paused_at, 0.15)
        time.sleep(0.2)
        self.assertEqual(player.position(), paused_at)
        player.seek(2.0)
        self.assertAlmostEqual(player.position(), 2.0)
        player.skip(-5)
        self.assertEqual(player.position(), 0.0)
        player.seek(99)
        self.assertAlmostEqual(player.position(), 3.0)

    def test_end_callback_and_replay(self):
        ended = threading.Event()
        player = self.make_player(seconds=0.4, on_end=ended.set)
        self.addCleanup(player.stop)
        player.play()
        self.assertTrue(ended.wait(3))
        self.assertFalse(player.playing)
        player.play()  # at the end -> restarts from 0
        self.assertLess(player.position(), 0.3)

    def test_no_player_available(self):
        path = self.tmp / "a.wav"
        path.write_bytes(_wav_bytes())
        with mock.patch.object(sg.shutil, "which", return_value=None):
            player = sg.WavPlayer(path)
        self.assertFalse(player.available)
        with self.assertRaisesRegex(RuntimeError, "no audio player"):
            player.play()


class PipelineTest(IsolatedMixin):

    def run_dry(self, image, **over):
        params = {"output_dir": str(self.tmp / "out"), "voices": [], "dry_run": True}
        params.update(over)
        return sg.generate_story(image, **params)

    def test_dry_run_folder_contents(self):
        result = self.run_dry(self.storyboard())
        folder = Path(result["folder"])
        self.assertEqual(folder.parent, self.tmp / "out")
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["audio.wav", "roteiro.md", "story.json", "storyboard.png"])
        meta = json.loads((folder / "story.json").read_text(encoding="utf-8"))
        self.assertEqual(len(meta["scenes"]), 6)
        starts = [s["start"] for s in meta["scenes"]]
        self.assertEqual(starts, sorted(starts))
        with wave.open(str(folder / "audio.wav")) as src:
            self.assertAlmostEqual(src.getnframes() / src.getframerate(),
                                   meta["audio_seconds"], places=2)
        rows = sg.read_log_rows(self.tmp / "out" / sg.LOG_FILENAME)
        self.assertEqual([r["folder"] for r in rows], [folder.name])
        self.assertFalse(any(p.name.startswith(".story-") for p in (self.tmp / "out").iterdir()))

    def test_real_flow_with_mocks(self):
        image = self.storyboard()
        voices = [{"id": VOICE_A, "label": "menino"}, {"id": VOICE_B, "label": "narradora"}]
        texts = []

        def fake_tts(text, **kwargs):
            texts.append((text, kwargs["voice_id"], kwargs["tts_model"], kwargs["api_key"]))
            return _wav_bytes(1.0), f"gen-{len(texts)}"

        with mock.patch.object(ig, "_post_json",
                               return_value=(200, _chat_payload(_story_json(VOICE_B)))), \
                mock.patch.object(sg, "synthesize", side_effect=fake_tts):
            result = sg.generate_story(image, output_dir=str(self.tmp / "out"), voices=voices,
                                       openrouter_key="k", tts_model="fish-audio/s1")
        self.assertEqual(Path(result["folder"]).name, "Um dia comum")
        self.assertEqual([t[1] for t in texts], [VOICE_B] * 3)
        self.assertEqual({t[3] for t in texts}, {"k"})  # same OpenRouter key as the writer
        self.assertEqual(result["generation_ids"], ["gen-1", "gen-2", "gen-3"])
        self.assertEqual(texts[0][0], "Cena 0 texto.")
        meta = result["story"]
        self.assertEqual([s["start"] for s in meta["scenes"]], [0.0, 1.6, 3.2])
        self.assertEqual(result["entry"]["voice_label"], "narradora")
        self.assertEqual(result["entry"]["key_hash"], ig.key_hash("k"))
        self.assertEqual(result["entry"]["tts_cost_usd"], "")  # paid: filled in background
        self.assertEqual(result["story"]["tts_generation_ids"], ["gen-1", "gen-2", "gen-3"])

    def test_paid_narration_cost_filled_in_background(self):
        image = self.storyboard()
        with mock.patch.object(ig, "_post_json",
                               return_value=(200, _chat_payload(_story_json(VOICE_B)))), \
                mock.patch.object(sg, "synthesize",
                                  side_effect=lambda text, **kw: (_wav_bytes(0.5), "gen-x")), \
                mock.patch.object(sg, "lookup_generation_cost", return_value=0.003) as cost, \
                mock.patch.object(sg, "check_writer_model"), \
                mock.patch.object(sg, "fetch_voice_info", return_value={}):
            batch = sg.run_story_batch([image], voices=[{"id": VOICE_B}], openrouter_key="k",
                                       output_dir=str(self.tmp / "out"),
                                       tts_model="fish-audio/s1", writer_model="m")
        self.assertEqual(cost.call_args[0][:2], (["gen-x"] * 3, "k"))
        result = batch["created"][0]
        self.assertEqual(result["entry"]["tts_cost_usd"], "0.003000")
        self.assertAlmostEqual(batch["cost"], 0.001 + 0.003)
        self.assertEqual(sg.load_story_meta(Path(result["folder"]))["tts_cost_usd"], 0.003)
        log_path = self.tmp / "out" / sg.LOG_FILENAME
        self.assertEqual(sg.read_log_rows(log_path)[0]["tts_cost_usd"], "0.003000")
        self.assertIn("total_cost_usd=0.004000", log_path.read_text().splitlines()[0])

    def test_free_narration_cost_is_zero_immediately(self):
        image = self.storyboard()
        with mock.patch.object(ig, "_post_json",
                               return_value=(200, _chat_payload(_story_json(VOICE_B)))), \
                mock.patch.object(sg, "synthesize",
                                  side_effect=lambda text, **kw: (_wav_bytes(0.5), "gen-x")), \
                mock.patch.object(sg, "lookup_generation_cost") as cost:
            result = sg.generate_story(image, output_dir=str(self.tmp / "out"),
                                       voices=[{"id": VOICE_B}], openrouter_key="k",
                                       tts_model="fish-audio/s2.1-pro-free:free")
        cost.assert_not_called()
        self.assertEqual(result["entry"]["tts_cost_usd"], "0.000000")

    def test_backfill_free_costs(self):
        result = self.run_dry(self.storyboard())
        folder = Path(result["folder"])
        log_path = self.tmp / "out" / sg.LOG_FILENAME
        rows = sg.read_log_rows(log_path)
        rows[0].update(tts_cost_usd="", tts_model="fish-audio/s2.1-pro-free:free")
        sg.write_log_rows(log_path, rows)
        meta = sg.load_story_meta(folder)
        meta.update(tts_cost_usd=None, tts_model="fish-audio/s2.1-pro-free:free")
        (folder / sg.STORY_JSON).write_text(json.dumps(meta))
        self.assertEqual(sg.backfill_free_costs(self.tmp / "out"), 1)
        self.assertEqual(sg.read_log_rows(log_path)[0]["tts_cost_usd"], "0.000000")
        self.assertEqual(sg.load_story_meta(folder)["tts_cost_usd"], 0.0)

    def test_skip_existing_and_force(self):
        image = self.storyboard()
        first = self.run_dry(image)
        again = self.run_dry(image)
        self.assertTrue(again["skipped"])
        self.assertEqual(again["folder"], first["folder"])
        forced = self.run_dry(image, force=True)
        self.assertFalse(forced["skipped"])
        self.assertTrue(forced["folder"].endswith("(2)"))

    def test_cancel_leaves_nothing(self):
        image = self.storyboard()
        cancel = threading.Event()

        def cancelling_tts(text, **kwargs):
            cancel.set()
            return _wav_bytes(), ""

        with mock.patch.object(sg, "synthesize", side_effect=cancelling_tts):
            with self.assertRaises(ig.GenerationCancelled):
                self.run_dry(image, cancel_event=cancel)
        out = self.tmp / "out"
        self.assertEqual([p.name for p in out.iterdir()], [])

    def test_error_while_writing_files_cleans_temp(self):
        with mock.patch.object(sg, "write_wav", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_dry(self.storyboard())
        self.assertEqual(list((self.tmp / "out").iterdir()), [])

    def test_batch_continues_after_error(self):
        good = self.storyboard("b.png")
        bad = self.storyboard("a.png")
        real = sg.generate_story
        seen = []

        def flaky(image, **kwargs):
            if Path(image).name == "a.png":
                raise ig.ContentPolicyError("blocked")
            return real(image, **kwargs)

        with mock.patch.object(sg, "generate_story", side_effect=flaky):
            batch = sg.run_story_batch([bad, good], voices=[], dry_run=True,
                                       output_dir=str(self.tmp / "out"),
                                       on_progress=lambda info: seen.append(info["done"]))
        self.assertEqual(len(batch["created"]), 1)
        self.assertEqual(len(batch["errors"]), 1)
        self.assertIn("blocked", batch["errors"][0]["error"])
        self.assertEqual(seen, [1, 2])

    def test_batch_cancel_propagates(self):
        with mock.patch.object(sg, "generate_story",
                               side_effect=ig.GenerationCancelled("stop")):
            with self.assertRaises(ig.GenerationCancelled):
                sg.run_story_batch([self.storyboard()], voices=[], dry_run=True,
                                   output_dir=str(self.tmp / "out"))

    def test_list_stories_newest_first(self):
        self.run_dry(self.storyboard("a.png"))
        time.sleep(1.1)
        self.run_dry(self.storyboard("b.png"))
        titles = [meta["title"] for _f, meta in sg.list_stories(self.tmp / "out")]
        self.assertEqual(titles, ["Teste b", "Teste a"])

    def test_story_list_label(self):
        folder = Path("/x/Um dia")
        meta = {"title": "Um dia", "source_image": "/p/muse 1/a.png", "style": "connective"}
        self.assertEqual(sg.story_list_label(folder, meta), "Um dia - muse 1 - Narrativo")
        self.assertEqual(sg.story_list_label(folder, {**meta, "style": "descriptive"}),
                         "Um dia - muse 1 - Descritivo")
        # made before styles existed -> Descritivo; no source -> title + style
        self.assertEqual(sg.story_list_label(folder, {"title": "Um dia",
                                                      "source_image": "/p/muse 1/a.png"}),
                         "Um dia - muse 1 - Descritivo")
        self.assertEqual(sg.story_list_label(folder, {}), "Um dia - Descritivo")

    def test_safe_folder_name(self):
        self.assertEqual(sg.safe_folder_name('A/B: "c"?'), "A B c")
        self.assertEqual(sg.safe_folder_name("  ..  "), "Historia")
        self.assertLessEqual(len(sg.safe_folder_name("x" * 200)), sg.MAX_TITLE_CHARS)

    def test_find_storyboard_image_prefers_preview(self):
        folder = self.tmp / "s"
        folder.mkdir()
        (folder / "storyboard.jpg").write_bytes(b"x")
        self.assertEqual(sg.find_storyboard_image(folder, {"storyboard": "storyboard.jpg"}),
                         folder / "storyboard.jpg")
        (folder / "preview.png").write_bytes(b"x")
        self.assertEqual(sg.find_storyboard_image(folder, {}), folder / "preview.png")


class ControlsTest(IsolatedMixin):
    """Writer style, target duration, status steps, failures + retry."""

    def run_dry(self, image, **over):
        params = {"output_dir": str(self.tmp / "out"), "voices": [], "dry_run": True}
        params.update(over)
        return sg.generate_story(image, **params)

    def test_parse_duration(self):
        for raw, expected in (("", None), (None, None), ("90", 90), ("90s", 90),
                              ("1:30", 90), ("2m", 120), ("1m30s", 90), (" 45 ", 45)):
            self.assertEqual(sg.parse_duration(raw), expected, raw)
        for bad in ("5", "abc", "1:75", "31m", "-3"):
            with self.assertRaises(ValueError, msg=bad):
                sg.parse_duration(bad)

    def test_styles(self):
        self.assertEqual(sg.style_from_label(sg.style_label("connective")), "connective")
        self.assertEqual(sg.style_from_label("nonsense"), sg.DEFAULT_STYLE)
        descriptive = sg.writer_instruction([], "pt-BR", "descriptive")
        connective = sg.writer_instruction([], "pt-BR", "connective")
        self.assertIn("do not invent extra scenes", descriptive)
        self.assertNotIn("bridging the gap", descriptive)
        self.assertIn("bridging the gap", connective)
        self.assertIn("never contradict a panel", connective)
        for text in (descriptive, connective):
            self.assertIn("one scene per panel", text)  # keeps the audio/panel sync

    def test_target_duration_words_per_voice(self):
        voices = [{"id": VOICE_A, "label": "lento"}, {"id": VOICE_B, "label": "rapido"}]
        rates = {VOICE_A: 15.0, VOICE_B: 20.0, "*": 17.5}
        text = sg.writer_instruction(voices, "pt-BR", target_s=90, rates=rates)
        slow, fast = sg.length_words(90, 15.0), sg.length_words(90, 20.0)
        self.assertLess(slow, fast)
        self.assertIn(f"about {slow} words", text)
        self.assertIn(f"about {fast} words", text)
        self.assertIn("1:30", text)
        self.assertNotIn("sentences per scene", text)
        self.assertIn("sentences per scene", sg.writer_instruction(voices, "pt-BR"))
        # 90 s at 17.4 chars/s minus 5 pauses ~ 270 words
        self.assertTrue(250 <= sg.length_words(90, sg.DEFAULT_CHARS_PER_S) <= 290)

    def test_speech_rates_from_history(self):
        out = self.tmp / "out"
        folder = out / "s"
        folder.mkdir(parents=True)
        scenes = [{"narration": "x" * 150}, {"narration": "y" * 150}]
        (folder / sg.STORY_JSON).write_text(json.dumps(
            {"voice_id": VOICE_A, "scenes": scenes, "audio_seconds": 15 + sg.SCENE_GAP_S,
             "created": "2026"}))
        rates = sg.speech_rates(out)
        self.assertAlmostEqual(rates[VOICE_A], 20.0)
        self.assertAlmostEqual(rates["*"], 20.0)
        self.run_dry(self.storyboard())  # dry-run stories never calibrate
        self.assertEqual(set(sg.speech_rates(out)), {VOICE_A, "*"})

    def test_style_and_target_reach_the_writer_and_log(self):
        image = self.storyboard()
        prompts = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            prompts.append(body["messages"][0]["content"][0]["text"])
            return 200, _chat_payload(_story_json(VOICE_A))

        with mock.patch.object(ig, "_post_json", side_effect=fake_post), \
                mock.patch.object(sg, "synthesize",
                                  side_effect=lambda text, **kw: (_wav_bytes(0.5), "")):
            result = sg.generate_story(image, output_dir=str(self.tmp / "out"),
                                       voices=[{"id": VOICE_A}], openrouter_key="k",
                                       style="connective", target_s=60)
        self.assertIn("bridging the gap", prompts[0])
        self.assertIn("1:00", prompts[0])
        row = sg.read_log_rows(self.tmp / "out" / sg.LOG_FILENAME)[0]
        self.assertEqual((row["status"], row["style"], row["target_seconds"]),
                         ("ok", "connective", "60"))
        self.assertEqual(result["story"]["style"], "connective")

    def test_status_steps(self):
        steps = []
        sg.run_story_batch([self.storyboard()], voices=[], dry_run=True,
                           output_dir=str(self.tmp / "out"), on_status=steps.append)
        joined = "\n".join(steps)
        self.assertIn("sending storyboard to the writer", joined)
        self.assertIn(f"writer {sg.WRITER_DEFAULT_MODEL} replied", joined)
        self.assertIn("narrating scene 1/6", joined)
        self.assertIn("narrating scene 6/6", joined)
        self.assertIn("saving the story folder", joined)

    def test_rate_limit_wait_is_reported(self):
        steps = []
        replies = iter([(429, "busy"), (200, _chat_payload(_story_json()))])
        with mock.patch.object(ig, "_post_json", side_effect=lambda *a, **k: next(replies)), \
                mock.patch.object(sg.time, "sleep"):
            sg.write_story(self.storyboard(), api_key="k", model="m", voices=[],
                           language="pt-BR", status=steps.append)
        self.assertTrue(any("writer m busy (HTTP 429): retrying in 5 s (1/3)" in s
                            for s in steps), steps)

    def test_parse_model_chain(self):
        self.assertEqual(sg.parse_model_chain(" a ; b;;a; c "), ["a", "b", "c"])
        self.assertEqual(sg.parse_model_chain(""), [sg.WRITER_DEFAULT_MODEL])
        self.assertEqual(sg.parse_model_chain(None), [sg.WRITER_DEFAULT_MODEL])
        self.assertEqual(sg.parse_model_chain(["x", "x", "y"]), ["x", "y"])

    def test_fallback_after_rate_limit_retries(self):
        calls, steps = [], []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            calls.append(body["model"])
            if body["model"] == "free/a:free":
                return 429, "busy"
            return 200, _chat_payload(_story_json(), cost=0.002)

        with mock.patch.object(ig, "_post_json", side_effect=fake_post), \
                mock.patch.object(sg.time, "sleep"):
            story, cost = sg.write_story(self.storyboard(), api_key="k",
                                         model="free/a:free;paid/b", voices=[],
                                         language="pt-BR", status=steps.append)
        self.assertEqual(calls, ["free/a:free"] * 4 + ["paid/b"])  # 1 + 3 retries, then b
        self.assertEqual(story["writer_model"], "paid/b")
        self.assertEqual(story["writer_fallbacks"][0]["model"], "free/a:free")
        self.assertIn("429", story["writer_fallbacks"][0]["error"])
        self.assertAlmostEqual(cost, 0.002)
        self.assertTrue(any("falling back to paid/b (2/2)" in m for m in steps), steps)

    def test_status_sequence_through_retries_and_fallback(self):
        steps = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            return (429, "busy") if body["model"] == "q:free" else \
                (200, _chat_payload(_story_json()))

        with mock.patch.object(ig, "_post_json", side_effect=fake_post), \
                mock.patch.object(sg.time, "sleep"):
            sg.write_story(self.storyboard(), api_key="k", model="q:free;g", voices=[],
                           language="pt-BR", status=steps.append)
        self.assertEqual(steps, [
            "sent to the writer (q:free, thinking default): waiting for the story (up to 3 min)...",
            "writer q:free busy (HTTP 429): retrying in 5 s (1/3)",
            "writer q:free: retry 1/3 sent, waiting for the reply...",
            "writer q:free busy (HTTP 429): retrying in 15 s (2/3)",
            "writer q:free: retry 2/3 sent, waiting for the reply...",
            "writer q:free busy (HTTP 429): retrying in 30 s (3/3)",
            "writer q:free: retry 3/3 sent, waiting for the reply...",
            "writer q:free failed: falling back to g (2/2)",
            "sent to the writer (g, thinking default): waiting for the story (up to 3 min)...",
        ])

    QWEN_LIKE = {"q": {"id": "q", "reasoning": {"mandatory": False, "default_enabled": True,
                                                "supported_efforts": ["xhigh", "medium", "low"],
                                                "default_effort": "xhigh"}},
                 "g": {"id": "g", "reasoning": {"mandatory": True, "default_enabled": True,
                                                "supported_efforts": ["high", "medium", "low"],
                                                "default_effort": "medium"}},
                 "off": {"id": "off", "reasoning": {"mandatory": False,
                                                    "default_enabled": False}},
                 "plain": {"id": "plain"}}

    def test_effort_ladder(self):
        self.assertEqual(sg.effort_ladder("q", self.QWEN_LIKE), ["xhigh", "medium", "low", "none"])
        self.assertEqual(sg.effort_ladder("g", self.QWEN_LIKE), ["medium", "low"])  # mandatory
        self.assertEqual(sg.effort_ladder("off", self.QWEN_LIKE), [])
        self.assertEqual(sg.effort_ladder("plain", self.QWEN_LIKE), [])
        self.assertEqual(sg.effort_ladder("unknown", self.QWEN_LIKE),
                         ["medium", "low", "minimal", "none"])

    def test_empty_reply_lowers_thinking_one_level_each_time(self):
        sent, steps = [], []
        replies = iter([_chat_payload(""), _chat_payload("   "),
                        _chat_payload(_story_json(), 0.001)])

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            sent.append(body.get("reasoning"))
            return 200, next(replies)

        with mock.patch.object(sg, "openrouter_models", return_value=self.QWEN_LIKE), \
                mock.patch.object(ig, "_post_json", side_effect=fake_post):
            story, _cost = sg.write_story(self.storyboard(), api_key="k", model="q", voices=[],
                                          language="pt-BR", status=steps.append)
        self.assertEqual(sent, [None, {"effort": "medium"}, {"effort": "low"}])
        self.assertEqual(story["writer_effort"], "low")
        self.assertIn("writer q: empty reply - retrying with less thinking "
                      "(effort default -> medium)", steps)
        self.assertIn("writer q: empty reply - retrying with less thinking "
                      "(effort medium -> low)", steps)

    def test_timeout_lowers_thinking(self):
        sent = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            sent.append(body.get("reasoning"))
            if len(sent) == 1:
                raise TimeoutError("The read operation timed out")
            return 200, _chat_payload(_story_json())

        with mock.patch.object(sg, "openrouter_models", return_value=self.QWEN_LIKE), \
                mock.patch.object(ig, "_post_json", side_effect=fake_post):
            story, _ = sg.write_story(self.storyboard(), api_key="k", model="q", voices=[],
                                      language="pt-BR")
        self.assertEqual(sent, [None, {"effort": "medium"}])
        self.assertEqual(story["writer_effort"], "medium")

    def test_truncated_by_thinking_counts_as_empty(self):
        sent = []
        truncated = json.dumps({"choices": [{"message": {"content": '{"title": "Um'},
                                             "finish_reason": "length"}]})

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            sent.append(body.get("reasoning"))
            return 200, truncated if len(sent) == 1 else _chat_payload(_story_json())

        with mock.patch.object(sg, "openrouter_models", return_value=self.QWEN_LIKE), \
                mock.patch.object(ig, "_post_json", side_effect=fake_post):
            sg.write_story(self.storyboard(), api_key="k", model="q", voices=[],
                           language="pt-BR")
        self.assertEqual(sent, [None, {"effort": "medium"}])

    def test_lowest_level_failing_moves_to_next_model(self):
        sent = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            sent.append((body["model"], (body.get("reasoning") or {}).get("effort")))
            if body["model"] == "g":
                return 200, _chat_payload("")
            return 200, _chat_payload(_story_json())

        with mock.patch.object(sg, "openrouter_models", return_value=self.QWEN_LIKE), \
                mock.patch.object(ig, "_post_json", side_effect=fake_post):
            story, _ = sg.write_story(self.storyboard(), api_key="k", model="g;q", voices=[],
                                      language="pt-BR")
        # g: default(medium) -> low (its lowest, thinking is mandatory) -> fallback to q
        self.assertEqual(sent, [("g", None), ("g", "low"), ("q", None)])
        self.assertEqual(story["writer_model"], "q")
        self.assertIn("lowest thinking level", story["writer_fallbacks"][0]["error"])

    def test_model_without_thinking_fails_fast_on_empty(self):
        with mock.patch.object(sg, "openrouter_models", return_value=self.QWEN_LIKE), \
                mock.patch.object(ig, "_post_json", return_value=(200, _chat_payload(""))) as post:
            with self.assertRaisesRegex(RuntimeError, "empty reply"):
                sg.write_story(self.storyboard(), api_key="k", model="off", voices=[],
                               language="pt-BR")
        self.assertEqual(post.call_count, 1)

    def test_invalid_json_retries_same_level(self):
        sent = []
        replies = iter([_chat_payload("não sei"), _chat_payload(_story_json())])

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            sent.append(body.get("reasoning"))
            return 200, next(replies)

        with mock.patch.object(sg, "openrouter_models", return_value=self.QWEN_LIKE), \
                mock.patch.object(ig, "_post_json", side_effect=fake_post):
            sg.write_story(self.storyboard(), api_key="k", model="q", voices=[],
                           language="pt-BR")
        self.assertEqual(sent, [None, None])  # not an empty reply: thinking unchanged

    def test_inkling_not_suggested(self):
        catalog = {"thinkingmachines/inkling:free": {"architecture": {
            "input_modalities": ["image"], "output_modalities": ["text"]}},
                   "g/vis:free": {"architecture": {"input_modalities": ["image"],
                                                   "output_modalities": ["text"]}}}
        self.assertEqual(sg.vision_model_suggestions(catalog, free_only=True), ["g/vis:free"])

    def test_fallback_on_any_failure_and_all_failed(self):
        replies = {"a": (500, "server down"), "b": (200, _chat_payload("not json", 0.001)),
                   "c": (200, _chat_payload(_story_json(), 0.001))}
        with mock.patch.object(ig, "_post_json",
                               side_effect=lambda url, body, *a, **k: replies[body["model"]]):
            story, cost = sg.write_story(self.storyboard(), api_key="k", model="a;b;c",
                                         voices=[], language="pt-BR")
        self.assertEqual(story["writer_model"], "c")
        self.assertEqual([f["model"] for f in story["writer_fallbacks"]], ["a", "b"])
        self.assertAlmostEqual(cost, 0.003)  # b's two billed invalid replies + c
        with mock.patch.object(ig, "_post_json", return_value=(500, "down")):
            with self.assertRaisesRegex(RuntimeError, "all 2 writer models failed: a: .* \\| b:"):
                sg.write_story(self.storyboard(), api_key="k", model="a;b", voices=[],
                               language="pt-BR")

    def test_fallback_stops_on_cancel(self):
        cancel = threading.Event()
        calls = []

        def fake_post(url, body, headers, timeout_s, cancel_event=None):
            calls.append(body["model"])
            cancel.set()
            raise ig.GenerationCancelled("stop")

        with mock.patch.object(ig, "_post_json", side_effect=fake_post):
            with self.assertRaises(ig.GenerationCancelled):
                sg.write_story(self.storyboard(), api_key="k", model="a;b", voices=[],
                               language="pt-BR", cancel_event=cancel)
        self.assertEqual(calls, ["a"])

    def test_chain_checked_before_batch_and_log_shows_model_used(self):
        catalog = {m: {"id": m, "architecture": {"input_modalities": ["text", "image"],
                                                 "output_modalities": ["text"]}}
                   for m in ("vis/a", "vis/b")}
        catalog["txt/c"] = {"id": "txt/c", "architecture": {"input_modalities": ["text"],
                                                            "output_modalities": ["text"]}}
        with mock.patch.object(sg, "openrouter_models", return_value=catalog):
            sg.check_writer_model("vis/a;vis/b")
            with self.assertRaisesRegex(ValueError, "txt/c.*não aceita imagem"):
                sg.check_writer_model("vis/a;txt/c")
        replies = {"vis/a": (429, "busy"), "vis/b": (200, _chat_payload(_story_json(VOICE_A)))}
        with mock.patch.object(ig, "_post_json",
                               side_effect=lambda url, body, *a, **k: replies[body["model"]]), \
                mock.patch.object(sg.time, "sleep"), \
                mock.patch.object(sg, "synthesize",
                                  side_effect=lambda text, **kw: (_wav_bytes(0.3), "")):
            result = sg.generate_story(self.storyboard(), output_dir=str(self.tmp / "out"),
                                       voices=[{"id": VOICE_A}], openrouter_key="k",
                                       writer_model="vis/a;vis/b")
        self.assertEqual(result["entry"]["writer_model"], "vis/b")
        self.assertEqual(result["story"]["writer_chain"], ["vis/a", "vis/b"])
        self.assertEqual(result["story"]["writer_fallbacks"][0]["model"], "vis/a")

    def test_failures_are_logged_and_retryable(self):
        good, bad = self.storyboard("good.png"), self.storyboard("bad.png")
        real = sg.generate_story

        def flaky(image, **kwargs):
            if Path(image).name == "bad.png":
                raise ig.ContentPolicyError("blocked by filter")
            return real(image, **kwargs)

        out = self.tmp / "out"
        with mock.patch.object(sg, "generate_story", side_effect=flaky):
            sg.run_story_batch([bad, good], voices=[], dry_run=True, output_dir=str(out),
                               style="connective")
        rows = sg.read_log_rows(out / sg.LOG_FILENAME)
        errors = [r for r in rows if r["status"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["source_image"], str(bad))
        self.assertIn("blocked by filter", errors[0]["error"])
        self.assertEqual(errors[0]["style"], "connective")
        self.assertEqual(sg.failed_storyboards(out), [bad])
        self.assertEqual(sg.failed_storyboards(out, "connective"), [bad])
        self.assertEqual(sg.failed_storyboards(out, "descriptive"), [])
        # a descriptive story does not fix the connective failure...
        self.run_dry(bad)
        self.assertEqual(sg.failed_storyboards(out, "connective"), [bad])
        # ...retrying in the failed style does (history row stays)
        self.run_dry(bad, style="connective")
        self.assertEqual(sg.failed_storyboards(out), [])
        self.assertEqual(len([r for r in sg.read_log_rows(out / sg.LOG_FILENAME)
                              if r["status"] == "error"]), 1)

    def test_skip_is_per_writer_style(self):
        image = self.storyboard()
        first = self.run_dry(image)                       # descriptive
        self.assertTrue(self.run_dry(image)["skipped"])   # same style -> skipped
        narrative = self.run_dry(image, style="connective")
        self.assertFalse(narrative["skipped"])            # other style -> written
        self.assertNotEqual(narrative["folder"], first["folder"])
        again = self.run_dry(image, style="connective")
        self.assertTrue(again["skipped"])
        self.assertEqual((again["folder"], again["style"]), (narrative["folder"], "connective"))

    def test_stories_without_style_count_as_descriptive(self):
        image = self.storyboard()
        folder = Path(self.run_dry(image)["folder"])
        meta = sg.load_story_meta(folder)
        meta.pop("style")                                 # made before styles existed
        (folder / sg.STORY_JSON).write_text(json.dumps(meta))
        self.assertTrue(self.run_dry(image)["skipped"])
        self.assertFalse(self.run_dry(image, style="connective")["skipped"])
        self.assertEqual(sg.story_style({}), "descriptive")
        self.assertEqual(sg.story_style({"style": "weird"}), "descriptive")

    def test_cli_skips_per_style(self):
        self.storyboard("a.png")
        base = ["--input-dir", str(self.tmp / "in"), "--output-dir", str(self.tmp / "out"),
                "--dry-run"]
        with redirect_stdout(io.StringIO()):
            sg.main(base)
        with redirect_stdout(io.StringIO()) as out:
            sg.main(base + ["--style", "connective"])
        self.assertIn("1 created, 0 skipped", out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            sg.main(base + ["--style", "connective"])
        self.assertIn("skipped (already done as connective)", out.getvalue())

    def test_failed_storyboard_deleted_is_not_listed(self):
        bad = self.storyboard("gone.png")
        sg.log_failure(self.tmp / "out", bad, "boom", {})
        self.assertEqual(sg.failed_storyboards(self.tmp / "out"), [bad])
        bad.unlink()
        self.assertEqual(sg.failed_storyboards(self.tmp / "out"), [])

    def test_first_failure_creates_output_dir(self):
        with mock.patch.object(sg, "generate_story", side_effect=RuntimeError("x")):
            sg.run_story_batch([self.storyboard()], voices=[], dry_run=True,
                               output_dir=str(self.tmp / "new" / "out"))
        self.assertTrue((self.tmp / "new" / "out" / sg.LOG_FILENAME).is_file())


class DeleteTest(IsolatedMixin):
    """Deleting a production: whole story folder or audio only."""

    def setUp(self):
        super().setUp()
        # never touch the real desktop Trash from tests
        no_gio = mock.patch.object(sg.shutil, "which",
                                   side_effect=lambda cmd: None if cmd == "gio" else "/bin/x")
        no_gio.start()
        self.addCleanup(no_gio.stop)
        self.image = self.storyboard()
        self.result = sg.generate_story(self.image, output_dir=str(self.tmp / "out"),
                                        voices=[], dry_run=True)
        self.folder = Path(self.result["folder"])
        self.log = self.tmp / "out" / sg.LOG_FILENAME

    def test_delete_whole_production_keeps_original(self):
        self.assertEqual(sg.delete_story(self.folder), "deleted")
        self.assertFalse(self.folder.exists())
        self.assertTrue(self.image.is_file())  # original storyboard untouched
        self.assertEqual(sg.read_log_rows(self.log)[-1]["status"], "deleted")
        self.assertEqual(sg.failed_storyboards(self.tmp / "out"), [])
        again = sg.generate_story(self.image, output_dir=str(self.tmp / "out"), voices=[],
                                  dry_run=True)
        self.assertFalse(again["skipped"])  # no story anymore -> written again

    def test_delete_audio_only(self):
        sg.delete_story(self.folder, audio_only=True)
        self.assertFalse((self.folder / sg.AUDIO_WAV).exists())
        self.assertTrue((self.folder / sg.SCRIPT_MD).is_file())
        self.assertTrue(sg.load_story_meta(self.folder)["audio_deleted"])
        self.assertEqual(sg.read_log_rows(self.log)[-1]["status"], "no audio")
        self.assertTrue(sg.generate_story(self.image, output_dir=str(self.tmp / "out"),
                                          voices=[], dry_run=True)["skipped"])
        with self.assertRaisesRegex(FileNotFoundError, "no audio anymore"):
            sg.delete_story(self.folder, audio_only=True)

    def test_error_rows_are_not_relabelled(self):
        sg.log_failure(self.tmp / "out", self.image, "boom", {})
        rows = sg.read_log_rows(self.log)
        rows[-1]["folder"] = self.folder.name  # an error row sharing the name
        sg.write_log_rows(self.log, rows)
        sg.delete_story(self.folder)
        statuses = [r["status"] for r in sg.read_log_rows(self.log)]
        self.assertEqual(statuses, ["deleted", "error"])

    def test_not_a_story_folder(self):
        other = self.tmp / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "not a story folder"):
            sg.delete_story(other)

    def test_trash_when_gio_is_available(self):
        def fake_gio(cmd, **kwargs):
            sg.shutil.rmtree(cmd[-1])  # what "gio trash" does from our point of view
            return mock.Mock(returncode=0)

        with mock.patch.object(sg.shutil, "which", return_value="/usr/bin/gio"), \
                mock.patch.object(sg.subprocess, "run", side_effect=fake_gio) as run:
            self.assertEqual(sg.delete_story(self.folder), "trash")
        self.assertEqual(run.call_args[0][0][:2], ["gio", "trash"])

    def test_cli_delete(self):
        args = sg.build_parser().parse_args(["--delete", str(self.folder), "--audio-only"])
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sg.main_cli(args), 0)
        self.assertIn("deleted: audio of", out.getvalue())
        args = sg.build_parser().parse_args(["--delete", str(self.tmp / "nope")])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(sg.main_cli(args), 2)


class CliTest(IsolatedMixin):

    def args(self, **over) -> argparse.Namespace:
        args = sg.build_parser().parse_args([])
        for key, value in {"output_dir": str(self.tmp / "out"), "dry_run": True, **over}.items():
            setattr(args, key, value)
        return args

    def test_dry_run_input_dir(self):
        self.storyboard("a.png")
        self.storyboard("b.jpg")
        (self.tmp / "in" / "notes.txt").write_text("x")
        with redirect_stdout(io.StringIO()) as out:
            code = sg.main_cli(self.args(input_dir=str(self.tmp / "in")))
        self.assertEqual(code, 0)
        self.assertIn("2 created", out.getvalue())
        self.assertEqual(len(sg.list_stories(self.tmp / "out")), 2)

    def test_errors(self):
        for over in ({}, {"input_dir": str(self.tmp / "missing")},
                     {"image": [str(self.storyboard())], "dry_run": False}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = sg.main_cli(self.args(**over))
            self.assertEqual(code, 2, over)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(sg.main(["--voice", "bad id", "--image", "x.png"]), 2)

    def test_retry_failed_flag(self):
        bad = self.storyboard("bad.png")
        sg.log_failure(self.tmp / "out", bad, "RuntimeError: boom", {})
        with redirect_stdout(io.StringIO()) as out:
            code = sg.main_cli(self.args(retry_failed=True))
        self.assertEqual(code, 0)
        self.assertIn("1 created", out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sg.main_cli(self.args(retry_failed=True)), 0)
        self.assertIn("nothing to retry", out.getvalue())

    def test_cli_reports_failures_and_how_to_retry(self):
        self.storyboard()
        with mock.patch.object(sg, "generate_story", side_effect=RuntimeError("boom")), \
                redirect_stdout(io.StringIO()) as out:
            code = sg.main_cli(self.args(input_dir=str(self.tmp / "in")))
        self.assertEqual(code, 1)
        self.assertIn("failed: sb.png: RuntimeError: boom", out.getvalue())
        self.assertIn("--retry-failed", out.getvalue())

    def test_style_and_duration_flags(self):
        settings = sg.settings_from_args(self.args(style="connective", duration="1:30"))
        self.assertEqual((settings["style"], settings["target_s"]), ("connective", 90.0))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(sg.main_cli(self.args(image=["x.png"], duration="abc")), 2)

    def test_list_and_voices_flag(self):
        self.storyboard()
        with redirect_stdout(io.StringIO()):
            sg.main_cli(self.args(image=[str(self.tmp / "in" / "sb.png")]))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sg.main_cli(self.args(list=True)), 0)
        self.assertIn("Teste sb", out.getvalue())
        settings = sg.settings_from_args(self.args(voice=[f"{VOICE_A}=x"]))
        self.assertEqual(settings["voices"], [{"id": VOICE_A, "label": "x"}])

    def test_config_migrates_old_direct_fish_model_names(self):
        self.assertEqual(sg.sanitize_config({"tts_model": "s2.1-pro"})["tts_model"],
                         "fish-audio/s2.1-pro")
        self.assertEqual(sg.sanitize_config({"tts_model": "s2.1-pro-free"})["tts_model"],
                         sg.DEFAULT_TTS_MODEL)
        self.assertEqual(sg.sanitize_config({"tts_model": "drama-3-preview"})["tts_model"],
                         sg.DEFAULT_TTS_MODEL)

    def test_cli_list_log_filter_sort(self):
        for name in ("a.png", "b.png"):
            self.storyboard(name)
        with redirect_stdout(io.StringIO()):
            sg.main_cli(self.args(input_dir=str(self.tmp / "in")))
        sg.log_failure(self.tmp / "out", self.tmp / "in" / "c.png", "boom", {"style": "connective"})
        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(sg.main(["--list-log", "--output-dir", str(self.tmp / "out"),
                                      "--log-filter", "status=ok", "--log-sort", "title"]), 0)
        rows = list(csv.DictReader(io.StringIO(buf.getvalue())))
        self.assertEqual([r["title"] for r in rows], ["Teste a", "Teste b"])
        with redirect_stdout(io.StringIO()) as buf:
            sg.main(["--list-log", "--output-dir", str(self.tmp / "out"),
                     "--log-filter", "style=connective"])
        self.assertEqual([r["status"] for r in csv.DictReader(io.StringIO(buf.getvalue()))],
                         ["error"])

    def test_config_roundtrip_and_sanitize(self):
        sg.save_config({"voices": [{"id": VOICE_A, "label": "n"}], "tts_model": "nope",
                        "output_dir": "/x", "junk": 1})
        clean = sg.sanitize_config(sg.load_config())
        self.assertEqual(clean["voices"], [{"id": VOICE_A, "label": "n"}])
        self.assertEqual(clean["tts_model"], sg.DEFAULT_TTS_MODEL)
        self.assertNotIn("junk", sg.load_config())


if __name__ == "__main__":
    unittest.main()
