"""Project-wide paths and constants. Kept tiny and dependency-free."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "emails.db"
CREDENTIALS_PATH = PROJECT_ROOT / "credentials.json"
TOKEN_PATH = PROJECT_ROOT / "token.json"

# gmail.modify allows read/label/archive/move-to-trash but NOT permanent delete.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
