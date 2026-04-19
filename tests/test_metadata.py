from __future__ import annotations

import json
from pathlib import Path


def test_manifest_and_behaviors_align() -> None:
    root = Path(__file__).resolve().parent.parent / "src"
    manifest = json.loads((root / "manifest.json").read_text())
    behaviors = json.loads((root / "behaviors.json").read_text())

    manifest_capabilities = set(manifest["entities"][0]["capabilities"])
    behavior_capabilities = set(behaviors["devices"][0]["capabilities"])

    assert manifest["id"] == "airthings-consumer-cloud-api"
    assert manifest["api"]["endpoints"]["config_sync"] == "/configs/sync"
    assert behavior_capabilities == manifest_capabilities
