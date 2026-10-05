from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from piphi_network_airthings.cloud.client import (
    ACCOUNTS_BASE_URL_ENV,
    ALLOW_INSECURE_TEST_ENDPOINTS_ENV,
    CONSUMER_BASE_URL_ENV,
    AirthingsCloudAuthError,
    AirthingsCloudClient,
    AirthingsCloudRequestError,
    AirthingsCredentials,
    airthings_cloud_client_from_environment,
)


def test_runtime_client_uses_https_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv(ACCOUNTS_BASE_URL_ENV, "https://accounts.example.test/")
    monkeypatch.setenv(CONSUMER_BASE_URL_ENV, "https://consumer.example.test/")

    client = airthings_cloud_client_from_environment()

    assert client.accounts_base_url == "https://accounts.example.test"
    assert client.consumer_base_url == "https://consumer.example.test"


def test_runtime_client_rejects_insecure_override_without_test_opt_in(monkeypatch) -> None:
    monkeypatch.setenv(ACCOUNTS_BASE_URL_ENV, "http://127.0.0.1:38001")

    with pytest.raises(ValueError, match=ALLOW_INSECURE_TEST_ENDPOINTS_ENV):
        airthings_cloud_client_from_environment()


def test_runtime_client_allows_insecure_fixture_with_explicit_test_opt_in(monkeypatch) -> None:
    monkeypatch.setenv(ACCOUNTS_BASE_URL_ENV, "http://127.0.0.1:38001/")
    monkeypatch.setenv(CONSUMER_BASE_URL_ENV, "http://127.0.0.1:38001/")
    monkeypatch.setenv(ALLOW_INSECURE_TEST_ENDPOINTS_ENV, "true")

    client = airthings_cloud_client_from_environment()

    assert client.accounts_base_url == "http://127.0.0.1:38001"
    assert client.consumer_base_url == "http://127.0.0.1:38001"


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


@pytest.mark.asyncio
async def test_secret_rotation_uses_distinct_token_and_account_cache_entries() -> None:
    token_requests = 0
    account_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests, account_requests
        if request.url.path == "/v1/token":
            token_requests += 1
            return httpx.Response(
                200,
                json={"access_token": f"token-{token_requests}", "expires_in": 3600},
            )
        if request.url.path == "/v1/accounts":
            account_requests += 1
            return httpx.Response(200, json={"accounts": [{"id": f"account-{account_requests}"}]})
        if request.url.path.endswith("/devices"):
            return httpx.Response(200, json={"devices": []})
        raise AssertionError(f"Unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as http_client:
        client = AirthingsCloudClient(
            accounts_base_url="https://example.test",
            consumer_base_url="https://example.test",
            http_client=http_client,
        )
        await client.list_devices(AirthingsCredentials("shared-client", "old-secret"))
        await client.list_devices(AirthingsCredentials("shared-client", "new-secret"))

    assert token_requests == 2
    assert account_requests == 2


@pytest.mark.asyncio
async def test_upstream_error_body_is_not_exposed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            return httpx.Response(
                500,
                text="upstream-secret-body",
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as http_client:
        client = AirthingsCloudClient(
            accounts_base_url="https://example.test",
            consumer_base_url="https://example.test",
            retry_attempts=1,
            http_client=http_client,
        )
        with pytest.raises(AirthingsCloudRequestError) as exc_info:
            await client.list_devices(AirthingsCredentials("client-1", "secret-1"))

    assert "upstream-secret-body" not in str(exc_info.value)
    assert str(exc_info.value) == "Airthings token request failed with status 500."
