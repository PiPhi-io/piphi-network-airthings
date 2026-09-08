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
    assert manifest["ui"]["experience_packages"] == [
        {
            "registry_id": "io.piphi.airthings-air-quality",
            "version_range": ">=0.1,<1",
            "auto_install": True,
        }
    ]
    assert behavior_capabilities == manifest_capabilities


def test_air_quality_experience_matches_integration_capabilities() -> None:
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "src" / "manifest.json").read_text())
    package = json.loads(
        (root / "experiences" / "air-quality" / "package.source.json").read_text()
    )

    assert package["sdk_version_range"] == ">=0.5,<0.6"
    assert package["owning_integration_id"] == manifest["id"]
    required = {
        capability
        for slot in package["widgets"][0]["binding_slots"]
        for capability in slot["capability_requirements"]
    }
    assert required <= set(manifest["capabilities"])
