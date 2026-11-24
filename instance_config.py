import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

ENV_PATH = Path(
    os.environ.get("CHUNGUS_ENV_FILE", str(BASE_DIR / ".env"))
).expanduser().resolve()

if not ENV_PATH.is_file():
    raise RuntimeError(f"Configuration file not found: {ENV_PATH}")

load_dotenv(ENV_PATH, override=True)
