from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError

import pytest


def _load_guard_module():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "airthings_docker_tag_guard",
        root / "scripts" / "assert_docker_tag_absent.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status = status
        self._payload = json.dumps(payload or {}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self._payload


def _opener_with_manifest_result(result):
    calls = 0

    def opener(_request, *, timeout):
        nonlocal calls
        assert timeout == 15
        calls += 1
        if calls == 1:
            return _Response(200, {"token": "registry-token"})
        if isinstance(result, Exception):
            raise result
        return _Response(result)

    return opener


def test_tag_guard_reports_existing_and_definite_absence() -> None:
    module = _load_guard_module()

    assert module.docker_hub_tag_exists(
        "piphinetwork/airthings-consumer-cloud",
        "0.1.6",
        opener=_opener_with_manifest_result(200),
    ) is True
    not_found = HTTPError("https://registry.test", 404, "not found", {}, io.BytesIO())
    assert module.docker_hub_tag_exists(
        "piphinetwork/airthings-consumer-cloud",
        "0.1.6",
        opener=_opener_with_manifest_result(not_found),
    ) is False


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError("https://registry.test", 401, "unauthorized", {}, io.BytesIO()),
        HTTPError("https://registry.test", 429, "rate limited", {}, io.BytesIO()),
        URLError("network unavailable"),
    ],
)
def test_tag_guard_fails_closed_for_indeterminate_registry_results(failure) -> None:
    module = _load_guard_module()

    with pytest.raises(module.RegistryCheckError, match="indeterminate|manifest check failed"):
        module.docker_hub_tag_exists(
            "piphinetwork/airthings-consumer-cloud",
            "0.1.6",
            opener=_opener_with_manifest_result(failure),
        )


def test_tag_guard_fails_closed_when_authentication_is_unavailable() -> None:
    module = _load_guard_module()

    def opener(_request, *, timeout):
        assert timeout == 15
        raise HTTPError("https://auth.test", 401, "unauthorized", {}, io.BytesIO())

    with pytest.raises(module.RegistryCheckError, match="authentication check failed"):
        module.docker_hub_tag_exists(
            "piphinetwork/airthings-consumer-cloud",
            "0.1.6",
            opener=opener,
        )
