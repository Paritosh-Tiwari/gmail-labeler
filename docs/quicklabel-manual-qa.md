# QuickLabel — Manual QA Checklist

Cases that require a real browser, real Gmail account, or real LLM and
therefore can't be covered by the automated test suite (`tests/`). Run
through this list when you've made non-trivial changes, before sharing
with anyone else, or after upgrading dependencies.

For automated coverage, see:
- `tests/test_quicklabel_*.py` — 376 unit tests (per-module + edge cases + security + setup wizard + tray)
- `scripts/quicklabel_intel_smoke.py` — live LLM smoke (no Gmail)
- `scripts/quicklabel_filter_smoke.py` — live Gmail filter CRUD (test-only sender)
- `scripts/quicklabel_e2e.py` — full per-email flow against real Gmail
- `scripts/quicklabel_queue_e2e.py` — queue-scan + skip flow

## Setup before running these

If you installed via `setup.ps1` (Windows) or `setup.command` (Mac):
the server is already auto-starting at login and you don't need to do
anything. Skip ahead to the install / lifecycle / wizard sections below.

If you're running from a dev checkout without the install scripts:

1. `pip install -e .` once (replaces the old `PYTHONPATH=src` ritual).
2. Server: `.venv\Scripts\python.exe -m quicklabel serve` (Win) /
   `.venv/bin/python -m quicklabel serve` (Mac).
3. Bookmarklet installed in current browser (drag from landing page if not).
4. `data/quicklabel.db` exists (created on first server start).
5. Ollama running with whichever model `/settings` shows as configured.

---

## Install + lifecycle (Phase 1)

### I1. Fresh install via `setup.ps1` / `setup.command`
- [ ] On a machine without QuickLabel installed before, extract release
      ZIP, right-click `setup.ps1` → Run with PowerShell (or double-click
      `setup.command` on Mac).
- [ ] Script detects/installs Python 3.11+ (offers winget/brew install
      if missing).
- [ ] Script detects/installs Ollama (offers winget/brew install if missing).
- [ ] Script prompts for an LLM model. Pick option 4 (skip) if you don't
      want to wait for a multi-GB download during testing.
- [ ] Script registers Task Scheduler entry "QuickLabel" (Win) or
      `~/Library/LaunchAgents/com.quicklabel.plist` (Mac).
- [ ] Script starts the server, opens browser to `http://127.0.0.1:8765`.

**What could go wrong:**
- Windows SmartScreen blocks the script on first run — user must click
  "More info" → "Run anyway".
- winget is too old / missing on Win 10 < 1809 — script falls back to
  asking the user to install Python from python.org.
- Ollama install on Mac via brew may fail without `brew services start
  ollama` — script does this automatically; verify Ollama is reachable
  on `127.0.0.1:11434` after install.

### I2. Idempotent re-run
- [ ] Run `setup.ps1` / `setup.command` a second time on the same machine.
      Should skip the Python+venv+Ollama steps (or report "already
      installed"), regenerate the Task Scheduler entry, and finish faster
      than the first run.

### I3. Auto-start at login
- [ ] After install, log out and log back in. Within ~10 s of desktop
      load, `http://127.0.0.1:8765/healthz` returns 200.
- [ ] On Win: `taskschd.msc` shows the `QuickLabel` task in state
      "Running" (or "Ready" if it's between trigger fires).
- [ ] On Mac: `launchctl list | grep quicklabel` shows
      `com.quicklabel` with PID > 0.

### I4. Lifecycle wrappers
- [ ] `.\stop.ps1` / `./stop.command` — server stops; `/healthz` no longer
      responds.
- [ ] `.\start.ps1` / `./start.command` — server starts; `/healthz` returns 200.
- [ ] `.\restart.ps1` / `./restart.command` — equivalent to stop + start;
      use after editing Python in `src/quicklabel/`.

### I5. Uninstall keeps data
- [ ] `.\uninstall.ps1` / `./uninstall.command` removes the auto-start
      entry but does NOT delete `data/`, `.venv/`, or the OS-keyring
      OAuth token.
- [ ] `data/quicklabel.db` and `data/settings.json` still on disk.
- [ ] On Win: `taskschd.msc` no longer shows the `QuickLabel` task.
- [ ] On Mac: `launchctl list | grep quicklabel` returns nothing.
- [ ] Re-running `setup.ps1` / `setup.command` after uninstall picks up
      the existing data without losing queue history or applies.

---

## OAuth wizard walkthrough (Phase 2)

### W1. First-time install routes to /setup
- [ ] On a fresh install (no `credentials.json`, no token in keyring):
      visit `http://127.0.0.1:8765/`. Should 303 redirect to `/setup`.
- [ ] Same for `/queue`, `/label?id=...`, `/history`, `/settings` — all
      redirect to `/setup` until OAuth is complete.
- [ ] `/healthz` is the only path that returns 200 in this state.

### W2. Step 1 — Cloud Console deep links
- [ ] All 4 "Open ↗" buttons in step 1 open the correct Google Cloud
      Console pages in a new tab (project creator, Gmail API library,
      OAuth consent screen, Credentials page).
- [ ] The required scopes shown verbatim
      (`gmail.modify` + `gmail.settings.basic`) are addable on the
      consent screen — both should be in Google's scope picker.

### W3. Credentials upload — happy path
- [ ] After downloading the JSON from Cloud Console, drop it into the
      file picker on step 1. Click Upload.
- [ ] Page reloads; step 1 is now ✓ done, step 2 (active).
- [ ] `credentials.json` is now present at the project root.

### W4. Credentials upload — error paths
- [ ] Upload a non-JSON file (e.g. a PNG): error banner says "Not valid
      JSON".
- [ ] Upload a JSON file that doesn't have `installed` at the top level
      (e.g. `{"foo": "bar"}`): error banner says "Unrecognized
      credentials file".
- [ ] Upload a Web-application OAuth client JSON (top-level `web` key):
      error banner specifically calls out "Wrong OAuth client type" and
      tells user to re-create as Desktop app.
- [ ] Upload a >64 KB file: error banner says "Upload too large".

### W5. Authorize — happy path
- [ ] Click "Open Google sign-in" on step 2.
- [ ] A new tab opens to `accounts.google.com`. Sign in with the same
      Gmail account used for the OAuth client setup.
- [ ] Approve all requested scopes (gmail.modify + gmail.settings.basic).
- [ ] Browser shows "The authentication flow has completed" page; close
      that tab.
- [ ] Return to the `/setup` tab — page should have refreshed showing
      step 3 ✓.

### W6. Authorize — failure paths
- [ ] Click "Open Google sign-in", then immediately close the OAuth tab
      without approving. Wizard hangs (no timeout). Workaround:
      `.\restart.ps1` / `./restart.command` to free the request, then
      retry. This is a known limitation of the synchronous flow.
- [ ] If you decline scopes (Cancel button on Google consent): error
      banner shows "Authorization failed".

### W7. /setup/reset
- [ ] On a configured install, visit `http://127.0.0.1:8765/setup/reset`.
- [ ] Wizard goes back to step 2 (credentials remain, token cleared).
- [ ] Re-authorizing works.

### W8. Wizard correctly hidden when ready
- [ ] After full setup, visit `/setup`. Page shows "All set ✓" with
      a link to landing page (no form fields).
- [ ] Visit `/`, `/queue`, etc. — no longer redirected to `/setup`.

---

## Settings (Phase 3)

### S1. Installed Ollama models in the picker
- [ ] On `/settings`, the LLM model field's dropdown lists the models
      currently in `ollama list` (with their disk sizes).
- [ ] If Ollama is unreachable, the dropdown falls back to the three
      recommended models.

### S2. Status pill
- [ ] When the configured `llm_model` is one Ollama actually has pulled:
      green pill "✓ Installed (X.X GB)".
- [ ] When configured but not pulled: amber pill "⚠ Not installed",
      with an `ollama pull <name>` hint.
- [ ] When Ollama is stopped: amber pill "⚠ Ollama not reachable".
      (Stop Ollama via OS service controls and refresh `/settings` to
      verify.)

### S3. Test-this-model button (success)
- [ ] Set `llm_model` to a small pulled model (e.g. `qwen2.5:3b-instruct`).
- [ ] Click "Test this model" — within ~30 s, top of page shows
      "Test: qwen2.5:3b-instruct ✓ Responded in N ms" with the response
      body in a code block.

### S4. Test-this-model button (failure)
- [ ] Type a model name that isn't pulled (e.g. `nonexistent:99b`).
- [ ] Click "Test this model" — shows "✗ Failed" with the Ollama error
      message ("model not found") in red.

### S5. Save settings
- [ ] Change a setting (e.g. log_level: INFO → DEBUG), click Save.
- [ ] Green flash banner says settings saved + warns to restart for
      port/log-level changes.
- [ ] Verify `data/settings.json` reflects the new value.
- [ ] After `.\restart.ps1`, check that the new log_level is in effect
      (e.g. DEBUG-level entries in `data/quicklabel.log`).

---

## System tray (Phase 5)

Skip this section if you didn't run `setup-tray.ps1` /
`setup-tray.command`.

### T1. Install + first appearance
- [ ] Run `.\setup-tray.ps1` (Win) or `./setup-tray.command` (Mac).
- [ ] Within ~5 s, a colored dot icon appears in the system tray
      (notification area on Win, menu bar on Mac).
- [ ] Hover the icon — tooltip says "QuickLabel: running" (green)
      or "QuickLabel: server is down" (red).

### T2. Status follows server state
- [ ] With server up: icon is green.
- [ ] Stop the server (`.\stop.ps1` / from tray "Stop server"). Within
      ~10 s the icon turns red.
- [ ] Start the server again. Within ~10 s the icon turns green.

### T3. Each menu item works
- [ ] **Open landing page** → opens `http://127.0.0.1:8765/`.
- [ ] **Open queue** → opens `/queue`.
- [ ] **Settings** → opens `/settings`.
- [ ] **Audit log** → opens `/audit`.
- [ ] **Open data folder** → opens `data/` in Explorer (Win) /
      Finder (Mac).
- [ ] **Restart server** — within ~5 s, server restarts (`/healthz`
      momentarily unavailable then back to 200; tray icon stays alive
      throughout).
- [ ] **Stop server** — server stops; tray turns red but stays alive.
- [ ] **Quit tray** — tray icon disappears; server is unaffected
      (`/healthz` still 200).

### T4. Tray survives server restart
- [ ] With both server and tray running, click "Restart server".
- [ ] Tray icon stays in the system tray throughout. Briefly turns
      red (during the restart window), then green when server is back.
- [ ] Critical: the tray must NOT die when the server it's watching
      restarts.

### T5. Uninstall tray
- [ ] `.\uninstall-tray.ps1` / `./uninstall-tray.command` removes the
      tray auto-start entry.
- [ ] On Win: `taskschd.msc` no longer shows "QuickLabel Tray".
- [ ] On Mac: `launchctl list | grep tray` returns nothing.
- [ ] Server auto-start is unaffected — `/healthz` still 200.

---

## Critical-path manual tests

### 1. LLM behavior across email categories
Open 5–10 different emails via the bookmarklet, one of each below. Verify
the proposed label, filter shape, and suggested actions look sensible.

- [ ] **Newsletter** (e.g. Substack, NYT Morning Briefing)
  - Expected: list-id filter, "AI Newsletters/X" or similar nested label,
    suggested skip-inbox + mark-read on heavy senders
- [ ] **Transactional** (receipt, order confirmation)
  - Expected: from-only or from+stable-subject filter, suggested mark-read
    but NOT skip-inbox
- [ ] **Personal** (from a real human you know)
  - Expected: from-only filter, default actions (no auto-archive)
- [ ] **No-reply notification** (LinkedIn, GitHub)
  - Expected: skip-inbox + mark-read suggested
- [ ] **Multi-recipient thread** (CC chain at work)
  - Expected: should still work; verify filter doesn't accidentally match
    by To/CC

**What could go wrong:**
- LLM picks a brand-new top-level label when an existing parent fits
- Filter subject contains variable parts ($, dates, IDs)
- Confidence reports >0.95 but the suggestion is wrong (over-confident)

### 2. Bookmarklet install + click
- [ ] Drag "Label this" from landing page to bookmark bar (Chrome strips
      `javascript:` from address-bar pastes — drag is the only way)
- [ ] Click bookmark on a Gmail conversation → new tab opens to /label?id=...
- [ ] Click bookmark on Gmail's inbox list (no email open) → alert:
      "Click an email in Gmail first..."
- [ ] Click bookmark on a non-Gmail page (e.g. github.com) → alert:
      "QuickLabel only works inside Gmail..."
- [ ] Enable Chrome's popup blocker, click bookmark on a real email →
      alert: "could not open a new tab — your browser likely blocked it..."

### 3. Real Gmail Apply (round-trip)
Pick a low-stakes sender (e.g. one of your newsletters with 10–20 prior).
- [ ] Apply a label with scope=both. Verify in Gmail web UI:
  - The label appears on the new email
  - The label appears on existing matching emails
  - A new filter shows up in Settings → Filters
- [ ] Click Undo from the green flash banner OR /history page
- [ ] Verify in Gmail web UI:
  - The filter is gone from Settings → Filters
  - The label is removed from the same set of emails (existing ones
    that had it before are untouched — only the ones added by this apply
    are reverted)

### 4. Trash apply (the most destructive path)
Pick a sender you'd actually be willing to delete (e.g. an old newsletter
you don't read).
- [ ] In the queue or per-email view, choose Inbox=Trash, scope=both
- [ ] Confirm dialog should appear (server-side confirm.html, not just
      JS popup) listing the count and warning text
- [ ] Verify destructive sentence reads correctly:
      "Applying this will retroactively **move to trash** N existing
      emails matching <code>filter</code>"
- [ ] Confirm and apply. Check Gmail Trash folder — matches should be there
- [ ] Undo from /history. Check Inbox — matches should reappear

### 5. Filter conflict + Replace
- [ ] Pick a sender that already has a Gmail filter (you'll know from
      the queue row — yellow strip "Currently auto-labeled as: X")
- [ ] Click Apply with a different label
- [ ] Confirm dialog should show: warning about stacking + "[x] Delete the
      existing filter(s) and replace" pre-checked
- [ ] Confirm and apply. Check Gmail Settings → Filters: old filter
      should be gone, new one in its place
- [ ] If the queue had multiple rows for the same sender (e.g., multiple
      newsletters from substack.com with different list-ids), confirm
      ONLY the row you applied disappears, not the others (bug #6 fix)

### 6. Regenerate-with-AI
- [ ] In the queue, click "✨ Regenerate with AI" on a row → page redirects
      to /label?id=... (full per-email view) with fresh LLM proposal
- [ ] On the per-email page, click "✨ Regenerate with AI" → page reloads
      with a different LLM run (cache is busted)
- [ ] Open the same email in another tab → cache hit, instant render with
      same proposal
- [ ] Click Regenerate again → cache miss, takes 5–15s

### 7. OAuth refresh
- [ ] Let your token sit unused for ~1 hour
- [ ] Open /queue and click Scan now
- [ ] Should still work (token refresh is automatic). If you see a 401 in
      the server log, the refresh path is broken
- [ ] Worst case: delete `data/quicklabel_token.json`, restart server,
      hit /queue → browser should open Google OAuth → grant → redirect
      back works

### 8. Multiple Gmail accounts
If you have 2+ Gmail accounts in the same Chrome profile:
- [ ] Verify the bookmarklet always opens against the account you
      OAuth'd with (token is bound to one account)
- [ ] Switching Gmail account in the browser does NOT change which
      account QuickLabel reads — it stays on the OAuth'd one

### 9. Browser CSP enforcement
- [ ] Open browser DevTools (F12) → Console tab
- [ ] Refresh /queue and /label?id=... and /history
- [ ] Look for **red CSP violation reports** ("Refused to execute inline
      script because it violates...")
- [ ] If any appear, the CSP is too strict for our templates and needs
      a directive added

### 10. DNS rebinding (real attack — optional, requires infra)
- [ ] Set up a domain you control with very-low-TTL DNS
- [ ] Serve attack page; rebind to 127.0.0.1
- [ ] Visit the page in a browser where QuickLabel is running
- [ ] Verify all requests to /queue, /apply, etc. return 421
- [ ] Verify the attempts show up in /audit page with red BLOCKED badges

(The unit tests already verify the Host-header middleware logic; this
manual test would verify the browser-DNS-rebinding actually triggers
the same code path against a real attacker setup.)

---

## Performance / scale tests

### 11. Large queue
- [ ] Once you have 100+ pending proposals, scroll /queue. Should render
      under 2 seconds
- [ ] If slow, the bottleneck is `current_labels_by_id` (one Gmail
      call for filters + labels per page render — though it's cached)

### 12. Large back-prop
- [ ] Apply a filter that matches 1000+ existing messages (e.g. a heavy
      sender). Back-prop is capped at 5000 in `backprop_label`
- [ ] Should complete in <30s for 1000 messages
- [ ] Verify Undo on that volume works (reverses on the recorded set
      via `reverse_backprop`)

### 13. Mobile / tablet
- [ ] Open /queue on your phone. Layout will likely break (we only
      styled for desktop — `max-width: 720px` containers + horizontal
      table on /audit)
- [ ] Decide whether to add responsive CSS or document "desktop only"

---

## Workspace / vanity domain tests

### 14. Workspace account with custom domain
- [ ] If you OAuth'd as `you@company.com` (Google Workspace), does the
      bookmarklet still work? Workspace users still hit `mail.google.com`
      under the hood, so the host check should pass
- [ ] If your company uses a vanity URL (`mail.company.com`) that
      iframes Gmail, the bookmarklet's `mail.google.com` host check will
      reject it — this is a known limitation

---

## Things to watch in the audit log

After running through the above:

- [ ] Open /audit (linked from landing page)
- [ ] Every request you made should appear, newest first
- [ ] Status codes should make sense (200 / 303 / 404 / 422 — not
      unexplained 500s)
- [ ] No row should have a non-localhost Host header (means a DNS
      rebinding attempt got through to the audit step — it would have
      been blocked by HostValidationMiddleware so it should also have
      `BLOCKED` badge in red)

---

## Bugs found in past QA passes (for reference)

The 14-bug QA pass and follow-on edge-case battery already fixed:

1. `/apply` silently dropped user edits to `filter_query` (`eedcc3f`)
2. `_criteria_from_query` lost `list:` when combined with `from:` (`eedcc3f`)
3. Queue Apply silently fired hidden destructive actions (`eedcc3f`)
4. LLM bool coercion of `"false"` returned `True` (`22bac8f`)
5. `categorize` action missing from LLM prompt + parser (`22bac8f`)
6. Sender with multiple newsletters: applying one swept all (`22bac8f`)
7. Per-(sender, list_id) dedup gap (`22bac8f`)
8. Dead Undo form on applied.html (`f360be1`)
9. Hidden `required` blocked Skip/Never (`f360be1`)
10. Filter conflict missed `from:` inside query field (`f360be1`)
11. ensure_label race recovery KeyError on case mismatch (`f360be1`)
12. extract_body shadowed HTML when plain decoded empty (`f360be1`)
13. Model name captured at first call (`f360be1`)
14. Bonus: NameError in /apply queue cleanup after #1 fix (`d21ee70`)
15. Quoted `list:"foo.com"` value kept leading quote (`1eef3c3`)

If any of these regress in your manual testing, the bug is back —
file an issue with the commit hash referenced above.
