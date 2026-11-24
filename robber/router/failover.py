"""Automated failover matrix: retry, rotate keys, then hop models."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.config import ROUTER_FAILOVER_ATTEMPTS, TURN_RETRY_BASE_SECONDS
from robber.jail import workspace_root
from robber.router.key_rotator import APIKeyRotator, KeyLease
from robber.vault import LOCAL_PROVIDERS
from robber.router.model_router import ModelRouter, RouteDecision, RouteTarget
from robber.router.provider_proxy import ModelRequest, ModelResponse, ProviderError, UnifiedProviderProxy
from robber.state_manager import StateManager

logger = logging.getLogger(__name__)

MAX_FALLBACK_DEPTH = 3


class ProviderExhaustionError(Exception):
    """Every registered route failed or was skipped. Do not retry the same chain."""

    def __init__(self, detail: str, events: list[FailoverEvent] | None = None) -> None:
        self.detail = (detail or "no usable API keys").strip() or "no usable API keys"
        self.events = list(events or [])
        super().__init__(f"All API providers failed: {self.detail}")

    def public(self) -> dict[str, Any]:
        return {
            "ok": False,
            "kind": "exhausted",
            "error": str(self),
            "events": [item.public() for item in self.events],
        }


@dataclass
class FailoverEvent:
    provider: str
    label: str
    status: int
    kind: str
    action: str

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "label": self.label,
            "status": self.status,
            "kind": self.kind,
            "action": self.action,
        }


@dataclass
class FailoverResult:
    response: ModelResponse | None
    decision: RouteDecision
    events: list[FailoverEvent] = field(default_factory=list)
    switched: bool = False
    error: str = ""
    exhaustion: ProviderExhaustionError | None = None

    def public(self) -> dict[str, Any]:
        payload = {
            "ok": self.response is not None,
            "switched": self.switched,
            "model": self.response.model if self.response else "",
            "provider": self.response.provider if self.response else "",
            "events": [item.public() for item in self.events],
            "route": self.decision.public(),
            "error": self.error,
            "exhausted": self.exhaustion is not None,
        }
        if self.exhaustion is not None:
            payload["exhaustion"] = self.exhaustion.public()
        return payload


class FailoverMatrix:
    """Primary -> fallback models without dropping the in-flight task identity."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        router: ModelRouter | None = None,
        proxy: UnifiedProviderProxy | Any | None = None,
        rotator: APIKeyRotator | None = None,
        manager: StateManager | None = None,
        sleep: Any = None,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.router = router or ModelRouter()
        self.proxy = proxy or UnifiedProviderProxy()
        if rotator is None:
            from robber.security.vault import iter_secured_keys

            rotator = APIKeyRotator(keys_fn=iter_secured_keys)
        self.rotator = rotator
        self.manager = manager
        self._sleep = sleep or asyncio.sleep

    async def execute(
        self,
        prompt: str,
        *,
        task: dict[str, Any] | None = None,
        task_type: str | None = None,
        prefer: str | None = None,
        max_attempts: int | None = None,
        messages: list[dict[str, str]] | None = None,
    ) -> FailoverResult:
        decision = self.router.choose(task, prompt=prompt, task_type=task_type, prefer=prefer)
        try:
            from robber.local.ollama_adapter import attach_local_fallbacks

            decision = attach_local_fallbacks(decision)
        except Exception:
            pass
        events: list[FailoverEvent] = []
        attempts = max(1, min(MAX_FALLBACK_DEPTH, int(max_attempts or ROUTER_FAILOVER_ATTEMPTS)))
        switched = False
        last_exc: BaseException | None = None
        http_hops = 0
        walked = 0
        walk_cap = max(attempts * 4, attempts + 1)
        for target in decision.chain:
            walked += 1
            if walked > walk_cap:
                break
            local = (target.provider or "").strip().lower() in LOCAL_PROVIDERS
            if http_hops >= attempts and not local:
                events.append(FailoverEvent(target.provider, target.label, 0, "depth", "skip"))
                switched = True
                continue
            skip_reason = _skip_reason(target)
            if skip_reason:
                logger.debug("skip %s: %s", target.label or target.provider, skip_reason)
                events.append(FailoverEvent(target.provider, target.label, 0, "no_key", "skip"))
                self._log(task, f"skip {target.label}", skip_reason, events[-1])
                switched = True
                continue
            if not local and not self.rotator.has_usable_key(target.provider):
                logger.debug("skip %s: API key missing", target.provider or target.label)
                events.append(FailoverEvent(target.provider, target.label, 0, "no_key", "skip"))
                self._log(task, f"skip {target.label}", "no usable key", events[-1])
                switched = True
                continue
            lease = self.rotator.acquire(target.provider)
            if lease is None:
                logger.debug("skip %s: no usable key", target.provider or target.label)
                events.append(FailoverEvent(target.provider, target.label, 0, "no_key", "skip"))
                self._log(task, f"skip {target.label}", "no usable key", events[-1])
                switched = True
                continue
            if http_hops >= attempts and not local:
                break
            http_hops += 1
            try:
                response = await self._call(target, lease, decision.prompt, messages=messages)
            except ProviderError as exc:
                last_exc = exc
                from robber.orchestrator import http_status_of

                status = http_status_of(exc) or exc.status_code
                kind = exc.kind or _classify(exc, status)
                action = self._recover(lease, kind, status)
                events.append(FailoverEvent(target.provider, target.label, status or 0, kind, action))
                logger.error(
                    "[ROUTER ERROR] Provider %s failed with status %s: %s",
                    target.provider or target.label,
                    status or 0,
                    str(exc)[:400],
                )
                self._log(task, f"failover {target.label}", f"{kind} HTTP {status}", events[-1])
                if action == "retry_key" and http_hops < attempts:
                    alt = self.rotator.acquire(target.provider)
                    if alt is not None and alt.index != lease.index:
                        http_hops += 1
                        try:
                            response = await self._call(target, alt, decision.prompt, messages=messages)
                            self.rotator.record(alt, response.total_tokens, status=200)
                            return FailoverResult(response, decision, events, switched=True)
                        except ProviderError as retry_exc:
                            last_exc = retry_exc
                            logger.error(
                                "[ROUTER ERROR] Provider %s failed with status %s: %s",
                                target.provider or target.label,
                                retry_exc.status_code or 0,
                                str(retry_exc)[:400],
                            )
                            self.rotator.cooldown(alt)
                switched = True
                if http_hops >= attempts and not local:
                    break
                if kind == "network" or (kind == "server" and http_hops == 1):
                    await self._sleep(TURN_RETRY_BASE_SECONDS)
                continue
            except Exception as exc:
                last_exc = exc
                from robber.orchestrator import is_transient_error

                kind = "network" if is_transient_error(exc) else "error"
                events.append(FailoverEvent(target.provider, target.label, 0, kind, "fallback_model"))
                logger.error(
                    "[ROUTER ERROR] Provider %s failed with status %s: %s",
                    target.provider or target.label,
                    0,
                    f"{type(exc).__name__}: {exc}"[:400],
                )
                self._log(task, f"failover {target.label}", f"{type(exc).__name__}", events[-1])
                switched = True
                if http_hops >= attempts and not local:
                    break
                continue
            self.rotator.record(lease, response.total_tokens, status=200)
            if events:
                self._log(task, f"recover {target.label}", "ok after failover", events[-1])
            return FailoverResult(response, decision, events, switched=switched)
        return self._exhausted(decision, events, last_exc, switched=True, task=task)

    def _exhausted(
        self,
        decision: RouteDecision,
        events: list[FailoverEvent],
        last_exc: BaseException | None,
        *,
        switched: bool,
        task: dict[str, Any] | None = None,
    ) -> FailoverResult:
        detail = _exhausted_detail(events, last_exc)
        exhaustion = ProviderExhaustionError(detail, events)
        logger.error("[ROUTER ERROR] Provider %s failed with status %s: %s", "all", 0, detail)
        if last_exc is not None:
            self._log(task, "failover exhausted", type(last_exc).__name__, None)
        else:
            self._log(task, "failover exhausted", detail, None)
        return FailoverResult(
            None,
            decision,
            events,
            switched=switched,
            error=str(exhaustion),
            exhaustion=exhaustion,
        )

    async def _call(
        self,
        target: RouteTarget,
        lease: KeyLease,
        prompt: str,
        *,
        messages: list[dict[str, str]] | None = None,
    ) -> ModelResponse:
        local = target.provider in LOCAL_PROVIDERS
        request = ModelRequest(
            prompt=prompt,
            model=target.model,
            provider=target.provider,
            api_key=lease.secret or None,
            api_base=target.api_base or None,
            protocol=target.protocol,
            max_tokens=512 if local else None,
            messages=messages,
        )
        return await self.proxy.generate(request)

    def _recover(self, lease: KeyLease, kind: str, status: int) -> str:
        if kind == "rate_limit" or status == 429:
            self.rotator.cooldown(lease)
            return "retry_key"
        if kind in {"server", "network", "overflow"} or (status or 0) >= 500:
            return "fallback_model"
        return "fallback_model"

    def _log(self, task: dict[str, Any] | None, choice: str, rationale: str, event: FailoverEvent | None) -> None:
        if self.manager is None:
            return
        extra = event.public() if event is not None else {}
        task_id = str((task or {}).get("id") or "") or None
        try:
            self.manager.record_decision(
                choice[:80],
                rationale=rationale[:800],
                task_id=task_id,
                branch="router",
                version_tag="failover",
                payload=str(extra)[:800],
            )
        except Exception:
            return


def _skip_reason(target: RouteTarget) -> str:
    provider = (target.provider or "").strip().lower()
    if provider in {"fallback", "none", ""}:
        return "dummy fallback route pruned"
    if not (target.model or "").strip():
        return "model id is empty"
    if provider in LOCAL_PROVIDERS and not _local_target_ready(target):
        return "local model server is not ready"
    return ""


def _local_target_ready(target: RouteTarget) -> bool:
    try:
        from robber.local.ollama_adapter import get_adapter

        adapter = get_adapter()
    except Exception:
        return False
    base = (target.api_base or "").rstrip("/")
    for row in adapter.endpoints:
        if not row.ok:
            continue
        host = (row.host or "").rstrip("/")
        if base and host and base == host:
            return True
        if not base and row.kind in {(target.provider or "").strip().lower(), "ollama"}:
            return True
    return False


def _exhausted_detail(events: list[FailoverEvent], last_exc: BaseException | None) -> str:
    parts: list[str] = []
    for item in events:
        if item.action == "skip":
            continue
        label = item.label or item.provider or "provider"
        status = f" HTTP {item.status}" if item.status else ""
        parts.append(f"{label}{status} ({item.kind})")
    if last_exc is not None:
        parts.append(f"{type(last_exc).__name__}: {last_exc}"[:240])
    if not parts:
        skipped = [item.label or item.provider for item in events if item.action == "skip"]
        if skipped:
            return "no usable API keys (" + ", ".join(skipped[:6]) + ")"
        return "no usable API keys"
    return "; ".join(parts[:6])


def _classify(exc: ProviderError, status: int | None) -> str:
    from robber.orchestrator import is_transient_error
    if exc.kind and exc.kind != "error":
        return exc.kind
    code = int(status or 0)
    if code == 429:
        return "rate_limit"
    if code >= 500:
        return "server"
    text = str(exc).lower()
    if "context" in text or "token" in text and "max" in text:
        return "overflow"
    if is_transient_error(exc):
        return "network"
    return "error"
