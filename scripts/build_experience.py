#!/usr/bin/env python3
"""Build and sign the declarative Airthings experience package."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "experiences" / "air-quality" / "package.source.json"
SIGNING_CONTEXT = b"piphi-widget-package-manifest-v1\0"


def _normalized(source: dict) -> dict:
    payload = json.loads(json.dumps(source))
    payload.setdefault("description", None)
    payload.setdefault("owning_integration_id", None)
    payload.setdefault("requires_integrations", [])
    payload.setdefault("requires_packages", [])
    payload.setdefault("recommended_for_integrations", [])
    payload.setdefault("lifecycle", "active")
    for requirement in payload["requires_integrations"]:
        requirement.setdefault("version_range", None)
        requirement.setdefault("optional", False)
    for widget in payload["widgets"]:
        widget.setdefault("description", None)
        widget.setdefault("entry", None)
        widget.setdefault("binding_slots", [])
        widget.setdefault("permissions", [])
        widget.setdefault("settings", [])
        widget.setdefault("settings_schema_version", "1")
        widget.setdefault("settings_migrations", [])
        widget.setdefault("default_column_span", 3)
        widget.setdefault("default_row_span", 3)
        for slot in widget["binding_slots"]:
            slot.setdefault("required", True)
            slot.setdefault("multiple", False)
            slot.setdefault("binding_modes", ["read"])
            slot.setdefault("value_kinds", [])
            slot.setdefault("capability_requirements", [])
            slot.setdefault("compatible_integration_ids", [])
        recipe = widget.get("recipe")
        if recipe:
            recipe.setdefault("schema_version", "1")
            recipe.setdefault("layout", "stack")
            recipe.setdefault("columns", 2)
            for item in recipe["items"]:
                if item["type"] == "metric":
                    item.setdefault("label", None)
                    item.setdefault("format", "auto")
                    item.setdefault("show_freshness", True)
                elif item["type"] == "status":
                    item.setdefault("label", None)
                    item.setdefault("true_label", "On")
                    item.setdefault("false_label", "Off")
                    item.setdefault("show_freshness", False)
                elif item["type"] == "progress":
                    item.setdefault("label", None)
                    item.setdefault("minimum", 0)
                    item.setdefault("maximum", 100)
                    item.setdefault("show_value", True)
                elif item["type"] == "text":
                    item.setdefault("text", None)
                    item.setdefault("setting_id", None)
                    item.setdefault("variant", "body")
    return payload


def _archive(source: dict) -> bytes:
    payload = json.dumps(source, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    output = io.BytesIO()
    info = ZipInfo("package.source.json", date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    with ZipFile(output, "w") as package:
        package.writestr(info, payload)
    return output.getvalue()


def _private_key(check: bool, env_name: str) -> Ed25519PrivateKey:
    if check:
        return Ed25519PrivateKey.generate()
    encoded = str(os.getenv(env_name) or "").strip()
    if not encoded:
        raise SystemExit(f"{env_name} must contain a base64-encoded PEM Ed25519 private key")
    try:
        loaded = serialization.load_pem_private_key(
            base64.b64decode(encoded, validate=True), password=None
        )
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ValueError("key is not Ed25519")
        return loaded
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{env_name} is not a valid Ed25519 private key") from exc


def build(output_dir: Path, *, check: bool, env_name: str, key_id: str) -> tuple[Path, Path]:
    source = _normalized(json.loads(SOURCE.read_text(encoding="utf-8")))
    release_ref = str(os.getenv("GITHUB_REF_NAME") or "").strip()
    expected_ref = f"experience-air-quality-v{source['identity']['version']}"
    if release_ref and release_ref != expected_ref:
        raise SystemExit(
            f"release ref {release_ref!r} does not match package version; expected {expected_ref!r}"
        )
    archive = _archive(source)
    private_key = _private_key(check, env_name)
    manifest = {
        **source,
        "artifact": {
            "digest": f"sha256:{hashlib.sha256(archive).hexdigest()}",
            "size_bytes": len(archive),
            "media_type": "application/vnd.piphi.widget-package+zip",
            "key_id": key_id,
            "signature": None,
        },
    }
    signing_payload = SIGNING_CONTEXT + json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = private_key.sign(signing_payload)
    private_key.public_key().verify(signature, signing_payload)
    manifest["artifact"]["signature"] = base64.b64encode(signature).decode("ascii")

    version = source["identity"]["version"]
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"airthings-air-quality-{version}.zip"
    manifest_path = output_dir / f"airthings-air-quality-{version}.manifest.json"
    archive_path.write_bytes(archive)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print(f"archive={archive_path}")
    print(f"manifest={manifest_path}")
    print(f"digest={manifest['artifact']['digest']}")
    print(f"publisher_public_key={base64.b64encode(public_key).decode('ascii')}")
    return archive_path, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--private-key-env", default="PIPHI_WIDGET_SIGNING_KEY_PEM_BASE64")
    parser.add_argument("--key-id", default="piphi-release-1")
    args = parser.parse_args()
    if args.check:
        with tempfile.TemporaryDirectory(prefix="piphi-airthings-experience-") as tmp:
            build(Path(tmp), check=True, env_name=args.private_key_env, key_id=args.key_id)
    else:
        build(
            args.output_dir,
            check=False,
            env_name=args.private_key_env,
            key_id=args.key_id,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
