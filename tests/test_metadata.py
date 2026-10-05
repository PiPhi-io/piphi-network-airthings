from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
from zipfile import ZipFile

import pytest


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

    device = behaviors["devices"][0]
    conditions = {condition["id"]: condition for condition in device["conditions"]}
    above_params = {item["name"]: item for item in conditions["metric_above"]["params"]}
    below_params = {item["name"]: item for item in conditions["metric_below"]["params"]}
    assert above_params["metric"]["default"] == "co2_ppm"
    assert above_params["value"]["default"] == 1000
    assert below_params["metric"]["default"] == "battery_percent"
    assert below_params["value"]["default"] == 20
    trigger_events = {trigger["runtime"]["event"] for trigger in device["triggers"]}
    assert {
        "device.state_changed",
        "airthings.cloud.sample.failed",
        "airthings.cloud.sample.recovered",
    } <= trigger_events

    template_action = behaviors["templates"][0]["config"]["actions"][0]
    assert template_action["action"] == "builtin.notification.send"
    assert "capability" not in template_action
    assert "sourceRef" not in template_action
    assert "target" not in template_action
    assert template_action["runtime"] == {
        "command": "builtin.notification.send",
        "semanticType": "notification",
        "actionContractVersion": 1,
    }
    assert template_action["parameters"]["title"]
    assert "channel" not in template_action["parameters"]


def test_all_release_version_projections_match() -> None:
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "src" / "manifest.json").read_text())
    catalog = json.loads((root / "docs" / "capability-catalog.json").read_text())
    package = json.loads(
        (root / "experiences" / "air-quality" / "package.source.json").read_text()
    )
    pyproject_text = (root / "pyproject.toml").read_text()
    project_version = next(
        line.split('"')[1]
        for line in pyproject_text.splitlines()
        if line.startswith("version = ")
    )

    assert manifest["version"] == project_version
    assert catalog["catalog_version"] == project_version
    assert package["identity"]["version"] == project_version
    assert manifest["runtime"]["linux"]["container"]["image"].endswith(
        f":{project_version}"
    )


def test_release_script_updates_catalog_and_experience_versions() -> None:
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "airthings_release", root / "scripts" / "release.py"
    )
    assert spec is not None and spec.loader is not None
    release = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = release
    spec.loader.exec_module(release)
    catalog = {"catalog_version": "0.1.5"}
    package = {"identity": {"version": "0.1.5"}}

    release.update_release_projections(
        catalog=catalog,
        experience_package=package,
        version="0.1.6",
    )

    assert catalog["catalog_version"] == "0.1.6"
    assert package["identity"]["version"] == "0.1.6"


def test_release_image_binds_source_manifest_and_behavior_provenance() -> None:
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text()
    workflow = (root / ".github" / "workflows" / "release.yml").read_text()
    required_labels = {
        "org.opencontainers.image.version": "RELEASE_VERSION",
        "org.opencontainers.image.revision": "SOURCE_REVISION",
        "org.opencontainers.image.source": "SOURCE_REPOSITORY",
        "io.piphi.manifest.sha256": "MANIFEST_SHA256",
        "io.piphi.behaviors.sha256": "BEHAVIORS_SHA256",
    }
    workflow_labels = {
        "org.opencontainers.image.version=${{ steps.version.outputs.version }}",
        "org.opencontainers.image.revision=${{ steps.contracts.outputs.revision }}",
        "org.opencontainers.image.source=https://github.com/PiPhi-io/piphi-network-airthings",
        "io.piphi.manifest.sha256=${{ steps.contracts.outputs.manifest_sha256 }}",
        "io.piphi.behaviors.sha256=${{ steps.contracts.outputs.behaviors_sha256 }}",
    }

    for label, build_argument in required_labels.items():
        assert f"ARG {build_argument}=" in dockerfile
        assert f'{label}="${{{build_argument}}}"' in dockerfile
    assert workflow_labels <= set(workflow.splitlines()) | {
        line.strip() for line in workflow.splitlines()
    }
    assert "revision=$(git rev-parse HEAD)" in workflow
    assert "manifest_sha256=$(sha256sum src/manifest.json" in workflow
    assert "behaviors_sha256=$(sha256sum src/behaviors.json" in workflow
    assert workflow.index("Validate bumped release contract") < workflow.index(
        "Commit release metadata"
    )
    assert workflow.index("Publish immutable release revision") < workflow.index(
        "Promote qualified digest without rebuilding"
    )


def test_release_workflow_recovers_only_missing_images_without_overwriting_tags() -> None:
    root = Path(__file__).resolve().parent.parent
    workflow = (root / ".github" / "workflows" / "release.yml").read_text()

    assert "existing_tag:" in workflow
    assert "permissions: {}" in workflow
    assert "preflight:" in workflow
    assert 'git fetch --no-tags origin "refs/tags/${EXISTING_TAG}"' in workflow
    assert 'TAG_COMMIT=$(git rev-parse "FETCH_HEAD^{commit}")' in workflow
    assert "needs: preflight" in workflow
    assert "ref: ${{ needs.preflight.outputs.tag_commit || github.ref }}" in workflow
    assert '[[ "${EXISTING_TAG}" == "${TAG}" ]]' in workflow
    assert workflow.count("if: ${{ needs.preflight.outputs.existing_tag == '' }}") == 2
    assert "Recovery publication from an existing tag must set push_latest=false" in workflow
    assert "python scripts/assert_docker_tag_absent.py" in workflow
    assert workflow.index("Log in to Docker Hub") < workflow.index(
        "Prove immutable version tag is absent"
    )


def test_release_workflow_qualifies_exact_image_before_publishing_revision() -> None:
    root = Path(__file__).resolve().parent.parent
    workflow = (root / ".github" / "workflows" / "release.yml").read_text()

    assert "Build and stage immutable multi-architecture candidate" in workflow
    assert "Qualify exact release candidate across restart" in workflow
    assert "python scripts/qualify_release_image.py" in workflow
    assert '--image "${RELEASE_IMAGE}@${{ steps.build.outputs.digest }}"' in workflow
    assert "--version \"${{ steps.version.outputs.version }}\"" in workflow
    assert "--revision \"${{ steps.contracts.outputs.revision }}\"" in workflow
    assert "--manifest-sha256 \"${{ steps.contracts.outputs.manifest_sha256 }}\"" in workflow
    assert "--behaviors-sha256 \"${{ steps.contracts.outputs.behaviors_sha256 }}\"" in workflow
    assert workflow.index("Qualify exact release candidate across restart") < workflow.index(
        "Publish immutable release revision"
    )
    assert workflow.count("uses: docker/build-push-action@v6") == 1
    assert "Promote qualified digest without rebuilding" in workflow
    assert 'SOURCE="${RELEASE_IMAGE}@${{ steps.build.outputs.digest }}"' in workflow
    assert "python scripts/verify_published_image.py" in workflow
    assert '--image "${RELEASE_IMAGE}:latest"' in workflow
    assert workflow.index("Publish immutable release revision") < workflow.index(
        "Promote qualified digest without rebuilding"
    ) < workflow.index("Verify promoted digest and platforms")


def test_capability_catalog_marks_declared_scope_implementation_complete() -> None:
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "src" / "manifest.json").read_text())
    catalog = json.loads((root / "docs" / "capability-catalog.json").read_text())
    rows = {row["id"]: row for row in catalog["capabilities"]}

    assert catalog["integration_id"] == manifest["id"]
    assert catalog["catalog_version"] == manifest["version"]
    assert catalog["completion"] == {
        "status": "implementation_complete",
        "scope": "Airthings Consumer Cloud devices and readings declared by this integration",
        "automated_validation": "passed",
        "physical_device_validation": "pending",
    }
    assert set(manifest["capabilities"]) <= rows.keys()
    assert all(rows[capability]["status"] == "implemented" for capability in manifest["capabilities"])
    assert set(manifest["commands"]) == {
        row["id"] for row in catalog["commands"] if row["status"] == "implemented"
    }


def test_air_quality_experience_matches_integration_capabilities() -> None:
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "src" / "manifest.json").read_text())
    package = json.loads(
        (root / "experiences" / "air-quality" / "package.source.json").read_text()
    )

    assert package["sdk_version_range"] == ">=0.6.1,<0.7"
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
    assert widget["presentation"]["shell"] == "core"
    targets = widget["interaction_targets"]
    assert len(targets) == 5
    assert {target["binding_slot_id"] for target in targets if target["kind"] == "binding"} == set(slots)
    assert all(target["default_action"] == "more-info" for target in targets if target["kind"] == "binding")
    assert all("command" not in target["allowed_actions"] for target in targets)
    assert widget["core_card_replacements"] == [
        {
            "schema_version": "1",
            "id": "airthings-air-quality-v1",
            "source_card_types": ["sensor-card", "stat"],
            "source_capability_ids": [
                "temperature_c",
                "humidity_percent",
                "radon_short_term_bqm3",
                "co2_ppm",
            ],
            "strategy": "same-device",
        }
    ]
    assert widget["default_theme_id"] == "airthings"
    assert {theme["id"] for theme in widget["themes"]} == {"airthings", "quiet"}
    assert all(item["action"]["type"] == "details" for item in widget["recipe"]["items"])
    branded_theme = (root / "experiences" / "air-quality" / "themes" / "airthings.css").read_text()
    assert "--piphi-experience-shadow: none;" in branded_theme
    assert "--piphi-experience-tile-shadow: none;" in branded_theme


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


def test_experience_check_build_is_branch_safe_but_release_build_requires_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "airthings_build_experience_release_ref", root / "scripts" / "build_experience.py"
    )
    assert spec is not None and spec.loader is not None
    build_experience = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_experience)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")

    build_experience.build(tmp_path, check=True, env_name="unused", key_id="test-key")
    with pytest.raises(SystemExit, match="does not match package version"):
        build_experience.build(tmp_path, check=False, env_name="unused", key_id="test-key")
