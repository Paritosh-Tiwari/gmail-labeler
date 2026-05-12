# QuickLabel — End-to-end test runbook

How to validate a fresh QuickLabel install end-to-end without uninstalling
anything from your dev machine and without redoing the 10-minute Google
Cloud Console setup.

**Time:** ~15 min setup + ~30 min walking the manual QA checklist.
**Approach:** Fresh local Windows user account (works on Win 11 Home,
no VM needed).
**Preserves:** your existing dev environment, OAuth client + token.
**Tests:** install scripts, auto-start at login, full in-product UX.

---

## Why a fresh user account (not a VM, not the same user)

- VirtualBox / VMware needs a Windows ISO + ~30 GB disk + 30 min setup.
- Windows Sandbox needs Pro/Enterprise — not available on Win 11 Home.
- A fresh Windows local user gives you a clean `%USERPROFILE%`, no
  Python, no Ollama, no Credential Manager entries, no Task Scheduler
  entries. Effectively a "fresh OS" simulation for everything QuickLabel
  touches, without the VM overhead.
- Your dev install keeps running on your main user; deleting `qatest`
  cleans up everything (its Task Scheduler entries run as that user
  and die with it).

---

## Prereqs (do this once, on your dev install)

Export your existing OAuth token to a file so you can carry it into
the test environment and skip even the 5-second "click Allow" step:

```powershell
# Run in your dev install (the one with the working server):
.\.venv\Scripts\python.exe -c "from quicklabel.token_store import load_token; from pathlib import Path; Path('data/quicklabel_token.json').write_text(load_token())"
```

This produces `data/quicklabel_token.json` containing your refresh
token. Keep it private — it's your Gmail access token in plaintext.
You'll move it into the test install and the keyring migration code
will absorb it back into Credential Manager on first server start.

You also need to know where your `credentials.json` lives. In the dev
install it's at the project root:

```
C:\Users\<you>\QuickLabel\credentials.json
```

---

## Per-test setup

### 1. Create a fresh local user

1. Settings → Accounts → **Other users** → **Add account**
2. Click "I don't have this person's sign-in info"
3. Click "Add a user without a Microsoft account"
4. Username: `qatest` (or anything you'll remember). Password optional —
   skipping it means one less click each time you switch.
5. Don't grant admin rights — non-developer friends won't have admin,
   so this matches the realistic case.

### 2. Stage the files where qatest can read them

Both Windows users can read `C:\Users\Public\`. Stage the files there:

```powershell
# Run as your dev user:
mkdir C:\Users\Public\quicklabel-test 2>$null
copy C:\Users\<you>\QuickLabel\credentials.json C:\Users\Public\quicklabel-test\
copy C:\Users\<you>\QuickLabel\data\quicklabel_token.json C:\Users\Public\quicklabel-test\
```

(If you're testing the `claude-code` flavor too, stage the right
release ZIP for that branch as well.)

### 3. Get the QuickLabel release ZIP into the staging folder

Download the latest release ZIP from the GitHub Releases page (or
`git archive` from your dev checkout) into the same staging folder:

```powershell
# Option A: from a release ZIP you've downloaded
copy ~\Downloads\QuickLabel-vX.Y.Z.zip C:\Users\Public\quicklabel-test\

# Option B: zip from your dev checkout
cd C:\Users\<you>\QuickLabel
git archive --format=zip --output=C:\Users\Public\quicklabel-test\QuickLabel.zip HEAD
```

### 4. Switch to qatest and install

1. Press **Win+L** to lock; on the lock screen, click qatest at bottom-left.
2. Log in as qatest.
3. Open Explorer → `C:\Users\Public\quicklabel-test\` → extract the
   ZIP to `C:\Users\qatest\Documents\QuickLabel\` (or anywhere you'll
   remember).
4. Copy `credentials.json` into that folder (project root).
5. Create the `data/` subdirectory and copy `quicklabel_token.json`
   into it:
   ```powershell
   mkdir C:\Users\qatest\Documents\QuickLabel\data
   copy C:\Users\Public\quicklabel-test\quicklabel_token.json C:\Users\qatest\Documents\QuickLabel\data\
   ```
6. Right-click `setup.ps1` → **Run with PowerShell**.
7. Follow the prompts. The installer will offer to install Python and
   Ollama via winget — **say yes both times** (this is exactly the
   path a friend on a fresh laptop would take).
8. Pick option 4 (skip) when asked for an LLM model — it'll be a
   multi-GB download you can do later if needed.

### 5. Verify the install

After setup completes, the browser opens to `http://127.0.0.1:8765/`.

- Server should reach `/healthz` → 200.
- The legacy token file you placed at `data/quicklabel_token.json`
  has been migrated into qatest's Credential Manager and the file is
  deleted. Verify by checking that the file is gone.
- `/setup` should show all three steps as ✓ done (you uploaded
  `credentials.json` already and the token migrated cleanly).

If the wizard asks you to upload `credentials.json` again, it means
the file wasn't where the wizard expected — drop it via the upload
zone and continue.

### 6. Walk the manual QA checklist

Open `docs/quicklabel-manual-qa.md` and tick boxes top-to-bottom.
The most important sections, in order:

- **Install + lifecycle (I1–I5)** — you just exercised I1 + I3.
  Run I2 (idempotent re-run) by re-running setup.ps1. Run I4
  (start/stop/restart). Skip I5 (uninstall) until you're done with
  the test pass — you'll need the install to test other sections.
- **OAuth wizard (W1–W8)** — W3 was just tested. W2 (deep links)
  worth verifying. W7 (`/setup/reset`) is safe to test — re-authorize
  is one click since `credentials.json` stays in place.
- **Settings (S1–S5)** — only S1, S2, S5 work without an LLM pulled.
  Pull a small model (`ollama pull qwen2.5:3b-instruct`) if you want
  to test S3, S4.
- **System tray (T1–T5)** — run `setup-tray.ps1` first. The most
  important is T4 (tray survives server restart).
- **Critical-path manual tests (1–10)** — these need a real Gmail
  email. The bookmarklet flow (cases 2, 3) is the highest-value to
  validate end-to-end.

### 7. Cleanup

When you're done testing:

```powershell
# Inside qatest, optional -- removes auto-start cleanly:
.\uninstall-tray.ps1
.\uninstall.ps1

# Then switch back to your dev user. Settings -> Accounts -> Other
# users -> qatest -> Remove. Choose "Delete account and data" to wipe
# everything qatest created.
```

Removing the user account also removes:
- All Task Scheduler entries owned by qatest
- qatest's Credential Manager (so the migrated keyring token is gone)
- qatest's whole `%USERPROFILE%` including the QuickLabel install
  and `data/`

So the only thing left on your machine after the test is the staging
folder at `C:\Users\Public\quicklabel-test\`. Delete that too.

---

## Re-running the test

After cleanup:
1. Re-create qatest (Settings → Add account again).
2. Re-stage the files (the staging folder might still exist).
3. Repeat steps 4–6.

Each test pass is fully isolated — no risk of carrying over weird
state from a previous run.

---

## Critical paths to NOT break

If anything below fails, that's a productization regression:

- **Auto-start at login.** After running setup.ps1 once, a logout +
  login should bring the server back up automatically.
- **Token migration.** Dropping a legacy `data/quicklabel_token.json`
  should result in (a) the token in keyring, (b) the file deleted,
  (c) the wizard at /setup showing "all set" without re-auth.
- **`pip install -e .` cleanly succeeds.** Phase 1's claim was that
  `python -m quicklabel serve` works without `PYTHONPATH=src`. Verify
  this by doing it manually after install:
  `.venv\Scripts\python.exe -m quicklabel serve` (no env var).
- **Setup is idempotent.** Re-running setup.ps1 should never break a
  working install. If it does, the install scripts have a bug.
- **Tray survives server restart.** Restarting via the tray menu
  should not kill the tray.

---

## What this test does NOT cover

- A truly fresh Windows install (Windows itself is your dev OS — only
  user-level state is fresh, not OS-level state).
- Mac: you'd need an actual Mac to validate `setup.command` end-to-end.
  The unit tests cover the cross-platform helpers in `tray.py`; the
  bash-side install scripts have no automated coverage.
- Network-restricted environments (corporate proxies, firewalls).

For the Mac case, the soonest you can validate it is whenever you have
access to a Mac — borrowed laptop, a friend's machine, or a Mac VM on
a host that supports virtualization.

---

## When this runbook itself goes stale

Update it whenever you change:
- The install scripts (setup.ps1, setup.command, lifecycle wrappers)
- The wizard surface (/setup endpoints or template)
- The token migration path (token_store.py)
- The auto-start service definition (Task Scheduler args, plist contents)

The QA cases in `quicklabel-manual-qa.md` reference specific UI elements
+ behaviors. If you change the UI, refresh both this file and that one.
