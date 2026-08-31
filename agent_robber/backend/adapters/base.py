"""Provider adapter base class.

All provider adapters implement the same async ping interface so the
backend can health-check any upstream uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PingResult:
    """Standardised ping response."""
    success: bool
    latency_ms: int
    status_code: int
    detail: str


class BaseAdapter(ABC):
    """Interface that every provider adapter must satisfy."""

    provider_id: str = "base"

    @abstractmethod
    async def ping(
        self,
        base_url: str,
        api_key: str,
        model_string: str,
    ) -> PingResult:
        """Send a lightweight health-check to the upstream.

        Returns:
            PingResult with success flag, round-trip latency, HTTP status, and
            a human-readable detail string (either response snippet or error).
        """
