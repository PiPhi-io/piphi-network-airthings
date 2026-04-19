from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from piphi_network_airthings.app import app
from piphi_network_airthings.cloud.client import AirthingsCloudClient
from piphi_network_airthings.runtime import reset_runtime_state, set_cloud_client


class FakeAirthingsCloudClient(AirthingsCloudClient):
    def __init__(self) -> None:
        super().__init__(http_client=AsyncClient())
        self.devices: dict[str, dict] = {}
        self.samples: dict[str, dict] = {}
        self.latest_sample_failures: dict[str, Exception] = {}
        self.list_devices_calls: list[str] = []
        self.latest_sample_calls: list[str] = []

    async def aclose(self) -> None:
        return None

    async def list_devices(self, credentials):
        self.list_devices_calls.append(credentials.client_id)
        from piphi_network_airthings.cloud.models import AirthingsCloudDevice

        return [
            AirthingsCloudDevice(
                serial_number=record["serialNumber"],
                name=record["name"],
                device_type=record.get("type"),
                sensors=list(record.get("sensors") or []),
                home=record.get("home"),
                segment_name=record.get("segmentName"),
                raw=dict(record),
            )
            for record in self.devices.values()
        ]

    async def latest_sample(self, *, credentials, serial_number):
        del credentials
        self.latest_sample_calls.append(serial_number)
        if serial_number in self.latest_sample_failures:
            raise self.latest_sample_failures[serial_number]
        from piphi_network_airthings.cloud.models import AirthingsLatestSample

        payload = self.samples[serial_number]
        return AirthingsLatestSample.from_api_payload(serial_number, payload)


@pytest.fixture
def fake_cloud_client() -> FakeAirthingsCloudClient:
    client = FakeAirthingsCloudClient()
    set_cloud_client(client)
    return client


@pytest_asyncio.fixture(autouse=True)
async def _reset_runtime(fake_cloud_client: FakeAirthingsCloudClient) -> AsyncIterator[None]:
    del fake_cloud_client
    await reset_runtime_state()
    yield
    await reset_runtime_state()


@pytest_asyncio.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client
