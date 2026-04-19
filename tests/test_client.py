from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from piphi_network_airthings.cloud.client import (
    AirthingsCloudAuthError,
    AirthingsCloudClient,
    AirthingsCloudRequestError,
    AirthingsCredentials,
)


@pytest.mark.asyncio
async def test_list_devices_and_latest_sample_mapping() -> None:
    seen_requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((request.method, request.url.path, request.url.query.decode()))
        if request.url.path == "/v1/token":
            payload = request.read().decode("utf-8")
            assert "read:device:current_values" in payload
            return httpx.Response(
                200,
                json={
                    "access_token": "token-1",
                    "expires_in": 3600,
                },
            )
        if request.url.path == "/v1/accounts":
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "id": "account-1",
                        }
                    ]
                },
            )
        if request.url.path == "/v1/accounts/account-1/devices":
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {
                            "serialNumber": "2930046980",
                            "name": "Basement Wave Plus",
                            "type": "WAVE_PLUS",
                            "sensors": ["radonShortTermAvg", "temp", "humidity", "co2", "voc", "pressure"],
                        }
                    ]
                },
            )
        if request.url.path == "/v1/accounts/account-1/sensors":
            assert request.url.params.get("sn") == "2930046980"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "serialNumber": "2930046980",
                            "recorded": datetime(2026, 4, 18, 18, 30, tzinfo=UTC).isoformat(),
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
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as http_client:
        client = AirthingsCloudClient(
            accounts_base_url="https://example.test",
            consumer_base_url="https://example.test",
            http_client=http_client,
        )
        credentials = AirthingsCredentials(client_id="client-1", client_secret="secret-1")

        devices = await client.list_devices(credentials)
        sample = await client.latest_sample(credentials=credentials, serial_number="2930046980")

    assert len(devices) == 1
    assert devices[0].model_name == "Airthings Wave Plus"
    assert sample.metrics["radon_short_term_bqm3"] == 61.0
    assert sample.metrics["co2_ppm"] == 812.0
    assert sample.metrics["battery_percent"] == 94
    assert seen_requests == [
        ("POST", "/v1/token", ""),
        ("GET", "/v1/accounts", ""),
        ("GET", "/v1/accounts/account-1/devices", ""),
        ("GET", "/v1/accounts/account-1/sensors", "sn=2930046980"),
    ]


@pytest.mark.asyncio
async def test_token_rejection_raises_auth_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as http_client:
        client = AirthingsCloudClient(
            accounts_base_url="https://example.test",
            consumer_base_url="https://example.test",
            http_client=http_client,
        )
        with pytest.raises(AirthingsCloudAuthError):
            await client.list_devices(AirthingsCredentials(client_id="client-1", client_secret="secret-1"))


@pytest.mark.asyncio
async def test_latest_sample_404_raises_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 3600})
        if request.url.path == "/v1/accounts":
            return httpx.Response(200, json={"accounts": [{"id": "account-1"}]})
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as http_client:
        client = AirthingsCloudClient(
            accounts_base_url="https://example.test",
            consumer_base_url="https://example.test",
            http_client=http_client,
        )
        with pytest.raises(AirthingsCloudRequestError):
            await client.latest_sample(
                credentials=AirthingsCredentials(client_id="client-1", client_secret="secret-1"),
                serial_number="missing-serial",
            )
