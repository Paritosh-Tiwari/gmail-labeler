# QuickLabel — developer notes

User-facing setup, install, and daily-use docs live in the
[top-level README](../../README.md). This file is for people reading or
modifying the code.

## What it does

QuickLabel is a local FastAPI app (binds `127.0.0.1:8765`) that turns
one click on a draggable bookmarklet into a labeled+filtered email.
Three user-visible surfaces, all sharing the same proposal pipeline:

1. **`/label?id=<thread-id>`** — bookmarklet target. Reads one email,
   proposes a label name + Gmail filter rule via local LLM, lets the
   user confirm, then creates label, creates filter, and back-fills
   the label across every existing matching email.
2. **`/queue`** — batch review of recent inbox senders. Uses fast
   offline heuristics by default; a per-row "Regenerate with AI"
   button swaps in the LLM proposal for tricky cases.
3. **`/describe`** — plain-English rule input ("any sign-in
   notifications go under Security/Sign-In and skip the inbox"). LLM
   designs the filter, sample matches preview against the live inbox,
   user iterates via a Refine textbox, then applies.

## Why this shape (bookmarklet + localhost server)

A bookmarklet plus a local server was chosen over a browser
extension because:

- **No silent auto-updates.** A bookmarklet is one line of JavaScript
  the user can read; the local Python server is the same code forever
  unless they `git pull` or re-run the installer.
- **Narrow attack surface.** The server binds to `127.0.0.1` only.
  The bookmarklet only runs when the user clicks it, only reads the
  URL of the page they're on, and only talks to localhost.
- **Easy to audit.** One Python package, one bookmarklet line, no
  dependency on a Chrome Web Store binary.

Trade-off: no mobile (extensions don't run on mobile Gmail either,
so we're not losing anything there).

| | Browser extension | QuickLabel (bookmarklet + localhost) |
|---|---|---|
| Runs without being asked? | Continuously, on every Gmail page | Only when you click the bookmark |
| Talks to internet? | Anywhere | Only `127.0.0.1` |
| Auto-updates? | Yes, silently via store | No — only when you re-run the installer |
| Code auditable? | Hard (compiled bundle) | Easy — one Python package + one line of JS |
| Compromise blast radius? | All users instantly | Just you |

## Security model

The defenses inside the server (independent of the localhost bind):

- **`HostValidationMiddleware`** — rejects requests whose `Host`
  header isn't `127.0.0.1` / `localhost` / `[::1]`. Defeats
  DNS-rebinding attacks where a malicious site resolves a domain
  to `127.0.0.1` and tricks the browser into POSTing here.
- **Per-startup CSRF nonce** — embedded in every proposal page,
  required on POSTs. Other local processes can't blindly trigger
  label changes.
- **`AuditLogMiddleware`** — every request is logged to SQLite with
  Host, path, method, status; `/audit` page shows recent activity so
  unexpected traffic is visible.
- **`CSPHeaderMiddleware`** — strict Content-Security-Policy on all
  responses; blocks inline scripts from third parties even if a
  template were ever compromised.
- **OAuth refresh token in OS keyring** — never on disk in plaintext.
  Windows Credential Manager / macOS Keychain.
- **Loopback-only bind** — `settings.HOST = "127.0.0.1"`; widening
  this requires also widening `HostValidationMiddleware`'s allow-list
  or the rebinding defense is broken (there's a comment in
  `settings.py` warning about this).

## OAuth scopes

QuickLabel requests:

- `gmail.modify` — read messages, add/remove labels, `batchModify`
  (back-propagation).
- `gmail.settings.basic` — create/list/delete server-side filters.

It does **not** request: `gmail.send`, `gmail.compose`, account-level
delegation, or anything that could send mail. See `auth.py` for the
exact scope list.

## Architecture

```
src/quicklabel/
  __main__.py        CLI entry: serve / setup / bookmark / tray.
                     Handles pythonw stdio shim for hidden auto-start.
  server.py          FastAPI app, 25+ endpoints, middleware stack.
                     /label, /queue, /describe, /apply, /settings,
                     /audit, /history, /setup, /healthz.
  middleware.py      HostValidation + AuditLog + CSP middlewares.
  auth.py            OAuth flow + token refresh + setup_state().
  token_store.py     OS keyring wrapper (Credential Manager / Keychain).
  settings.py        Settings dataclass + JSON+env+default resolution.
  storage.py         SQLite store: apply_log, intel_cache, audit_log,
                     versioned migration runner.

  # The proposal pipeline (used by all three surfaces)
  resolve.py         Gmail URL fragment / thread id / message id ->
                     concrete message dict.
  headers.py         Pure email header parsing (From, List-ID, ...).
  body.py            MIME walker — extracts text body from any part.
  signals.py         Cheap content flags (has-unsubscribe, has-OTP, ...).
  sender_stats.py    Gmail-API sender history: count + sample subjects.
  scan.py            Inbox scan -> heuristic proposal list (no LLM).

  # Two proposers — heuristic + LLM
  smart_heuristic.py No-LLM proposer used by /queue rows.
  intelligence.py    LLM proposer + system prompt (filter-design
                     framework) + build_describe_prompt for /describe.
                     Has qwen3 /no_think shim and retry-once on
                     unparseable JSON.
  llm_status.py      installed_models() / probe_model() / is_model_installed
                     for /settings page.

  # Apply path (proposal -> Gmail mutations)
  proposal.py        Pure proposal dataclasses + filter assembly.
  actions.py         ActionChoices -> Gmail label-mutation list.
  preflight.py       find_filter_conflicts (auto-merge into existing
                     filters) + destructive-action detection.
  service.py         ensure_label, create_label_filter,
                     extend_filter_with_label, merge_filter_action,
                     backprop_label.

  # Misc
  bookmarklet.py     Generates the `javascript:` URI for the dragged
                     link on the landing page.
  gmail_id.py        Parses thread/message IDs out of Gmail URLs.
  tray.py            Optional pystray app (opt-in via setup-tray).
  _logging.py        RotatingFileHandler at data/quicklabel.log.

  templates/         Jinja2: landing, proposal, queue, describe,
                     describe_proposal, settings, setup, audit,
                     history, applied, confirm, undone, base, and
                     two reusable partials (_actions, _scope).
```

## Running locally

For end-to-end setup (Ollama install, OAuth client, system tray),
follow the top-level README's installer flow — `setup.ps1` on
Windows, `setup.command` on Mac. The installer's idempotent; safe to
re-run after pulling.

For code-only iteration (you already have data/settings.json and a
token):

```powershell
# Windows
.\.venv\Scripts\python.exe -m quicklabel serve

# Mac
./.venv/bin/python -m quicklabel serve
```

Restart is required after Python changes (`uvicorn` doesn't reload
Python). Jinja templates DO reload per-request.

## Tests

```powershell
# Unit tests (all of them, fast — no Gmail or Ollama needed)
.\.venv\Scripts\python.exe -m pytest tests/ -q

# E2E smoke scripts (server + Gmail + Ollama must be running)
.\.venv\Scripts\python.exe scripts\quicklabel_filter_smoke.py
.\.venv\Scripts\python.exe scripts\quicklabel_intel_smoke.py
.\.venv\Scripts\python.exe scripts\quicklabel_e2e.py
.\.venv\Scripts\python.exe scripts\quicklabel_queue_e2e.py
```

The unit suite covers headers, body parsing, signals, proposal
assembly, filter query rendering, label-status detection, security
middleware, undo, preflight conflict detection, smart-heuristic
proposer, LLM-status probing, and the LLM proposer's prompt-shaping
+ JSON-parse paths (with mocked Ollama).

The E2E scripts hit real Gmail and a real local model — useful for
verifying API contracts after a Gmail-side change or a model swap.

## Recurring gotchas

- **`pythonw.exe` has `None` stdio** on Windows hidden auto-start.
  `__main__.py` redirects `sys.stdout`/`sys.stderr` to `os.devnull`
  before uvicorn starts, otherwise the server crashes the first time
  it tries to log.
- **CRLF on `.command` files breaks Mac shebangs.** `.gitattributes`
  forces LF on `*.command` and `*.sh`.
- **Bookmarklet must be re-dragged after JS changes**, because the
  bookmarklet text is generated at install time and stored in the
  browser bookmark.
- **Per-startup CSRF nonce** means forms opened before a restart
  return 403 on submit. The fix is to reload the proposal page.
- **Don't name helpers `test_*` or `Test*` in `src/`** — pytest
  auto-collects them and treats parameters as fixtures. Use
  `probe_*` / `verify_*` / `try_*` instead.

## Status

Personal project. No commercial release planned. MIT-licensed. Fork
freely.
