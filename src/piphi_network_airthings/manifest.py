from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_manifest() -> dict[str, Any]:
    manifest_path = Path(__file__).resolve().parent.parent / "manifest.json"
    return json.loads(manifest_path.read_text())
