"""QuickLabel: a privacy-preserving Gmail labeling assistant.

Bookmarklet + localhost web UI. Reads the email you point it at,
proposes a filter rule and label name, applies on confirmation.

Per Hard Constraint of the parent project, no email content leaves
the user's machine. The local server binds to 127.0.0.1 only.
"""

__version__ = "0.1.0"
