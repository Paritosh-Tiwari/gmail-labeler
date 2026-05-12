# QuickLabel

A privacy-preserving Gmail labeling assistant. Click a bookmarklet
while reading an email; QuickLabel proposes a label name and a Gmail
filter rule, applies them on confirmation, and back-propagates the
label to every existing matching email.

## What it does

When you click the bookmarklet inside Gmail:

1. The bookmarklet extracts the thread ID from the page URL and opens
   `http://127.0.0.1:8765/label?id=<thread-id>` in a new tab.
2. QuickLabel reads the email via the Gmail API, inspects headers
   (From, List-ID, List-Unsubscribe, Subject), and queries your
   inbox for sender history.
3. It proposes:
   - **A label name** (derived from sender display name or domain).
   - **A filter rule** (most precise: `list:<list-id>` if present;
     else `from:<sender>` with optional subject narrowing).
   - **A back-prop count** (how many existing emails will be labeled).
4. You tweak / confirm. QuickLabel:
   - Creates the label (and parent if nested, e.g. `AI Newsletters/Sequoia`).
   - Creates a Gmail server-side filter (so future mail is auto-labeled).
   - Back-propagates the label to all existing matches.

## Why this design

Most Gmail filter pain is UX, not capability. The native flow takes
6+ clicks per filter, hides settings three layers deep, can't be done
on mobile, and gives no help picking *which* criteria to use. QuickLabel
collapses it to one click + confirm, with smart defaults.

The bookmarklet+localhost shape was chosen over a browser extension
because:

- **No silent auto-updates.** A bookmarklet is one line of JavaScript
  you can read; the local Python server is the same code forever
  unless you `git pull`.
- **Narrow attack surface.** The server binds to `127.0.0.1` only.
  The bookmarklet only runs when *you* click it, only reads the URL
  of the page you're on, and only talks to your machine.
- **Easy to audit.** One Python package, one bookmarklet line, no
  dependency on a Chrome Web Store binary.

The trade-off: no mobile (extensions don't run on mobile Gmail
either, so we're not losing anything there).

## Setup

Prerequisites:

- The parent `gmail-labeler` project's `credentials.json` already in place
  (see `docs/google-cloud-setup.md`).
- Python 3.12, dependencies installed (`pip install -r requirements.txt`).

One-time:

```
PYTHONPATH=src .venv/Scripts/python.exe -m quicklabel setup
```

This will:

1. Open a browser for OAuth (grants `gmail.modify` + `gmail.settings.basic`).
2. Save the token to `data/quicklabel_token.json` (separate from the
   parent project's `token.json` so its narrow scope is preserved).
3. Print the bookmarklet text — drag it to your bookmark bar.

Then, every time you want to use it:

```
PYTHONPATH=src .venv/Scripts/python.exe -m quicklabel serve
```

## Daily use

1. Open any email in Gmail.
2. Click the **Label this** bookmarklet.
3. New tab opens with a proposal — review, tweak, click **Apply**.
4. Done.

## Security model

| | Browser extension | QuickLabel (bookmarklet + localhost) |
|---|---|---|
| Runs without being asked? | Continuously, on every Gmail page | Only when you click the bookmark |
| Talks to internet? | Anywhere | Only `127.0.0.1` |
| Auto-updates? | Yes, silently via store | No — only when you `git pull` |
| Code auditable? | Hard (compiled bundle) | Easy — one Python package + one line of JS |
| Compromise blast radius? | All users instantly | Just you |

The local server requires a per-startup nonce (embedded in the proposal
page) on POST requests, so other local processes cannot blindly trigger
label changes.

## OAuth scopes

QuickLabel requests:

- `gmail.modify` — read messages, add/remove labels, batchModify (back-prop).
- `gmail.settings.basic` — create/list/delete server-side filters.

It does **not** request: `gmail.send`, `gmail.compose`, account-level
delegation, or anything that could send mail.

## Architecture

```
src/quicklabel/
  __main__.py        CLI: serve / setup / bookmark
  server.py          FastAPI app, binds 127.0.0.1
  auth.py            OAuth (separate token from parent project)
  resolve.py         Gmail URL fragment -> message dict
  headers.py         Pure header parsing
  proposal.py        Pure proposal engine (filter + label + questions)
  sender_stats.py    Hits Gmail to count + sample subjects
  service.py         ensure_label, create_filter, back-prop
  bookmarklet.py     Generates the javascript: URI
  templates/         Jinja2: landing, proposal, applied
  settings.py        HOST/PORT
```

Pure functions (proposal, headers, bookmarklet) have unit tests.
Gmail-touching code (sender_stats, service, server endpoints) is
verified by `scripts/quicklabel_e2e.py`.

## Tests

```
.venv/Scripts/python.exe -m pytest tests/                       # unit tests
.venv/Scripts/python.exe scripts/quicklabel_filter_smoke.py     # filter CRUD smoke
.venv/Scripts/python.exe scripts/quicklabel_e2e.py              # full flow (server must be running)
```

## Limitations (this is a tech demo)

- Desktop only (no mobile Gmail support).
- Not packaged for distribution — clone + run.
- No multi-account support (uses one Gmail account).
- No filter editing UI (use Gmail's native settings page for now).
- No undo (label removal is one Gmail search away though).
