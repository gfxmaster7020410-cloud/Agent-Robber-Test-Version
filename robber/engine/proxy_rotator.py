from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx


@dataclass
class ProxyConfig:
    id: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    protocol: str = "http"
    status: str = "active"
    latency_ms: int = 0
    fail_count: int = 0

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "ProxyConfig":
        host = str(raw.get("host") or "").strip()
        if not host:
            raise ValueError("proxy host is required")
        try:
            port = int(raw.get("port") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("proxy port must be an integer") from exc
        if port < 1 or port > 65535:
            raise ValueError("proxy port is out of range")
        protocol = str(raw.get("protocol") or "http").strip().lower() or "http"
        proxy_id = str(raw.get("id") or "").strip() or f"px-{uuid.uuid4().hex[:8]}"
        return cls(
            id=proxy_id,
            host=host,
            port=port,
            username=str(raw["username"]) if raw.get("username") else None,
            password=str(raw["password"]) if raw.get("password") else None,
            protocol=protocol,
            status=str(raw.get("status") or "active"),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "host": self.host,
            "port": self.port,
            "protocol": self.protocol,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "fail_count": self.fail_count,
            "auth": bool(self.username),
        }


class ProxyManager:
    def __init__(self) -> None:
        self.proxies: Dict[str, ProxyConfig] = {}
        self.profile_proxy_map: Dict[str, str] = {}

    def bootstrap_from_env(self) -> int:
        """Load optional ROBBER_PROXIES JSON array or a single ROBBER_PROXY_URL."""
        loaded = 0
        blob = os.environ.get("ROBBER_PROXIES", "").strip()
        if blob:
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError as exc:
                raise ValueError("ROBBER_PROXIES must be a JSON array") from exc
            if not isinstance(parsed, list):
                raise ValueError("ROBBER_PROXIES must be a JSON array")
            for item in parsed:
                if isinstance(item, dict):
                    self.register_proxy(ProxyConfig.from_mapping(item))
                    loaded += 1
        url = os.environ.get("ROBBER_PROXY_URL", "").strip()
        if url:
            self.register_proxy(self._from_url(url))
            loaded += 1
        return loaded

    @staticmethod
    def _from_url(url: str) -> ProxyConfig:
        from urllib.parse import urlparse

        parsed = urlparse(url if "://" in url else f"http://{url}")
        if not parsed.hostname or not parsed.port:
            raise ValueError("ROBBER_PROXY_URL must include host and port")
        return ProxyConfig(
            id="px-env",
            host=parsed.hostname,
            port=parsed.port,
            username=parsed.username,
            password=parsed.password,
            protocol=parsed.scheme or "http",
        )

    def register_proxy(self, proxy: ProxyConfig) -> ProxyConfig:
        self.proxies[proxy.id] = proxy
        return proxy

    def attach_saved(self, profile_id: str, proxy_id: str) -> Optional[ProxyConfig]:
        """Restore a persisted profile→proxy assignment. Missing proxies stay mapped."""
        profile_id = profile_id.strip()
        proxy_id = proxy_id.strip()
        if not profile_id or not proxy_id:
            return None
        self.profile_proxy_map[profile_id] = proxy_id
        return self.proxies.get(proxy_id)

    def assign_to_profile(self, profile_id: str, proxy_id: str | None = None) -> Optional[ProxyConfig]:
        """Attach a named proxy, or the least-used healthy proxy in the pool."""
        if proxy_id:
            proxy = self.proxies.get(proxy_id)
            if proxy is None:
                self.profile_proxy_map[profile_id] = proxy_id
                return None
            self.profile_proxy_map[profile_id] = proxy.id
            return proxy
        picked = self._least_used_active()
        if picked is None:
            return None
        self.profile_proxy_map[profile_id] = picked.id
        return picked

    def unassign(self, profile_id: str) -> None:
        self.profile_proxy_map.pop(profile_id, None)

    def _least_used_active(self) -> Optional[ProxyConfig]:
        active = [proxy for proxy in self.proxies.values() if proxy.status == "active"]
        pool = active or list(self.proxies.values())
        if not pool:
            return None
        usage = Counter(self.profile_proxy_map.values())
        return min(pool, key=lambda item: (usage[item.id], item.fail_count, item.id))

    async def check_proxy_health(self, proxy: ProxyConfig) -> bool:
        if proxy.username:
            proxy_url = f"{proxy.protocol}://{proxy.username}:{proxy.password}@{proxy.host}:{proxy.port}"
        else:
            proxy_url = f"{proxy.protocol}://{proxy.host}:{proxy.port}"

        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=5.0) as client:
                resp = await client.get("https://httpbin.org/ip")
                if resp.status_code == 200:
                    proxy.status = "active"
                    proxy.latency_ms = int(resp.elapsed.total_seconds() * 1000)
                    return True
        except TypeError:
            try:
                async with httpx.AsyncClient(
                    proxies={proxy.protocol: proxy_url}, timeout=5.0
                ) as client:
                    resp = await client.get("https://httpbin.org/ip")
                    if resp.status_code == 200:
                        proxy.status = "active"
                        proxy.latency_ms = int(resp.elapsed.total_seconds() * 1000)
                        return True
            except Exception:
                pass
        except Exception:
            pass

        proxy.status = "degraded"
        return False

    def get_proxy_for_profile(self, profile_id: str) -> Optional[ProxyConfig]:
        proxy_id = self.profile_proxy_map.get(profile_id)
        if proxy_id and proxy_id in self.proxies:
            return self.proxies[proxy_id]
        return next(iter(self.proxies.values()), None)

    def public_proxy(self, proxy_id: str) -> dict[str, Any] | None:
        proxy = self.proxies.get(proxy_id)
        if proxy is None:
            if proxy_id:
                return {"id": proxy_id, "status": "unregistered"}
            return None
        return proxy.public_dict()

    def mark_proxy_failed(self, profile_id: str, proxy_id: str) -> None:
        proxy = self.proxies.get(proxy_id)
        if proxy:
            proxy.fail_count += 1
            if proxy.fail_count >= 3:
                proxy.status = "degraded"
                replacement = self._least_used_active()
                if replacement and replacement.id != proxy_id:
                    self.profile_proxy_map[profile_id] = replacement.id

    def to_playwright_proxy(self, proxy: ProxyConfig) -> Dict[str, str]:
        payload: Dict[str, str] = {
            "server": f"{proxy.protocol}://{proxy.host}:{proxy.port}",
        }
        if proxy.username:
            payload["username"] = proxy.username
        if proxy.password:
            payload["password"] = proxy.password
        return payload
