import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

selected = os.environ.get("CHUNGUS_ENV_FILE")

if not selected:
    raise RuntimeError(
        "CHUNGUS_ENV_FILE must specify an instance configuration."
    )

ENV_PATH = Path(selected).expanduser()

if not ENV_PATH.is_absolute():
    ENV_PATH = BASE_DIR / ENV_PATH

ENV_PATH = ENV_PATH.resolve()

if not ENV_PATH.is_file():
    raise RuntimeError(f"Configuration file not found: {ENV_PATH}")

load_dotenv(ENV_PATH, override=True)
