from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


DIGEST = "sha256:" + "a" * 64
REVISION = "b" * 40
MANIFEST_SHA = "c" * 64
BEHAVIORS_SHA = "d" * 64


def _load_verifier_module():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "airthings_published_image_verifier",
        root / "scripts" / "verify_published_image.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _inspection(*, digest: str = DIGEST) -> str:
    return f"""Name: docker.io/piphinetwork/airthings-consumer-cloud:0.1.6
MediaType: application/vnd.oci.image.index.v1+json
Digest: {digest}
Manifests:
  Platform: linux/amd64
  Platform: linux/arm64
  Platform: unknown/unknown
    vnd.docker.reference.type:   attestation-manifest
  Platform: unknown/unknown
    vnd.docker.reference.type:   attestation-manifest
"""


def _images() -> dict:
    labels = {
        "org.opencontainers.image.version": "0.1.6",
        "org.opencontainers.image.revision": REVISION,
        "io.piphi.manifest.sha256": MANIFEST_SHA,
        "io.piphi.behaviors.sha256": BEHAVIORS_SHA,
    }
    image = {
        "config": {
            "User": "piphi",
            "Volumes": {"/var/lib/piphi": {}},
            "Labels": labels,
        }
    }
    return {"linux/amd64": image, "linux/arm64": image}


def _validate(module, inspection: str, images: dict) -> None:
    module.validate_published_image(
        inspection,
        images,
        digest=DIGEST,
        version="0.1.6",
        revision=REVISION,
        manifest_sha256=MANIFEST_SHA,
        behaviors_sha256=BEHAVIORS_SHA,
    )


def test_published_image_verifier_accepts_exact_multiarch_attested_digest() -> None:
    module = _load_verifier_module()
    _validate(module, _inspection(), _images())


def test_published_image_verifier_rejects_digest_drift() -> None:
    module = _load_verifier_module()

    with pytest.raises(AssertionError, match="does not match qualified digest"):
        _validate(module, _inspection(digest="sha256:" + "e" * 64), _images())


def test_published_image_verifier_rejects_missing_platform_or_attestation() -> None:
    module = _load_verifier_module()

    with pytest.raises(AssertionError, match="omit"):
        _validate(
            module,
            _inspection().replace("  Platform: linux/arm64\n", ""),
            _images(),
        )

    with pytest.raises(AssertionError, match="attestations"):
        _validate(
            module,
            _inspection().replace(
                "    vnd.docker.reference.type:   attestation-manifest\n",
                "",
                1,
            ),
            _images(),
        )
