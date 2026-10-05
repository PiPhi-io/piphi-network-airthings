#!/usr/bin/env python3
"""Verify that a promoted Docker image is the qualified multi-arch digest."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from typing import Any


def run_command(*command: str) -> str:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def validate_published_image(
    inspection: str,
    images: dict[str, Any],
    *,
    digest: str,
    version: str,
    revision: str,
    manifest_sha256: str,
    behaviors_sha256: str,
) -> None:
    digest_match = re.search(r"(?m)^Digest:\s+(sha256:[0-9a-f]{64})\s*$", inspection)
    if digest_match is None or digest_match.group(1) != digest:
        raise AssertionError(
            f"promoted digest {digest_match.group(1) if digest_match else None!r} "
            f"does not match qualified digest {digest!r}"
        )
    platforms = set(re.findall(r"(?m)^\s*Platform:\s+([^\s]+)\s*$", inspection))
    required_platforms = {"linux/amd64", "linux/arm64"}
    if not required_platforms <= platforms:
        raise AssertionError(f"published image platforms {platforms!r} omit {required_platforms!r}")
    if inspection.count("vnd.docker.reference.type:   attestation-manifest") < 2:
        raise AssertionError("published image does not expose attestations for both architectures")

    expected_labels = {
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": revision,
        "io.piphi.manifest.sha256": manifest_sha256,
        "io.piphi.behaviors.sha256": behaviors_sha256,
    }
    for platform in sorted(required_platforms):
        image = images.get(platform)
        if not isinstance(image, dict):
            raise AssertionError(f"published image metadata omitted {platform}")
        config = image.get("config") or {}
        if config.get("User") != "piphi":
            raise AssertionError(f"published {platform} image does not run as piphi")
        if "/var/lib/piphi" not in (config.get("Volumes") or {}):
            raise AssertionError(f"published {platform} image omits the persistent PiPhi volume")
        labels = config.get("Labels") or {}
        for label, expected in expected_labels.items():
            if labels.get(label) != expected:
                raise AssertionError(
                    f"published {platform} label {label}={labels.get(label)!r}, "
                    f"expected {expected!r}"
                )


def verify(args: argparse.Namespace) -> None:
    inspection = run_command("docker", "buildx", "imagetools", "inspect", args.image)
    image_json = run_command(
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        args.image,
        "--format",
        "{{json .Image}}",
    )
    images = json.loads(image_json)
    if not isinstance(images, dict):
        raise AssertionError("published image inspection did not return per-platform metadata")
    validate_published_image(
        inspection,
        images,
        digest=args.digest,
        version=args.version,
        revision=args.revision,
        manifest_sha256=args.manifest_sha256,
        behaviors_sha256=args.behaviors_sha256,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--behaviors-sha256", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    verify(parse_args())
    print("Published image matches the qualified digest, platforms, attestations, and labels.")
