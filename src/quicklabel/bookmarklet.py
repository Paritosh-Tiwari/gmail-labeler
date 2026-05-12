"""Generate the bookmarklet text for the user to drag to their bookmark bar.

The bookmarklet runs in the Gmail page, extracts the thread ID, and opens
a new tab pointing at the local QuickLabel server.

The thread-ID extraction also has a Python mirror (`extract_id_from_hash`)
so we can unit-test the algorithm against representative Gmail URL
patterns. The Python and JS implementations must stay in sync; the
unit tests document the expected behavior.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from .settings import BASE_URL


# -----------------------------------------------------------------------
# JS bookmarklet
# -----------------------------------------------------------------------
# Behavior:
# 1. If we're not on a Gmail host, alert and stop. Avoids hitting localhost
#    with random IDs from arbitrary pages.
# 2. Read the API-compatible thread ID from the open conversation's subject
#    heading (Gmail emits it as `data-legacy-thread-id` on `h2.hP`).
# 3. Fall back to the URL hash's last segment (server decodes base-40
#    permalink format if needed).
# 4. Validate length + character set.
# 5. window.open the localhost target; if it returned null (popup blocker),
#    alert the user instead of silently failing.
_BOOKMARKLET_JS = """
(function(){
  if (!/(^|\\.)mail\\.google\\.com$/i.test(location.hostname)) {
    alert('QuickLabel only works inside Gmail. Open mail.google.com first.');
    return;
  }
  var id = null;
  var el = document.querySelector('h2.hP[data-legacy-thread-id]');
  if (el) { id = el.getAttribute('data-legacy-thread-id'); }
  if (!id) {
    var h = window.location.hash || '';
    var clean = h.split('?')[0];
    var parts = clean.split('/');
    id = parts[parts.length-1].replace(/^#/, '');
  }
  if (!id || id.length < 10 || !/^[A-Za-z0-9_-]+$/.test(id)) {
    alert('Click an email in Gmail first, then click this bookmark.\\n\\n(QuickLabel needs the conversation view, not the inbox list.)');
    return;
  }
  var w = window.open('__BASE__/label?id=' + encodeURIComponent(id), '_blank');
  if (!w) {
    alert('QuickLabel could not open a new tab — your browser likely blocked it. Allow popups from mail.google.com and try again.');
  }
})();
""".strip().replace("\n", " ").replace("  ", " ")


def bookmarklet_url(base_url: str = BASE_URL) -> str:
    """Return the full `javascript:...` URL to put in the bookmark."""
    js = _BOOKMARKLET_JS.replace("__BASE__", base_url)
    return "javascript:" + quote(js, safe="(){};,'/?:&=+")


# -----------------------------------------------------------------------
# Python mirror of the JS hash-extraction logic. Used for unit tests so
# we catch regressions to the algorithm. KEEP IN SYNC with the JS above.
# -----------------------------------------------------------------------
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MIN_ID_LEN = 10


def extract_id_from_hash(hash_str: str) -> str | None:
    """Return the thread-ID candidate from a Gmail URL hash, or None.

    Mirrors the URL-fallback branch of the bookmarklet JS. Runs the same
    transformations:
      - strip everything after '?' (Gmail appends ?compose=... etc.)
      - split on '/' and take the last segment
      - strip a leading '#'
      - validate length >= 10 and chars in [A-Za-z0-9_-]

    Returns the ID if it passes validation, None otherwise.
    """
    h = hash_str or ""
    clean = h.split("?", 1)[0]
    parts = clean.split("/")
    candidate = parts[-1] if parts else ""
    # Mirror JS `replace(/^#/, '')`: strips ONE leading '#', not all of
    # them. lstrip('#') would over-strip and accept '##X' as 'X'.
    if candidate.startswith("#"):
        candidate = candidate[1:]
    if len(candidate) < _MIN_ID_LEN or not _VALID_ID_RE.match(candidate):
        return None
    return candidate
