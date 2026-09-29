# AGENTS.md — ImageGenerate

Two single-file programs (stdlib + `cryptography` only):
- `image_generate.py` — multi-provider AI image generator with Tkinter GUI (tabs
  Generate/Model/Dir + conditional Injection tab), a React+FastAPI web UI (same
  tabs) and a fully equivalent CLI.
- `story_generate.py` — storyboard images → written story (OpenRouter vision
  writer) + narration (Fish Audio TTS **via OpenRouter** `/audio/speech`, writer
  picks 1 of up to 5 Fish voice ids; one OpenRouter key for both) →
  `<title>/` folders; Tkinter GUI (Generate/Model/Voices/Player) + CLI. Imports
  `image_generate` for HTTP/cancel/vault/helpers (no web UI yet).

Responses in pt-BR; code, comments and identifiers in English.

## Run / verify (always do this after edits)

```bash
python3 -m py_compile image_generate.py
python3 image_generate.py --help
# offline end-to-end (no cost, no key):
python3 image_generate.py --prompt "smoke test" --prop 1:1 --resolution 512 --dry-run
# injection end-to-end (offline):
python3 image_generate.py --prompt "a {{animal}}" --dry-run \
  --inject "animal=cat" --inject "animal=dog" --output-dir /tmp/ig-test
# GUI smoke (needs display): python3 image_generate.py --gui
# web frontend build: npm run build in web/frontend
# regression tests (always run the ones for modified/related areas):
python3 -m unittest discover -s tests -v
# targeted examples (-k is case-sensitive: pass both cases to cover
# CamelCase classes and snake_case methods of the changed area):
python3 -m unittest tests.test_image_generate -v -k Summar -k summary
python3 -m unittest tests.test_image_generate -v -k Injection -k injection
python3 -m unittest tests.test_web_server -v -k Generate -k generate
# story_generate (offline, no cost):
python3 -m py_compile story_generate.py && python3 story_generate.py --help
python3 story_generate.py --input-dir <dir with .png> --dry-run --output-dir /tmp/sg-test
python3 -m unittest tests.test_story_generate -v
```

## Architecture map (`image_generate.py`)

| Area | Functions / notes |
|---|---|
| Provider registry | `PROVIDERS` (`openrouter`/`gemini`/`openai`), `normalize_provider` (pt-BR errors, unknown slugs rejected), `MODEL_PREFIXES` + `_check_model_for_provider` (family prefixes, never an allowlist), `REQUEST_FUNCS` — one entry + one `request_*` function per provider, same `(payload, start_ts, elapsed)` contract; `payload["seeds"]` holds the effective seed per image |
| HTTP (cancellable) | `_post_json` via `http.client`, `_fetch_json` (GET, capability discovery), `abort_all_http()` (shutdown+close), `GenerationCancelled` |
| Model capabilities | `_model_capabilities` (cached per model, `refresh=True` forces refetch, intersection across endpoints), `_adapt_image_body` — drops unsupported params, clamps enums/ranges (`_closest_ratio`), resolution validated against enum; `_fan_out_requests` loops until `count` images received (seed+i per call); per-call API ceilings `OPENROUTER_MAX_N`/`OPENAI_MAX_N` (10) and `_n_limit_from_error` (router ZodError `too_big` on `n` → retry split); reactive retry once on capability-mismatch 400s; `ContentPolicyError` for content-filter 400s |
| Core pipeline | `run_generation()` — image + summary threads in parallel, partial cleanup on cancel, appends CSV; `run_generation_batch()` runs one `count=1` generation per prompt (Injection mode) and aggregates results |
| Dynamic dirs | `DYNAMIC_DIR_KEYS`, `parse_dynamic_start`/`parse_dynamic_range`/`parse_dynamic_batch`/`parse_dynamic_spec` (range or `{start, range, batch}`), `dynamic_dir_for` (`<base>/<start + (i // batch) % (range - start + 1)>`), `check_dynamic_dirs` (validates the folders actually used before any call, returns `(start, range, batch)`); `run_generation_batch(dynamic_dirs=…, on_progress=…)` runs one `count=1` call per generation (seed+i) and reports each finished one (GUI/web refresh the log live; web job `progress` over SSE); CLI `--dynamic-{output,context,memory} RANGE` + `--dynamic-…-start START` + `--dynamic-…-batch BATCH`, GUI/web "Dynamic" checkbox + Start + Range + Batch under each dir (enabled only when count > 1), web `dynamic_dirs` field on `/api/generate`; `nearest_existing_dir` powers the GUI Browse start folder |
| Injection templating | `extract_template_vars` / `apply_template_values` / `resolve_injection_rows` (`{{name}}` placeholders; empty cell repeats the value above, first row falls back to the variable name); CLI `--inject NAME=VALUE,…` (one option per generation), GUI conditional yellow "Injection" tab, web `injection` field on `/api/generate` + yellow nav tab (`web/frontend/src/injection.ts`, `tabs/Injection.tsx`) |
| Key vault | `save/load/forget_remembered_key`, one Fernet key + one blob per provider in `vault_dir()` (`~/.config/image_generate/`), `key_hash()` (SHA-256/16, log only) |
| GUI config | `load/save/sanitize_gui_config` → `config.json` in the same dir (also the last `prompt`; every key needs a CLI flag with the same dest); precedence: hard defaults < config file < explicit CLI flags |
| CSV log | `LOG_FIELDS` (includes effective `seed` per image), `read/write/append_log_entries`, `sort_log_rows_newest_first` / `collect_log_rows` (merge several logs, newest first — GUI Summary + web `/api/log?extra_dir=`); `#` comment lines on top hold totals; GUI `Treeview` columns are generated from `LOG_FIELDS` |
| Analyse | `list_folder_images` (newest first by mtime), `build_analysis_rows` (row N = N-th image of each folder, `None` when a folder is shorter; `newest_first=False` aligns from the oldest), `choose_images` (picks `{row: folder}` one per row → copies into `<chosen>/<timestamp>/` as `<row>_<folder>_<file>` + `report.md`/`report.csv`; `_log_index` also reads the logs of up to 2 parent folders), `make_thumbnail(path, size)` (cached PNG per size via convert/ffmpeg in `~/.cache/image_generate/thumbs`; GUI Zoom −/+ over `THUMB_ZOOM_LEVELS`; hover preview `bind_preview` → `preview_geometry` (biggest size on the wider free side of the pointer, ≤75% width / 85% height, never over the pointer, multiples of `PREVIEW_STEP`) → window built WITHDRAWN and mapped only when the image is ready and placed (no corner flash), closed on leave/click/wheel/re-render), CLI `--analyse DIR… [--choose ROW:COL…] [--oldest-first] [--chosen-dir]`; GUI tab Analyse (config keys `analyse`, `chosen_dir`; `default_chosen_dir` = `<parent of output dir>/chosen`; footer wraps at the live window width) |
| Log tables | `column_kind` / `sort_key` / `apply_table_view` (filters `{col: {values, contains}}` + sort, numbers/dates aware, empties last), `parse_log_sort` / `parse_log_filters`; `TreeTable` (both GUIs: heading click = sort ▲/▼, heading right-click = spreadsheet filter popup ▾, `set_items([{values, tags, payload, sort}])`, `payload_of(iid)` — never map rows by tree index), config key `log_sort`; CLI `--list-log --log-filter COL=TEXT --log-sort COL[:desc]` (story_generate has the same flags) |
| GUI | `run_gui()` — Notebook tabs (Injection tab shown only when count > 1 and prompt has `{{vars}}`), spoiler log list, `on_generate`/`on_cancel`/`on_done`, hover `attach_help` tooltips, ratio-preview tooltip |
| Web | `web/server.py` (FastAPI, localhost only) — jobs + SSE, mirrors the core; frontend `web/frontend/src` (Vite/React, build with `npm run build` in `web/frontend`) |

## Architecture map (`story_generate.py`)

| Area | Functions / notes |
|---|---|
| Key | `resolve_openrouter_key` only (same vault slot/env as image_generate) — writer and narrator share it; no Fish Audio key |
| Voices | `parse_voice_spec` (`ID[=label]`), `validate_voices` (1..5, unique), `fetch_voice_info` (public `GET api.fish.audio/model/{id}`, no key: title/description/tags/languages), `describe_voices` |
| Writer | `writer_instruction` (panels left→right, top→bottom; one scene per panel + transitions; JSON reply), `extract_json_object`, `normalize_story` (voice by id, else label/title, else first + reason), `write_story` (writer model may be a fallback chain `a;b;c` → `parse_model_chain`; `_write_story_once` per model = its 429 retries + one retry on bad JSON + on an empty reply / `finish_reason=length` without JSON / timeout the next attempt sends `reasoning.effort` one level lower (`effort_ladder`: catalog `supported_efforts` from `default_effort` down, `none` if not `mandatory`; `EFFORT_ORDER`), lowest level failing → next model; `story["writer_effort"]` + log column `writer_effort`; any failure → next model, cancel stops; `story["writer_model"]` = model used, `writer_fallbacks`), `script_markdown`; `check_writer_model` (public `/api/v1/models` catalog: every model of the chain must accept image input, else ValueError with vision suggestions, run once before a batch); `with_rate_limit_retry` (HTTP 429 → waits `RATE_LIMIT_WAITS_S`, cancellable; writer + narrator) |
| Narrator / WAV | `synthesize` (`POST openrouter.ai/api/v1/audio/speech`, `TTS_MODELS` = `fish-audio/*`, `voice` = Fish id, `response_format=pcm` → `pcm_params` reads `rate`/`channels` from Content-Type → `pcm_to_wav`; returns `(wav, X-Generation-Id)`), `_post_bytes` (returns headers; registered in `ig._HTTP_CONNS` → `ig.abort_all_http()` cancels it), `parse_wav`, `join_scene_audio` (per-scene clips + `SCENE_GAP_S`, returns scene start/end) |
| TTS cost | `:free` narration → 0 immediately; paid → `start_cost_fill` background thread per story (`lookup_generation_cost` polls `GET /api/v1/generation?id=`, stats lag ~10-15 s) → story.json + `update_log_tts_cost`; `run_story_batch` waits `wait_cost_fills` at the end; `backfill_free_costs` fixes old empty `:free` rows; `tts_generation_ids` kept in story.json |
| Writer controls | `WRITER_STYLES` (`descriptive` / `connective`, `style_label` / `style_from_label`), `parse_duration` (`90`, `1:30`, `2m`), `speech_rates` (chars/s per voice from past non-dry-run stories, `DEFAULT_CHARS_PER_S` fallback) → `length_words` per voice inside `writer_instruction` |
| Pipeline | `generate_story` (skip same SHA-256 **and same writer style** (`find_existing_story(out, sha, style)`, `story_style` = descriptive for old stories) unless `force`; builds in memory, writes to hidden `.story-*` temp dir, renames to `<title>` → cancel leaves nothing, logs nothing; `status(msg)` per step), `run_story_batch` (`on_progress` + `on_status`; per-image error → `log_failure` row `status=error` with the reason, batch goes on; cancel stops all), `failed_storyboards(out, style)` (per (image, style): last attempt failed, no story in that style → `--retry-failed` / GUI "Retry failed"), CSV `LOG_FIELDS` (`status`, `style`, `target_seconds`, `error`) / `append_log_entry` / `write_log_rows` under `_LOG_LOCK` |
| Delete | `delete_story(folder, audio_only)` — whole story folder or only audio.wav (original storyboard never touched), `gio trash` when available else delete; log row → status `deleted` / `no audio`; CLI `--delete FOLDER [--audio-only]`, GUI right-click on a log row |
| Player | `WavPlayer` (feeds raw PCM to `aplay`/`pw-play`/`ffplay` paced by the clock; pause/seek kill+restart the process; `command=` injectable for tests), `scene_at`, `format_clock`, `find_storyboard_image` (`preview.png` via ffmpeg/convert for non-PNG storyboards: Tk has no JPEG) |
| GUI / CLI | Generate-tab log column `storyboard_dir` (folder name first, from `source_image`; also in Copy row) and Player list `story_list_label` (list | image | script share one `ttk.PanedWindow`, all dividers draggable; log footers pack the buttons first and wrap the totals text to the free width) ("<title> - <storyboards dir name> - <Narrativo|Descritivo>", `style_short_label`); `run_gui()` header `?` button → `docs.html#story` (same Help.TButton as image_generate); tabs Generate/Model/Voices/Player (script highlight + click-to-seek, keys space/←/→/↑/↓), `story_config.json` in the vault dir; CLI parity: `--input-dir/--image`, `--voice`, `--check-voices`, `--list`, `--play FOLDER`, `--style`, `--duration`, `--retry-failed`, `--remember-key/--forget-key` (OpenRouter key); GUI Generate tab: Writer + Duration, blue status line, red failed rows (double-click / right-click → retry), "Retry failed (n)", columns auto-fit + horizontal scroll, per-cell tooltip when text does not fit; context menus use `tk_popup` WITHOUT `grab_release()` (on X11 tk_popup returns at once; releasing the grab leaves the menu stuck — same in image_generate) |

## Conventions (must follow)

- **Stdlib only** (+ `cryptography` for the vault). No Pillow (image parsing is hand-rolled), no requests, no new dependency without justification.
- **No secrets in repo**: keys only via `--api-key`, env (`<PROVIDER>_API_KEY`) or vault. Never print/log full keys (`key_hash` only). `__pycache__/` is git-ignored.
- **GUI ↔ CLI parity**: every GUI action must be doable via CLI flags.
- **Small, surgical edits**; keep function contracts; `py_compile` + dry-run check after every change.
- **Regression tests**: after every change, run the tests for the modified/related areas (map Area → test `-k` keyword in the table above, e.g. Injection → `-k injection`, summary → `-k summary`, CSV log → `-k log`, web → `tests.test_web_server`); run the full suite (`discover -s tests`) when touching `run_generation`, provider contracts, or shared helpers.
- **Cancel semantics**: abort sockets, delete partial files, log nothing, raise `GenerationCancelled`. Summary failures fall back to truncation (never swallow cancellation).
- Commits: one per cohesive change set, concise message. Never force-push, never touch git config.
