#!/usr/bin/env python3
"""Fail closed unless Docker Hub authoritatively reports that a tag is absent."""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class RegistryCheckError(RuntimeError):
    """Raised when registry state cannot be established authoritatively."""


def _docker_hub_repository(image: str) -> str:
    repository = image.removeprefix("docker.io/").strip().strip("/")
    if not repository or "/" not in repository or ":" in repository or "@" in repository:
        raise ValueError("image must be an untagged Docker Hub namespace/repository")
    return repository


def docker_hub_tag_exists(
    image: str,
    tag: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> bool:
    repository = _docker_hub_repository(image)
    normalized_tag = str(tag).strip()
    if not normalized_tag or "/" in normalized_tag or "@" in normalized_tag:
        raise ValueError("tag must be a non-empty Docker tag")
    token_url = "https://auth.docker.io/token?" + urlencode(
        {
            "service": "registry.docker.io",
            "scope": f"repository:{repository}:pull",
        }
    )
    try:
        with opener(Request(token_url, headers={"Accept": "application/json"}), timeout=15) as response:
            token_payload = json.loads(response.read() or b"{}")
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RegistryCheckError(f"Docker Hub authentication check failed: {exc}") from exc
    token = str(token_payload.get("token") or token_payload.get("access_token") or "").strip()
    if not token:
        raise RegistryCheckError("Docker Hub authentication response omitted a bearer token")

    manifest_url = f"https://registry-1.docker.io/v2/{repository}/manifests/{normalized_tag}"
    request = Request(
        manifest_url,
        method="HEAD",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": ", ".join(
                (
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                )
            ),
        },
    )
    try:
        with opener(request, timeout=15) as response:
            status = int(response.status)
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise RegistryCheckError(
            f"Docker Hub manifest check returned indeterminate status {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RegistryCheckError(f"Docker Hub manifest check failed: {exc}") from exc
    if status == 200:
        return True
    raise RegistryCheckError(f"Docker Hub manifest check returned indeterminate status {status}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--tag", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    try:
        exists = docker_hub_tag_exists(arguments.image, arguments.tag)
    except (RegistryCheckError, ValueError) as exc:
        raise SystemExit(f"Refusing release because registry state is indeterminate: {exc}") from exc
    if exists:
        raise SystemExit(
            f"Refusing release because {arguments.image}:{arguments.tag} already exists and is immutable."
        )
    print(f"Confirmed absent: {arguments.image}:{arguments.tag}")
