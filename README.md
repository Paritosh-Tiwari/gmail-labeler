# QuickLabel

A privacy-preserving Gmail labeling assistant that runs entirely on your
own machine. Click a bookmarklet while reading an email; QuickLabel uses
a local LLM to propose a label name + Gmail filter rule, applies them
on confirmation, and back-fills the label across every existing
matching email.

Nothing leaves your computer except the unavoidable Gmail API calls.

## Two flavors — pick one before you download

QuickLabel ships in two branches with identical core code:

- **`master` (default)** — Vanilla install. Use this if you don't use
  [Claude Code](https://claude.com/claude-code).
- **`claude-code`** — Same code plus a `.claude/` directory with
  sub-agents (`quicklabel-qa`, `quicklabel-debug-proposal`,
  `quicklabel-add-action`) and a `CLAUDE.md` working-notes file.
  Useful if you want to extend QuickLabel and want Claude Code to
  understand the codebase invariants without a long onboarding.

Updates land on `master` first; `claude-code` periodically merges from
`master`. Either branch can be re-installed in place at any time without
losing data — the install scripts only touch code files, not `data/`.

## Quick install (Windows)

You need Windows 10/11 with PowerShell. The installer takes care of
the rest.

1. Download the latest release ZIP from the Releases page (or `git clone`
   this repo if you have git).
2. Extract it to a folder you'll keep -- e.g. `C:\Users\you\QuickLabel`.
   Avoid `Documents\` if it's auto-synced to OneDrive (the `data/`
   folder gets large; you don't want it cloud-synced).
3. Open the folder. Right-click `setup.ps1`, choose **"Run with
   PowerShell"**.
4. Follow the prompts. The installer will:
   - Check for Python 3.11+ (offers to install via `winget` if missing)
   - Create a project-local `.venv` and install Python dependencies
   - Check for Ollama (offers to install via `winget` if missing)
   - Ask which local LLM you want, then download it (multi-GB)
   - Register an auto-start task so the server runs every time you log in
   - Open the landing page in your browser

If Windows shows a SmartScreen warning ("Windows protected your PC"),
click "More info" -> "Run anyway". The installer is unsigned -- a
signing certificate costs money and isn't worth it for a personal tool.

The installer is **idempotent** -- you can re-run it any time to update
deps or change the LLM.

## After installing

You'll land on `http://127.0.0.1:8765`. From there:

1. **Drag the "Label this" link** onto your bookmark bar (Chrome strips
   `javascript:` from address-bar pastes -- drag is the only install).
2. Open any email in Gmail.
3. Click the bookmark. A new tab opens with a proposal.
4. Tweak / confirm. Done.

You'll need to authorize Gmail the first time you do this -- you'll be
prompted for OAuth in your browser. (See [Google Cloud setup](#one-time-google-cloud-setup)
below for the prerequisite.)

## Daily lifecycle

The server auto-starts every time you log in. You shouldn't need to
think about it. But when you do:

```powershell
.\start.ps1     # start the server now
.\stop.ps1      # stop it (auto-start at next login is unaffected)
.\restart.ps1   # stop + start (use after editing Python code)
.\uninstall.ps1 # remove the auto-start (keeps your data + token)
```

## Optional: system-tray icon

A small status icon in the notification area (Win) / menu bar (Mac).
Polls `/healthz` and shows green / red / amber. Click for a menu:
open queue / settings / audit, open data folder, restart server, stop
server, quit tray.

```powershell
# Windows
.\setup-tray.ps1
.\uninstall-tray.ps1   # if you change your mind
```

```sh
# Mac
./setup-tray.command
./uninstall-tray.command
```

The tray runs as its own auto-start (separate from the server) so
killing the tray doesn't kill the server, and crashing the tray
doesn't take the server with it.

## Settings

`http://127.0.0.1:8765/settings` -- LLM model, port, log verbosity.
Stored in `data/settings.json`. Restart for port and log-level changes
to take effect.

## Updating

For now, manual: pull/extract a newer release into the same folder, then
re-run `setup.ps1`. (A one-click `update.ps1` is on the roadmap.)

## Privacy + data

Everything is local:

- **Server** binds to `127.0.0.1` only (rejects requests with non-localhost
  Host headers to defeat DNS rebinding).
- **OAuth refresh token** lives in Windows Credential Manager (keyring),
  not on disk.
- **LLM proposals** run on a local Ollama model. No cloud LLM.
- **History** -- proposals, applies, undo log, and HTTP audit log live
  in `data/quicklabel.db` (SQLite, local).
- **Logs** at `data/quicklabel.log` (rotating, 5 MB x 3 backups, ~20 MB cap).

The `data/` folder is the only thing you need to back up. Deleting it
is a clean factory reset.

## One-time Google Cloud setup

You need your own OAuth client (each user makes their own; nothing is
shared with anyone). The first time you visit `/setup` (after
installing), QuickLabel walks you through it. Briefly:

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the Gmail API
3. Configure the OAuth consent screen (External, add yourself as a test user)
4. Create OAuth client credentials (Desktop app)
5. Download the credentials JSON
6. Drop it onto QuickLabel's `/setup` page

The wizard handles the rest. (Full guided wizard ships with Phase 2 of
the roadmap; for now there's [docs/google-cloud-setup.md](docs/google-cloud-setup.md).)

## Troubleshooting

**Server isn't running after login.**
Check Task Scheduler (`taskschd.msc`) -> look for the `QuickLabel` task.
If it's missing, re-run `setup.ps1`. If it's there but not running, run
`.\start.ps1` and check `data/quicklabel.log` for the error.

**Port collision (something else uses 8765).**
Open `/settings`, change the port, restart with `.\restart.ps1`.

**LLM proposals are slow / empty.**
Check `data/quicklabel.log` for the model name being used. First call
to a freshly-loaded model is ~30 s while it loads into VRAM; subsequent
calls are 5--10 s. If you see "empty LLM response", your model is too
big for your VRAM -- switch to a smaller one in `/settings`.

**Bookmarklet doesn't work.**
The bookmarklet contains a snapshot of the JS at install time. After
updating QuickLabel, re-drag the bookmarklet from `http://127.0.0.1:8765`.

## For developers

See [src/quicklabel/README.md](src/quicklabel/README.md) for architecture
notes, test setup, and contributing guidelines. Tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

## Status

Personal project. No commercial release planned. Fork freely.
