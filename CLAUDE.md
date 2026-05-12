# QuickLabel — Claude Code working notes

A privacy-preserving Gmail labeling assistant: bookmarklet -> local FastAPI
server (`127.0.0.1:8765`) -> proposal page powered by a local Ollama LLM.
Everything stays on the user's machine.

## How to run + develop

QuickLabel is installed editable (`pip install -e .` via `setup.ps1` /
`setup.command`). After install:

- `python -m quicklabel serve` works without `PYTHONPATH=src`
- The server normally auto-starts at login via Task Scheduler (Win) or
  launchd (Mac) — the user shouldn't need to run it manually
- After ANY Python edit: `.\restart.ps1` (Win) or `./restart.command` (Mac).
  Jinja templates auto-reload on every request, but uvicorn does NOT re-import
  Python modules — endpoints, middleware, and business logic stay frozen
  at startup until you restart. **Spell out the restart step at the end of
  every reply that edits Python code.**

Tests:

```
pytest tests/ -q
```

355+ tests, all green. Live scripts (need a running server + Gmail) are at
`scripts/quicklabel_*.py`.

Manual QA cases the unit suite can't cover live at
`docs/quicklabel-manual-qa.md`.

## Architecture invariants — don't accidentally violate

- **Single SQLite DB** at `data/quicklabel.db` with a versioned
  `schema_migrations` table. New schema changes = APPEND a new tuple to
  `_MIGRATIONS` in `storage.py`. Never edit a shipped migration. Legacy
  pre-runner DBs back-fill to v1 automatically.
- **OAuth refresh token in OS keyring** (`token_store.py`), never on disk.
  Legacy `data/quicklabel_token.json` is auto-migrated on first load.
- **Per-startup `NONCE`** on every POST. Stale forms get 403 after restart;
  user must reload the page.
- **`HostValidationMiddleware`** rejects non-localhost Host headers (returns
  421). Defeats DNS rebinding. Don't add bypasses.
- **`SetupRedirectMiddleware`** routes everything except `/setup*` and
  `/healthz` to the wizard until OAuth is complete. Don't bypass with
  application-level "if creds are missing" checks — let the middleware
  handle it.
- **Middleware order** (inner → outer):
  HostValidation → SetupRedirect → AuditLog → CSP. Add new middleware
  thoughtfully relative to this stack.
- **LLM defaults**: `gpt-oss:20b`, `num_ctx=16384`, `temperature=0`.
  Override in `/settings` (writes `data/settings.json`). Env vars
  (`QUICKLABEL_*`) override the file at launch.
- **Logs** rotate at `data/quicklabel.log` (5 MB × 3 backups).

## Five common tasks + how to do them

1. **Add a Gmail action** (e.g. snooze, send to category):
   `actions.py` (extend `ActionChoices`) → `intelligence.py` (LLM prompt
   examples + JSON parser) → `service.py` (Gmail API call) → templates
   (`_actions.html`, confirm/applied views) → `apply_log` schema if you
   need a new field for undo → undo path in `/history/undo`. Tests at
   `tests/test_quicklabel_actions.py`.

2. **Tweak the LLM prompt**: `intelligence.py` `_SYSTEM_PROMPT` and
   `_build_user_prompt`. After change, `python scripts/quicklabel_intel_smoke.py`
   exercises it against a few sample emails. The cache is keyed on the
   email — call `clear_intel(_cache_key(email, body))` (or use
   `/label/regenerate`) to bust it.

3. **Debug a queue row** (proposal looks wrong): pull `proposal_log` row,
   matching `audit_log` requests, then reconstruct the LLM input with the
   email's actual `EmailFingerprint` + body. The `/proposal/{id}/full`
   endpoint redirects to `/label?id=...` which shows the AI details
   inline (prompt, raw response, model, cache key). For programmatic
   debugging, use the `quicklabel-debug-proposal` agent.

4. **New schema column**: append a `(version, name, sql)` tuple to
   `_MIGRATIONS` in `storage.py`. SQL should be `ALTER TABLE ...` (column
   adds) or `CREATE TABLE IF NOT EXISTS ...` (new tables). Idempotent
   migrations are safer.

5. **New /settings field**: extend `Settings` dataclass + `load_settings`
   precedence + `settings.html` form fields + `POST /settings` validation.
   Keep env-var override pattern intact for parity.

## Gotchas (will burn you otherwise)

- **Restart required after Python edits** — the #1 most-hit footgun. End
  every reply that touches `*.py` with the restart command.
- **Bookmarklet stores a JS snapshot** — re-drag it from the landing page
  after `bookmarklet.py` changes.
- **Ollama's default `num_ctx=2048` is too small.** Returns empty content
  for QuickLabel-shaped prompts. We override to 16384.
- **`gpt-oss:20b` first call is ~30s** while the model loads into VRAM.
  Don't add aggressive timeouts.
- **Port 8765 collisions** — set `$env:QUICKLABEL_PORT=8766` to avoid
  conflict with the user's running server when spawning a test server.
- **Pytest auto-collects `test_*` functions and `Test*` classes** —
  helper functions in `src/quicklabel/` named `test_*` will be picked up
  as tests if a test file imports them. Use `probe_*` / `verify_*` /
  `try_*` instead.
- **CRLF on `.command` files breaks Mac shebangs.** `.gitattributes`
  already forces LF on `*.command` and `*.sh`. Don't override.

## Where things live

```
src/quicklabel/
  server.py            FastAPI app + 20+ endpoints + middleware wiring
  middleware.py        HostValidation, SetupRedirect, AuditLog, CSP
  auth.py              OAuth flow + setup_state() + authorize_interactive()
  token_store.py       OS keyring wrapper for OAuth token
  storage.py           SQLite layer (5 tables) + versioned migration runner
  settings.py          settings.json + env override + Settings dataclass
  llm_status.py        installed_models() / probe_model() for /settings UI
  intelligence.py      LLM proposer (gpt-oss:20b default)
  smart_heuristic.py   No-LLM fast proposer for the queue scan path
  preflight.py         Filter conflict + destructive-action detection
  service.py           Gmail API ops (ensure_label, create/delete filter, backprop)
  scan.py              Inbox scan -> proposals
  body.py              MIME walker
  signals.py           Cheap content flags (has_unsubscribe, looks_marketing)
  actions.py           ActionChoices dataclass + to_label_mutations
  bookmarklet.py       JS bookmarklet generator
  resolve.py           Resolve any of (api_thread_id | api_msg_id | permalink) -> message
  templates/           Jinja2: landing, proposal, queue, setup, settings, audit, history
  _logging.py          RotatingFileHandler setup
```

## Skills + agents shipped with this branch

This is the `claude-code` flavor of the repo. The `master` branch ships
the same code without `.claude/` for users who don't use Claude Code.

Agents available (auto-invoked by Claude when relevant):

- `quicklabel-qa` — runs the test suite then walks the manual QA checklist
- `quicklabel-debug-proposal` — given a sender email or proposal_id,
  pulls the row + audit context + reconstructs the LLM input
- `quicklabel-add-action` — guided walkthrough of adding a Gmail action
  end-to-end (all 6 touch points)
