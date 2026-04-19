from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .models import AirthingsCloudDevice, AirthingsLatestSample


logger = logging.getLogger(__name__)

DEFAULT_ACCOUNTS_BASE_URL = "https://accounts-api.airthings.com"
DEFAULT_CONSUMER_BASE_URL = "https://consumer-api.airthings.com"
DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 1.0
TOKEN_EXPIRY_SAFETY_SECONDS = 120


class AirthingsCloudError(RuntimeError):
    """Base error for Airthings consumer cloud operations."""


class AirthingsCloudAuthError(AirthingsCloudError):
    """Raised when Airthings consumer cloud credentials are rejected."""


class AirthingsCloudRateLimitError(AirthingsCloudError):
    """Raised when Airthings consumer cloud rate limits the client."""


class AirthingsCloudRequestError(AirthingsCloudError):
    """Raised for unexpected Airthings consumer cloud request failures."""


@dataclass(slots=True)
class AirthingsCredentials:
    client_id: str
    client_secret: str

    def cache_key(self) -> str:
        return self.client_id


@dataclass(slots=True)
class _TokenRecord:
    access_token: str
    expires_at: datetime

    def is_expired(self) -> bool:
        return datetime.now(tz=UTC) >= self.expires_at


class AirthingsCloudClient:
    def __init__(
        self,
        *,
        accounts_base_url: str = DEFAULT_ACCOUNTS_BASE_URL,
        consumer_base_url: str = DEFAULT_CONSUMER_BASE_URL,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.accounts_base_url = accounts_base_url.rstrip("/")
        self.consumer_base_url = consumer_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = max(int(retry_attempts), 1)
        self.retry_delay_seconds = max(float(retry_delay_seconds), 0.0)
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._token_cache: dict[str, _TokenRecord] = {}
        self._account_cache: dict[str, str] = {}

    async def aclose(self) -> None:
        if self._http_client is not None and self._owns_http_client:
            await self._http_client.aclose()
        self._http_client = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self.timeout_seconds)
            self._owns_http_client = True
        return self._http_client

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        client = await self._client()
        last_error: Exception | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                    params=params,
                )
                if response.status_code == 429:
                    if attempt >= self.retry_attempts:
                        raise AirthingsCloudRateLimitError("Airthings API rate limit exceeded.")
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else self.retry_delay_seconds * attempt
                    logger.warning(
                        "Airthings API rate limit on %s %s attempt=%s/%s retry_in_seconds=%.2f",
                        method,
                        url,
                        attempt,
                        self.retry_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status_code >= 500:
                    if attempt >= self.retry_attempts:
                        return response
                    delay = self.retry_delay_seconds * attempt
                    logger.warning(
                        "Airthings API server error on %s %s status=%s attempt=%s/%s retry_in_seconds=%.2f",
                        method,
                        url,
                        response.status_code,
                        attempt,
                        self.retry_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                return response
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= self.retry_attempts:
                    break
                delay = self.retry_delay_seconds * attempt
                logger.warning(
                    "Airthings API request error on %s %s attempt=%s/%s retry_in_seconds=%.2f error=%r",
                    method,
                    url,
                    attempt,
                    self.retry_attempts,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            raise AirthingsCloudRequestError(
                f"Airthings API request failed: {type(last_error).__name__}: {last_error}"
            ) from last_error
        raise AirthingsCloudRequestError(f"Airthings API request failed for {method} {url}")

    async def _access_token(self, credentials: AirthingsCredentials) -> str:
        cached = self._token_cache.get(credentials.cache_key())
        if cached is not None and not cached.is_expired():
            logger.debug(
                "Airthings token cache hit client_id=%s expires_at=%s",
                credentials.client_id,
                cached.expires_at.isoformat(),
            )
            return cached.access_token

        basic_token = base64.b64encode(
            f"{credentials.client_id}:{credentials.client_secret}".encode("utf-8")
        ).decode("ascii")
        response = await self._request(
            "POST",
            f"{self.accounts_base_url}/v1/token",
            headers={
                "Authorization": f"Basic {basic_token}",
                "Content-Type": "application/json",
            },
            json={
                "grant_type": "client_credentials",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "scope": ["read:device:current_values"],
            },
        )
        if response.status_code in {400, 401, 403}:
            raise AirthingsCloudAuthError("Airthings consumer cloud credentials were rejected.")
        if response.status_code >= 400:
            raise AirthingsCloudRequestError(
                f"Airthings token request failed with status {response.status_code}: {response.text}"
            )
        payload = response.json()
        access_token = str(payload.get("access_token") or "")
        expires_in = int(payload.get("expires_in") or 3600)
        if not access_token:
            raise AirthingsCloudRequestError("Airthings token response did not include an access_token.")
        expires_at = datetime.now(tz=UTC) + timedelta(
            seconds=max(expires_in - TOKEN_EXPIRY_SAFETY_SECONDS, 60)
        )
        self._token_cache[credentials.cache_key()] = _TokenRecord(
            access_token=access_token,
            expires_at=expires_at,
        )
        logger.info(
            "Airthings token refreshed client_id=%s expires_at=%s",
            credentials.client_id,
            expires_at.isoformat(),
        )
        return access_token

    def _clear_cached_auth(self, credentials: AirthingsCredentials) -> None:
        self._token_cache.pop(credentials.cache_key(), None)
        self._account_cache.pop(credentials.cache_key(), None)
        logger.warning("Airthings auth cache cleared client_id=%s", credentials.client_id)

    async def _account_id(self, credentials: AirthingsCredentials) -> str:
        cached_account_id = self._account_cache.get(credentials.cache_key())
        if cached_account_id:
            return cached_account_id

        access_token = await self._access_token(credentials)
        response = await self._request(
            "GET",
            f"{self.consumer_base_url}/v1/accounts",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code in {401, 403}:
            self._clear_cached_auth(credentials)
            raise AirthingsCloudAuthError(
                "Airthings consumer cloud token was rejected while listing accounts."
            )
        if response.status_code >= 400:
            raise AirthingsCloudRequestError(
                f"Airthings account lookup failed with status {response.status_code}: {response.text}"
            )

        payload = response.json()
        raw_accounts = payload.get("accounts") if isinstance(payload.get("accounts"), list) else payload
        if not isinstance(raw_accounts, list):
            raise AirthingsCloudRequestError("Airthings account lookup returned an unexpected payload.")

        for item in raw_accounts:
            if not isinstance(item, dict):
                continue
            account_id = str(item.get("id") or item.get("accountId") or "").strip()
            if account_id:
                self._account_cache[credentials.cache_key()] = account_id
                logger.info(
                    "Airthings account resolved client_id=%s account_id=%s",
                    credentials.client_id,
                    account_id,
                )
                return account_id

        raise AirthingsCloudRequestError("Airthings account lookup returned no account ids.")

    async def list_devices(self, credentials: AirthingsCredentials) -> list[AirthingsCloudDevice]:
        access_token = await self._access_token(credentials)
        account_id = await self._account_id(credentials)
        response = await self._request(
            "GET",
            f"{self.consumer_base_url}/v1/accounts/{account_id}/devices",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code in {401, 403}:
            self._clear_cached_auth(credentials)
            raise AirthingsCloudAuthError("Airthings consumer cloud token was rejected while listing devices.")
        if response.status_code >= 400:
            raise AirthingsCloudRequestError(
                f"Airthings device discovery failed with status {response.status_code}: {response.text}"
            )
        payload = response.json()
        raw_devices = payload.get("devices") if isinstance(payload.get("devices"), list) else payload
        devices: list[AirthingsCloudDevice] = []
        for item in raw_devices:
            if not isinstance(item, dict):
                continue
            serial_number = str(item.get("serialNumber") or item.get("serial_number") or "").strip()
            if not serial_number:
                continue
            devices.append(
                AirthingsCloudDevice(
                    serial_number=serial_number,
                    name=str(item.get("name") or item.get("deviceName") or serial_number),
                    device_type=item.get("type"),
                    sensors=list(item.get("sensors") or []),
                    home=item.get("home"),
                    segment_name=item.get("segmentName"),
                    raw=dict(item),
                )
            )
        logger.info(
            "Airthings devices listed client_id=%s account_id=%s discovered=%s",
            credentials.client_id,
            account_id,
            len(devices),
        )
        return devices

    async def latest_sample(
        self,
        *,
        credentials: AirthingsCredentials,
        serial_number: str,
    ) -> AirthingsLatestSample:
        access_token = await self._access_token(credentials)
        account_id = await self._account_id(credentials)
        response = await self._request(
            "GET",
            f"{self.consumer_base_url}/v1/accounts/{account_id}/sensors",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"sn": serial_number},
        )
        if response.status_code in {401, 403}:
            self._clear_cached_auth(credentials)
            raise AirthingsCloudAuthError(
                f"Airthings consumer cloud token was rejected while reading {serial_number}."
            )
        if response.status_code >= 400:
            raise AirthingsCloudRequestError(
                f"Airthings latest sample failed with status {response.status_code}: {response.text}"
            )
        payload = response.json()
        raw_results = payload.get("results") if isinstance(payload.get("results"), list) else payload
        if not isinstance(raw_results, list):
            raise AirthingsCloudRequestError("Airthings latest sample returned an unexpected payload.")

        for item in raw_results:
            if not isinstance(item, dict):
                continue
            item_serial = str(item.get("serialNumber") or item.get("serial_number") or "").strip()
            if item_serial == serial_number:
                logger.info(
                    "Airthings sample fetched client_id=%s account_id=%s serial_number=%s",
                    credentials.client_id,
                    account_id,
                    serial_number,
                )
                return AirthingsLatestSample.from_api_payload(serial_number, item)

        raise AirthingsCloudRequestError(
            f"Airthings device {serial_number} was not found in the cloud account."
        )
