# Google Cloud Setup — Step-by-Step

This is a one-time setup. It tells Google "this app on my computer is allowed to read and label my Gmail." It takes about 10 minutes. You only do it once.

**You will end up with one file:** `credentials.json`, saved in the project root (`C:\Users\<you>\QuickLabel\credentials.json`). That file is on the gitignore list, so it cannot be accidentally committed.

---

## Why we're doing this

The Gmail API is gated behind a Google Cloud "project" — basically a container for "things this developer is doing with Google services." We need to:

1. Create a Cloud project (free, no billing required for what we're doing).
2. Turn on the Gmail API for that project.
3. Tell Google "this app wants to read/label MY Gmail, here's the consent screen the user will see."
4. Generate an OAuth client ID — the credentials our Python code will use to ask "hey Gmail, can I have access?"

The scope we request is **`gmail.modify`** — that's read, label, archive, and move-to-trash, but **not** permanent delete. Trash is recoverable for 30 days, so any mistake we make is reversible. We never request the full-mailbox scope.

---

## Step 1 — Open the Google Cloud Console

Go to <https://console.cloud.google.com/>

Sign in with the **same Google account whose Gmail you want to label** (`your-gmail@example.com`).

If this is your first time, Google may ask you to agree to terms of service. Accept.

---

## Step 2 — Create a new project

1. At the very top of the page, click the project dropdown (it's next to the "Google Cloud" logo, may say "Select a project" or show an existing project name).
2. In the dialog that opens, click **"NEW PROJECT"** (top right of that dialog).
3. **Project name:** `gmail-labeler` (or whatever you want — purely a label for you).
4. **Organization:** leave as "No organization."
5. Click **CREATE**.

Wait ~10 seconds for the project to be created. A notification bell will ding when ready. Then click that notification's **"SELECT PROJECT"** link, or use the top dropdown to switch to your new `gmail-labeler` project.

> **Verify:** the project name in the top bar should now read `gmail-labeler`. Every step from here on assumes you are inside this project.

---

## Step 3 — Enable the Gmail API

1. In the search bar at the top of the Cloud Console, type **"Gmail API"** and click the result (an icon of an envelope).
2. On the Gmail API page, click the big blue **ENABLE** button.
3. Wait a few seconds. The page will reload showing API metrics (all zeros — that's expected).

> **Verify:** the page header now says "Gmail API" and shows tabs like "Metrics," "Quotas & System Limits," etc. instead of an Enable button.

---

## Step 4 — Configure the OAuth consent screen

This is the screen you (yourself, the only user) will see when our Python script asks for Gmail access for the first time.

1. In the left sidebar (or via search), navigate to **APIs & Services → OAuth consent screen**. If you can't find the sidebar, search "OAuth consent screen" in the top search bar.
2. You may see a **"Get started"** prompt — click it, then fill in the **App information** form:
   - **App name:** `gmail-labeler` (this is what shows on the consent screen — only you will ever see it)
   - **User support email:** select `your-gmail@example.com` from the dropdown
3. Click **NEXT**.
4. **Audience:** select **External**. (Internal is only available for Google Workspace organizations; we don't have one here.) Click **NEXT**.
5. **Contact information → Email addresses:** enter `your-gmail@example.com`. Click **NEXT**.
6. **Finish:** check the box agreeing to the Google API Services User Data Policy. Click **CONTINUE**, then **CREATE**.

You're now on the **OAuth consent screen** dashboard for the project.

### 4a. Add the Gmail scope

1. In the left sidebar still under APIs & Services → OAuth consent screen, click **Data Access** (or "Scopes" depending on the UI version).
2. Click **ADD OR REMOVE SCOPES**.
3. In the filter box at the top of the panel that opens, search for **`gmail.modify`**.
4. Check the box for the row that says:
   `https://www.googleapis.com/auth/gmail.modify` — "Read, compose, and send emails from your Gmail account" (Google's wording is misleading — the *modify* scope cannot send mail or permanently delete; that's an artifact of how Google groups its scope descriptions).
5. **Do not** check `https://mail.google.com/` (full access). Make sure that one is unchecked.
6. Scroll to the bottom of the panel and click **UPDATE**.
7. Click **SAVE** at the bottom of the Data Access page.

> **Verify:** the "Your sensitive scopes" section shows exactly **one** scope: `.../auth/gmail.modify`. No other scopes should be present.

### 4b. Add yourself as a Test User

While the app is in "Testing" mode (which is what we want — we will not "Publish" it), only listed test users can authorize it.

1. In the left sidebar under OAuth consent screen, click **Audience**.
2. Scroll to **Test users** and click **+ ADD USERS**.
3. Enter `your-gmail@example.com` and click **SAVE**.

> **Verify:** under "Test users" you see `your-gmail@example.com` listed.

> **Note about Testing mode:** Apps in Testing mode issue refresh tokens that expire after **7 days**. For our one-time backfill that's fine — we will finish the whole pipeline in well under a week, and even if a token expires we just re-run the OAuth flow (one browser click). We are intentionally NOT publishing the app, because publishing requires Google verification (not needed for personal use).

---

## Step 5 — Create the OAuth Client ID

This is the actual credential our Python code will use.

1. In the left sidebar, navigate to **APIs & Services → Credentials**.
2. Click **+ CREATE CREDENTIALS** at the top, then choose **OAuth client ID**.
3. **Application type:** select **Desktop app** from the dropdown.
   - This is critical — "Desktop app" is what works with the Python script's local browser flow. Do not pick "Web application."
4. **Name:** `gmail-labeler-desktop` (or anything — you'll only see this in the Cloud console).
5. Click **CREATE**.

A dialog appears showing your **Client ID** and **Client Secret**. Don't worry about copying them — we'll get them in the JSON file.

6. Click **DOWNLOAD JSON**. A file like `client_secret_XXXXXXX.apps.googleusercontent.com.json` will download.

---

## Step 6 — Place the credentials file

1. Find the JSON file you just downloaded (probably in `C:\Users\<you>\Downloads\`).
2. **Rename it to exactly** `credentials.json`.
3. **Move it to** `C:\Users\<you>\QuickLabel\credentials.json`.

> **Verify in PowerShell:**
> ```powershell
> Test-Path C:\Users\<you>\QuickLabel\credentials.json
> ```
> Should print `True`.

> **Safety check:** the `.gitignore` already excludes `credentials.json`, so it cannot be accidentally committed.

> **Tip:** you can also skip the manual file placement entirely — QuickLabel's `/setup` wizard accepts the credentials JSON via drag-and-drop and writes it to the right place for you.

---

## What's next

With `credentials.json` in place, start the QuickLabel server and visit
<http://127.0.0.1:8765/setup>. The wizard will:

1. Detect the credentials file.
2. Run the OAuth flow (a browser window pops up asking for consent — you'll see the consent screen you configured in Step 4).
3. Stash the refresh token in your OS keyring (Windows Credential Manager / macOS Keychain) so it's never on disk.

After that you're done — drag the bookmarklet from the landing page and start labeling.

> **A note on Testing-mode token expiry:** Because your OAuth app stays in Testing mode (intentional — publishing requires Google verification), refresh tokens expire every **7 days**. When that happens, visit `/setup/reset` and re-authorize — one click, takes ~5 seconds.
