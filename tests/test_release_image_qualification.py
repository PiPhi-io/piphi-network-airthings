from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _load_qualification_module():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "airthings_release_image_qualification",
        root / "scripts" / "qualify_release_image.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _image_inspect_payload(*, user: str = "piphi", version: str = "0.1.6") -> str:
    return json.dumps(
        [
            {
                "Config": {
                    "User": user,
                    "Volumes": {"/var/lib/piphi": {}},
                    "Labels": {
                        "org.opencontainers.image.version": version,
                        "org.opencontainers.image.revision": "a" * 40,
                        "io.piphi.manifest.sha256": "b" * 64,
                        "io.piphi.behaviors.sha256": "c" * 64,
                    },
                }
            }
        ]
    )


def test_release_image_metadata_validator_accepts_exact_non_root_contract(monkeypatch) -> None:
    module = _load_qualification_module()
    monkeypatch.setattr(
        module,
        "run_command",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": _image_inspect_payload()})(),
    )

    module.validate_image_metadata(
        "piphi-airthings:test",
        version="0.1.6",
        revision="a" * 40,
        manifest_sha256="b" * 64,
        behaviors_sha256="c" * 64,
    )


def test_release_image_metadata_validator_rejects_root_or_mismatched_release(monkeypatch) -> None:
    module = _load_qualification_module()
    monkeypatch.setattr(
        module,
        "run_command",
        lambda *_args, **_kwargs: type(
            "Result",
            (),
            {"stdout": _image_inspect_payload(user="root", version="0.1.5")},
        )(),
    )

    with pytest.raises(AssertionError, match="must run as piphi"):
        module.validate_image_metadata(
            "piphi-airthings:test",
            version="0.1.6",
            revision="a" * 40,
            manifest_sha256="b" * 64,
            behaviors_sha256="c" * 64,
        )
