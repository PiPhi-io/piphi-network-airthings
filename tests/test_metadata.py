from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from zipfile import ZipFile


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

    assert package["sdk_version_range"] == ">=0.5.1,<0.6"
    assert package["owning_integration_id"] == manifest["id"]
    required = {
        capability
        for slot in package["widgets"][0]["binding_slots"]
        for capability in slot["capability_requirements"]
    }
    assert required <= set(manifest["capabilities"])
    slots = {
        slot["id"]: slot
        for slot in package["widgets"][0]["binding_slots"]
    }
    assert slots["temperature"]["required"] is True
    assert slots["humidity"]["required"] is False
    assert slots["radon"]["required"] is False
    assert slots["co2"]["required"] is False
    widget = package["widgets"][0]
    assert widget["default_theme_id"] == "airthings"
    assert {theme["id"] for theme in widget["themes"]} == {"airthings", "quiet"}
    assert all(item["action"]["type"] == "details" for item in widget["recipe"]["items"])


def test_air_quality_release_archive_contains_normalized_theme_contract() -> None:
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "airthings_build_experience", root / "scripts" / "build_experience.py"
    )
    assert spec is not None and spec.loader is not None
    build_experience = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_experience)
    source = json.loads(
        (root / "experiences" / "air-quality" / "package.source.json").read_text()
    )
    normalized = build_experience._normalized(source)
    widget = normalized["widgets"][0]

    assert widget["themes"]
    assert all(
        slot["data_delivery"]
        == {"mode": "stream_preferred", "stale_after_seconds": None}
        for slot in widget["binding_slots"]
    )
    with ZipFile(io.BytesIO(build_experience._archive(normalized))) as package:
        assert {
            "package.source.json",
            "themes/airthings.css",
            "themes/quiet.css",
        } <= set(package.namelist())
