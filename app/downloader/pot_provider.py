"""Proof of Origin (PO-token) provider implementations."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class HttpPOTokenProvider:
    """Fetches YouTube PO-tokens from an external HTTP provider service.

    Compatible with standard self-hosted PO-token generators (e.g. bgutil or similar
    services that expose JSON responses containing po_token and optional visitor_data).
    """

    def __init__(
        self,
        endpoint_url: str,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.timeout = timeout
        self._client = client

    def __call__(self) -> tuple[str, str | None] | None:
        return self.get_token()

    def get_token(self) -> tuple[str, str | None] | None:
        """Call external endpoint to retrieve a fresh PO-token and visitor_data."""
        try:
            if self._client:
                response = self._client.get(self.endpoint_url, timeout=self.timeout)
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(self.endpoint_url)

            response.raise_for_status()
            data: dict[str, Any] = response.json()

            token = data.get("po_token") or data.get("token")
            visitor_data = data.get("visitor_data")

            if not token:
                logger.warning(
                    "HTTP PO-token provider %s returned JSON without token key: %s",
                    self.endpoint_url,
                    data,
                )
                return None

            return str(token), str(visitor_data) if visitor_data else None

        except Exception as exc:
            logger.error(
                "Failed to fetch PO-token from provider %s: %s",
                self.endpoint_url,
                exc,
            )
            return None
