#!/usr/bin/env python3
"""Exercise an exact Airthings release image against an isolated vendor fixture."""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from piphi_runtime_kit_python import build_runtime_auth_headers


SERIAL_NUMBER = "2930046980"
CONFIG_ID = "cfg-release-qualification"
CONTAINER_ID = "airthings-release-qualification"
INTERNAL_TOKEN = "airthings-release-runtime-token"
CLIENT_ID = "fixture-client"
CLIENT_SECRET = "fixture-secret"
ACCESS_TOKEN = "fixture-access-token"
IDEMPOTENCY_KEY = "airthings-release-refresh-v1"


@dataclass
class FixtureState:
    token_requests: int = 0
    device_requests: int = 0
    sample_requests: int = 0
    errors: list[str] = field(default_factory=list)


class AirthingsFixtureHandler(BaseHTTPRequestHandler):
    server_version = "AirthingsQualificationFixture/1.0"

    @property
    def fixture_state(self) -> FixtureState:
        return getattr(self.server, "fixture_state")

    def log_message(self, _format: str, *_args: Any) -> None:
        return None

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {ACCESS_TOKEN}":
            return True
        self.fixture_state.errors.append(f"unauthorized {self.command} {self.path}")
        self._json(401, {"error": "unauthorized"})
        return False

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/v1/token":
            self._json(404, {"error": "not_found"})
            return
        expected_basic = base64.b64encode(
            f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
        ).decode("ascii")
        if self.headers.get("Authorization") != f"Basic {expected_basic}":
            self.fixture_state.errors.append("invalid fixture client credentials")
            self._json(401, {"error": "invalid_client"})
            return
        content_length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(content_length) or b"{}")
        if payload.get("client_id") != CLIENT_ID or payload.get("client_secret") != CLIENT_SECRET:
            self.fixture_state.errors.append("token payload credentials did not match")
            self._json(401, {"error": "invalid_client"})
            return
        self.fixture_state.token_requests += 1
        self._json(200, {"access_token": ACCESS_TOKEN, "expires_in": 3600})

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = self.path.split("?", 1)[0]
        if not self._authorized():
            return
        if path == "/v1/accounts":
            self._json(200, {"accounts": [{"id": "account-1"}]})
            return
        if path == "/v1/accounts/account-1/devices":
            self.fixture_state.device_requests += 1
            self._json(
                200,
                {
                    "devices": [
                        {
                            "serialNumber": SERIAL_NUMBER,
                            "name": "Qualification Wave Plus",
                            "type": "WAVE_PLUS",
                            "sensors": [
                                "radonShortTermAvg",
                                "temp",
                                "humidity",
                                "co2",
                                "voc",
                                "pressure",
                            ],
                        }
                    ]
                },
            )
            return
        if path == "/v1/accounts/account-1/sensors":
            self.fixture_state.sample_requests += 1
            self._json(
                200,
                {
                    "results": [
                        {
                            "serialNumber": SERIAL_NUMBER,
                            "recorded": "2026-10-01T12:00:00+00:00",
                            "batteryPercentage": 94,
                            "sensors": [
                                {"sensorType": "radonShortTermAvg", "value": 61, "unit": "Bq/m3"},
                                {"sensorType": "temp", "value": 21.4, "unit": "C"},
                                {"sensorType": "humidity", "value": 44, "unit": "%"},
                                {"sensorType": "co2", "value": 812, "unit": "ppm"},
                                {"sensorType": "voc", "value": 88, "unit": "ppb"},
                                {"sensorType": "pressure", "value": 1011.5, "unit": "hPa"},
                            ],
                        }
                    ]
                },
            )
            return
        self.fixture_state.errors.append(f"unexpected fixture route {self.path}")
        self._json(404, {"error": "not_found"})


def run_command(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=True,
    )


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            status = response.status
            raw_body = response.read()
    except HTTPError as exc:
        status = exc.code
        raw_body = exc.read()
    if status != expected_status:
        raise AssertionError(
            f"{method} {url} returned {status}, expected {expected_status}: "
            f"{raw_body.decode('utf-8', errors='replace')}"
        )
    parsed = json.loads(raw_body or b"{}")
    if not isinstance(parsed, dict):
        raise AssertionError(f"{method} {url} did not return a JSON object")
    return parsed


def wait_for_health(base_url: str, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return request_json(f"{base_url}/health")
        except (AssertionError, URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(0.25)
    raise AssertionError(f"release image did not become healthy: {last_error}")


def validate_image_metadata(
    image: str,
    *,
    version: str,
    revision: str,
    manifest_sha256: str,
    behaviors_sha256: str,
) -> None:
    inspected = json.loads(run_command("docker", "image", "inspect", image).stdout)[0]
    config = inspected.get("Config") or {}
    labels = config.get("Labels") or {}
    expected_labels = {
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": revision,
        "io.piphi.manifest.sha256": manifest_sha256,
        "io.piphi.behaviors.sha256": behaviors_sha256,
    }
    if config.get("User") != "piphi":
        raise AssertionError(f"release image must run as piphi, got {config.get('User')!r}")
    if "/var/lib/piphi" not in (config.get("Volumes") or {}):
        raise AssertionError("release image does not declare the persistent PiPhi volume")
    for label, expected in expected_labels.items():
        if labels.get(label) != expected:
            raise AssertionError(
                f"release image label {label}={labels.get(label)!r}, expected {expected!r}"
            )


def start_container(
    *,
    image: str,
    container_name: str,
    volume_name: str,
    fixture_url: str,
    runtime_port: int,
) -> None:
    run_command(
        "docker",
        "run",
        "--detach",
        "--name",
        container_name,
        "--network",
        "host",
        "--volume",
        f"{volume_name}:/var/lib/piphi",
        "--env",
        f"PIPHI_AIRTHINGS_ACCOUNTS_BASE_URL={fixture_url}",
        "--env",
        f"PIPHI_AIRTHINGS_CONSUMER_BASE_URL={fixture_url}",
        "--env",
        "PIPHI_AIRTHINGS_ALLOW_INSECURE_TEST_ENDPOINTS=true",
        "--env",
        f"PIPHI_RUNTIME_PORT={runtime_port}",
        image,
    )


def stop_container(container_name: str) -> None:
    run_command("docker", "stop", "--time", "5", container_name, check=False)
    run_command("docker", "rm", "--force", container_name, check=False)


def configure_and_assert_state(base_url: str, headers: dict[str, str]) -> None:
    response = request_json(
        f"{base_url}/config",
        method="POST",
        headers=headers,
        payload={
            "id": CONFIG_ID,
            "container_id": CONTAINER_ID,
            "integration_id": "airthings-consumer-cloud-api",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "serial_number": SERIAL_NUMBER,
            "poll_interval_seconds": 86400,
        },
    )
    if response.get("config_id") != CONFIG_ID:
        raise AssertionError(f"config response did not preserve config identity: {response}")
    state = request_json(f"{base_url}/state", headers=headers)
    latest_state = ((state.get("entries") or {}).get(CONFIG_ID) or {}).get("latest_state") or {}
    if latest_state.get("co2_ppm") != 812.0 or latest_state.get("connected") is not True:
        raise AssertionError(f"release image state was not populated from the fixture: {state}")


def qualify(args: argparse.Namespace) -> dict[str, Any]:
    validate_image_metadata(
        args.image,
        version=args.version,
        revision=args.revision,
        manifest_sha256=args.manifest_sha256,
        behaviors_sha256=args.behaviors_sha256,
    )
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", args.runtime_port))

    fixture_state = FixtureState()
    fixture_server = ThreadingHTTPServer(("127.0.0.1", 0), AirthingsFixtureHandler)
    setattr(fixture_server, "fixture_state", fixture_state)
    fixture_thread = threading.Thread(target=fixture_server.serve_forever, daemon=True)
    fixture_thread.start()
    fixture_url = f"http://127.0.0.1:{fixture_server.server_port}"
    base_url = f"http://127.0.0.1:{args.runtime_port}"
    suffix = f"{os.getpid()}-{int(time.time())}"
    container_name = f"piphi-airthings-release-qualification-{suffix}"
    volume_name = f"piphi-airthings-release-ledger-{suffix}"
    auth_headers = dict(
        build_runtime_auth_headers(
            container_id=CONTAINER_ID,
            internal_token=INTERNAL_TOKEN,
        )
    )
    command_headers = {**auth_headers, "X-PiPhi-Idempotency-Key": IDEMPOTENCY_KEY}
    run_command("docker", "volume", "create", volume_name)
    try:
        start_container(
            image=args.image,
            container_name=container_name,
            volume_name=volume_name,
            fixture_url=fixture_url,
            runtime_port=args.runtime_port,
        )
        health = wait_for_health(base_url)
        contract = request_json(f"{base_url}/contract")
        request_json(f"{base_url}/state", expected_status=401)
        if health.get("version") != args.version and (
            (health.get("integration") or {}).get("version") != args.version
        ):
            raise AssertionError(f"health version does not match {args.version}: {health}")
        if contract.get("version") != args.version:
            raise AssertionError(f"contract version does not match {args.version}: {contract}")
        configure_and_assert_state(base_url, auth_headers)
        first_refresh = request_json(
            f"{base_url}/command",
            method="POST",
            headers=command_headers,
            payload={"command": "refresh", "entity_id": f"device:{CONFIG_ID}"},
        )
        if first_refresh.get("replayed") is not False:
            raise AssertionError(f"first refresh was unexpectedly replayed: {first_refresh}")

        stop_container(container_name)
        start_container(
            image=args.image,
            container_name=container_name,
            volume_name=volume_name,
            fixture_url=fixture_url,
            runtime_port=args.runtime_port,
        )
        wait_for_health(base_url)
        configure_and_assert_state(base_url, auth_headers)
        samples_before_replay = fixture_state.sample_requests
        replayed_refresh = request_json(
            f"{base_url}/command",
            method="POST",
            headers=command_headers,
            payload={"command": "refresh", "entity_id": f"device:{CONFIG_ID}"},
        )
        if replayed_refresh.get("replayed") is not True:
            raise AssertionError(f"refresh did not replay after restart: {replayed_refresh}")
        if fixture_state.sample_requests != samples_before_replay:
            raise AssertionError("replayed refresh called the Airthings fixture after restart")
        if fixture_state.errors:
            raise AssertionError(f"fixture observed invalid requests: {fixture_state.errors}")
        return {
            "status": "passed",
            "image": args.image,
            "version": args.version,
            "revision": args.revision,
            "device_requests": fixture_state.device_requests,
            "sample_requests": fixture_state.sample_requests,
            "restart_replay": True,
        }
    finally:
        stop_container(container_name)
        run_command("docker", "volume", "rm", "--force", volume_name, check=False)
        fixture_server.shutdown()
        fixture_server.server_close()
        fixture_thread.join(timeout=5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--behaviors-sha256", required=True)
    parser.add_argument("--runtime-port", type=int, default=3669)
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(qualify(parse_args()), sort_keys=True))
