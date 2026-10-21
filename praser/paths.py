"""Path resolution for shared map JSONs and the Zawgyi detection model."""

import json
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent
MAP_DIR = REPO_ROOT / "map"
DEFAULT_MODEL_PATH = REPO_ROOT / "model" / "zawgyiUnicodeModel.dat"

_MAP_JSON_CACHE: dict = {}


def map_json(name: str):
    """Load <root>/map/<name>.json once, cached."""
    if name not in _MAP_JSON_CACHE:
        with open(MAP_DIR / f"{name}.json", encoding="utf-8") as handle:
            _MAP_JSON_CACHE[name] = json.load(handle)
    return _MAP_JSON_CACHE[name]


def find_model_path() -> str:
    """Return the path to <root>/model/zawgyiUnicodeModel.dat; raise if missing."""
    if not DEFAULT_MODEL_PATH.is_file():
        raise FileNotFoundError(f"missing {DEFAULT_MODEL_PATH}")
    return str(DEFAULT_MODEL_PATH)
