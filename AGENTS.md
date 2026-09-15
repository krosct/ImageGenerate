# AGENTS.md — ImageGenerate

Single-file project: `image_generate.py` (stdlib + `cryptography` only).
Multi-provider AI image generator with Tkinter GUI (tabs Generate/Model/Dir +
conditional Injection tab), a React+FastAPI web UI (same tabs) and a fully
equivalent CLI. Responses in pt-BR; code, comments and identifiers in English.

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
```

## Architecture map (`image_generate.py`)

| Area | Functions / notes |
|---|---|
| Provider registry | `PROVIDERS`, `normalize_provider`, `REQUEST_FUNCS` — one entry + one `request_*` function per provider, same `(payload, start_ts, elapsed)` contract |
| HTTP (cancellable) | `_post_json` via `http.client`, `abort_all_http()` (shutdown+close), `GenerationCancelled` |
| Core pipeline | `run_generation()` — image + summary threads in parallel, partial cleanup on cancel, appends CSV; `run_generation_batch()` runs one `count=1` generation per prompt (Injection mode) and aggregates results |
| Injection templating | `extract_template_vars` / `apply_template_values` / `resolve_injection_rows` (`{{name}}` placeholders; empty cell repeats the value above, first row falls back to the variable name); CLI `--inject NAME=VALUE,…` (one option per generation), GUI conditional yellow "Injection" tab, web `injection` field on `/api/generate` + yellow nav tab (`web/frontend/src/injection.ts`, `tabs/Injection.tsx`) |
| Key vault | `save/load/forget_remembered_key`, one Fernet key + one blob per provider in `vault_dir()` (`~/.config/image_generate/`), `key_hash()` (SHA-256/16, log only) |
| GUI config | `load/save/sanitize_gui_config` → `config.json` in the same dir; precedence: hard defaults < config file < explicit CLI flags |
| CSV log | `LOG_FIELDS`, `read/write/append_log_entries`; `#` comment lines on top hold totals; GUI `Treeview` columns are generated from `LOG_FIELDS` |
| GUI | `run_gui()` — Notebook tabs (Injection tab shown only when count > 1 and prompt has `{{vars}}`), spoiler log list, `on_generate`/`on_cancel`/`on_done`, hover `attach_help` tooltips, ratio-preview tooltip |
| Web | `web/server.py` (FastAPI, localhost only) — jobs + SSE, mirrors the core; frontend `web/frontend/src` (Vite/React, build with `npm run build` in `web/frontend`) |

## Conventions (must follow)

- **Stdlib only** (+ `cryptography` for the vault). No Pillow (image parsing is hand-rolled), no requests, no new dependency without justification.
- **No secrets in repo**: keys only via `--api-key`, env (`<PROVIDER>_API_KEY`) or vault. Never print/log full keys (`key_hash` only). `__pycache__/` is git-ignored.
- **GUI ↔ CLI parity**: every GUI action must be doable via CLI flags.
- **Small, surgical edits**; keep function contracts; `py_compile` + dry-run check after every change.
- **Cancel semantics**: abort sockets, delete partial files, log nothing, raise `GenerationCancelled`. Summary failures fall back to truncation (never swallow cancellation).
- Commits: one per cohesive change set, concise message. Never force-push, never touch git config.
