"""Persona-based multi-model dynamic proxy router.

Dispatches LLM requests based on sub-worker specialization (Architect, Coder,
Auditor, Tester) and model availability. Integrates with the existing
ModelRouter / FailoverMatrix / UnifiedProviderProxy stack and the new
CostAwareRateLimiter for budget-aware routing.

Persona -> Model Tier Mapping:
  ArchitectWorker  -> "reason"  -> high-reasoning models (Claude, Gemini)
  CodeWorker       -> "codegen" -> high-speed coding models
  AuditorWorker    -> "reason"  -> analysis models
  TestWorker       -> "research"-> fast/cheap models
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.config import (
    LOCAL_OLLAMA_HOST,
    LOCAL_LMSTUDIO_HOST,
    PERSONA_ROUTE_MAP,
    LOCAL_FALLBACK_HOSTS,
)
from robber.jail import workspace_root
from robber.proxy.rate_limiter import CostAwareRateLimiter
from robber.router.failover import FailoverMatrix, FailoverResult
from robber.router.model_router import ModelRouter, RouteDecision
from robber.router.provider_proxy import ModelResponse
from robber.router.session import Engine, assign_engine, get_session_router

logger = logging.getLogger(__name__)

PERSONA_TIER_MAP: dict[str, str] = {
    "architect": "reason",
    "coder": "codegen",
    "auditor": "reason",
    "tester": "research",
    "vision_auditor": "research",
    "pr_writer": "codegen",
}

GENERIC_RESPONSE_PATTERNS = re.compile(
    r"(?i)\b(what kind of|what type of|can you tell me about|what are you looking to|"
    r"could you describe|what would you like|i'?m not sure what|"
    r"let me know what|how can I help|what do you need)\b",
)
DEFAULT_SESSION_ID = "default"


@dataclass
class PersonaRoute:
    worker_role: str
    task_type: str
    engine_label: str
    provider: str
    model: str
    cost_usd: float = 0.0
    tokens: int = 0
    latency_ms: float = 0.0
    switched: bool = False
    local_fallback: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "worker_role": self.worker_role,
            "task_type": self.task_type,
            "engine_label": self.engine_label,
            "provider": self.provider,
            "model": self.model,
            "cost_usd": round(self.cost_usd, 6),
            "tokens": self.tokens,
            "latency_ms": round(self.latency_ms, 1),
            "switched": self.switched,
            "local_fallback": self.local_fallback,
        }


class PersonaProxyRouter:
    """Route LLM requests based on worker persona and budget constraints.

    Sits between the swarm orchestrator and the existing FailoverMatrix,
    adding:
      1. Persona-based engine selection (Architect -> high-reasoning, etc.)
      2. Budget-aware local fallback when cost caps are hit
      3. Automatic token usage recording for the dashboard
      4. Mandatory SSOT system prompt injection on every request
      5. Stateful conversation history via SessionHistoryManager
      6. Context verification guardrail against generic responses
    """

    def __init__(
        self,
        *,
        root: Path | str | None = None,
        rate_limiter: CostAwareRateLimiter | None = None,
        failover: FailoverMatrix | None = None,
        ssot_path: Path | None = None,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self._rate_limiter = rate_limiter or CostAwareRateLimiter(ssot_path=ssot_path)
        self._failover = failover or FailoverMatrix(root=self.root)
        self._ssot_path = ssot_path
        self._dispatch_log: list[dict[str, Any]] = []
        self._max_log = 200

    @property
    def rate_limiter(self) -> CostAwareRateLimiter:
        return self._rate_limiter

    @property
    def failover(self) -> FailoverMatrix:
        return self._failover

    def persona_for_role(self, worker_role: str) -> str:
        return PERSONA_TIER_MAP.get(worker_role.strip().lower(), "research")

    def task_type_for_persona(self, worker_role: str) -> str:
        persona = self.persona_for_role(worker_role)
        mapping = {"reason": "REASONING", "codegen": "CODE_GEN", "research": "FAST_DRAFT"}
        return mapping.get(persona, "FAST_DRAFT")

    async def dispatch_llm_request(
        self,
        payload: dict[str, Any],
        *,
        worker_role: str = "coder",
        prompt: str = "",
        task_type: str | None = None,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Dispatch an LLM request through persona routing + failover.

        Automatically injects:
          - Mandatory SSOT system prompt header with workspace context chunks
          - Full session conversation history (stateful multi-turn)
          - Context verification guardrail against generic model responses

        Returns a dict with: response text, route info, cost, tokens, switched flag.
        """
        effective_session_id = (session_id or "").strip() or DEFAULT_SESSION_ID
        ssot_chunks = self._retrieve_ssot_chunks(prompt, payload)
        system_prompt = self._build_ssot_system_prompt(ssot_chunks, worker_role)
        history_messages = self._get_session_history(effective_session_id)
        user_message = {"role": "user", "content": prompt or str(payload.get("prompt", ""))}
        messages = [system_prompt] + history_messages + [user_message]

        if self._rate_limiter.should_use_local():
            result = await self._dispatch_local(
                payload, worker_role=worker_role, prompt=prompt, messages=messages,
            )
            self._record_turn(effective_session_id, user_message, result)
            return result

        effective_task_type = task_type or self.task_type_for_persona(worker_role)

        result: FailoverResult = await self._failover.execute(
            prompt,
            task=payload,
            task_type=effective_task_type,
            messages=messages,
        )

        route = PersonaRoute(
            worker_role=worker_role,
            task_type=effective_task_type,
            engine_label=result.decision.primary.label if result.decision else "",
            provider=result.response.provider if result.response else "",
            model=result.response.model if result.response else "",
            cost_usd=0.0,
            tokens=result.response.total_tokens if result.response else 0,
            switched=result.switched,
        )

        if result.response and route.provider:
            usage = self._rate_limiter.record_usage(
                provider=route.provider,
                model=route.model,
                input_tokens=result.response.prompt_tokens,
                output_tokens=result.response.completion_tokens,
                worker_role=worker_role,
            )
            route.cost_usd = usage.get("cost_usd", 0.0)

        self._log_dispatch(route)

        if result.response is None:
            logger.warning(
                "[PROXY] All providers exhausted for persona=%s: %s",
                worker_role, result.error or "no providers available",
            )
            fallback = await self._dispatch_local(
                payload, worker_role=worker_role, prompt=prompt, messages=messages,
            )
            self._record_turn(effective_session_id, user_message, fallback)
            return fallback

        response_text = result.response.text or ""
        if self._is_generic_response(response_text) and ssot_chunks:
            logger.warning(
                "[PROXY] Generic response detected, retrying with forced SSOT reading"
            )
            retry_result = await self._retry_with_forced_ssot(
                prompt, payload, effective_task_type, worker_role,
                ssot_chunks, effective_session_id, messages,
            )
            if retry_result is not None:
                self._record_turn(effective_session_id, user_message, retry_result)
                return retry_result

        output = {
            "ok": True,
            "text": response_text,
            "route": route.public(),
            "exhausted": False,
        }
        self._record_turn(effective_session_id, user_message, output)
        return output

    async def _dispatch_local(
        self,
        payload: dict[str, Any],
        *,
        worker_role: str = "coder",
        prompt: str = "",
        messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        engine = assign_engine(prompt or str(payload.get("prompt", "")), batch="eco")
        local_engine = Engine(
            label="local-fallback",
            litellm_model=engine.litellm_model or "ollama/llama3.2",
            provider="ollama",
            api_key=None,
            api_base=LOCAL_OLLAMA_HOST,
            fallback_label=None,
            batch_class="eco",
            protocol_type="OPENAI_COMPATIBLE",
            model_string=engine.model_string or "llama3.2",
        )

        from robber.router.provider_proxy import ModelRequest, UnifiedProviderProxy

        proxy = UnifiedProviderProxy(timeout=30.0)
        request = ModelRequest(
            prompt=prompt or str(payload.get("prompt", "")),
            model=local_engine.model_string or "llama3.2",
            provider="ollama",
            api_base=LOCAL_OLLAMA_HOST,
            protocol="OPENAI_COMPATIBLE",
            max_tokens=2048,
            messages=messages,
        )

        start = time.perf_counter()
        try:
            response = await proxy.generate(request)
            latency = (time.perf_counter() - start) * 1000.0

            self._rate_limiter.record_usage(
                provider="ollama",
                model=response.model,
                input_tokens=response.prompt_tokens,
                output_tokens=response.completion_tokens,
                worker_role=worker_role,
            )

            route = PersonaRoute(
                worker_role=worker_role,
                task_type="LOCAL_FALLBACK",
                engine_label="local-fallback",
                provider="ollama",
                model=response.model,
                cost_usd=0.0,
                tokens=response.total_tokens,
                latency_ms=latency,
                switched=True,
                local_fallback=True,
            )
            self._log_dispatch(route)

            return {
                "ok": True,
                "text": response.text,
                "route": route.public(),
                "exhausted": False,
            }
        except Exception as exc:
            logger.error("[PROXY] Local fallback failed: %s", exc)
            return {
                "ok": False,
                "text": "",
                "route": PersonaRoute(
                    worker_role=worker_role,
                    task_type="LOCAL_FALLBACK",
                    engine_label="local-fallback",
                    provider="ollama",
                    model="llama3.2",
                    local_fallback=True,
                ).public(),
                "error": str(exc),
                "exhausted": True,
            }

    def _log_dispatch(self, route: PersonaRoute) -> None:
        entry = {
            **route.public(),
            "timestamp": time.time(),
        }
        self._dispatch_log.append(entry)
        if len(self._dispatch_log) > self._max_log:
            self._dispatch_log = self._dispatch_log[-self._max_log:]

    def _retrieve_ssot_chunks(self, prompt: str, payload: dict[str, Any]) -> str:
        """Retrieve SSOT context chunks for the current request."""
        try:
            from robber.context_assembly import ContextAssembler, merge_payload

            task_payload = merge_payload(payload.get("payload_json")) if payload.get("payload_json") else {}
            files = []
            for key in ("files", "inputs", "outputs"):
                items = task_payload.get(key) or []
                if isinstance(items, list):
                    files.extend(str(f) for f in items if f)
            seen: list[str] = []
            for f in files:
                if f not in seen:
                    seen.append(f)
            files = seen[:8]

            assembler = ContextAssembler(self.root)
            codebase_lines: list[str] = []
            if files:
                for f in files[:5]:
                    try:
                        from robber.ssot_db import get_ssot_db

                        db = get_ssot_db()
                        entries = db.list_codebase(path=f, limit=3)
                        for entry in entries:
                            path = str(entry.get("path") or "")
                            lang = str(entry.get("language") or "")
                            summary = str(entry.get("ast_summary") or entry.get("symbol_index") or "")[:300]
                            if path:
                                codebase_lines.append(f"  {path} [{lang}] {summary}")
                    except Exception:
                        pass

            if not codebase_lines:
                try:
                    from robber.ssot_db import get_ssot_db

                    db = get_ssot_db()
                    entries = db.list_codebase(limit=6)
                    for entry in entries:
                        path = str(entry.get("path") or "")
                        lang = str(entry.get("language") or "")
                        summary = str(entry.get("ast_summary") or entry.get("symbol_index") or "")[:300]
                        if path:
                            codebase_lines.append(f"  {path} [{lang}] {summary}")
                except Exception:
                    pass

            if not codebase_lines:
                try:
                    from robber.ssot_context import get_context_window
                    from robber.notes import load_notes

                    notes = load_notes(self.root)
                    if notes:
                        codebase_lines.append(f"  Workspace notes: {notes[:500]}")
                except Exception:
                    pass

            return "\n".join(codebase_lines) if codebase_lines else "(no SSOT context available)"
        except Exception as exc:
            logger.debug("[PROXY] SSOT chunk retrieval failed: %s", exc)
            return "(SSOT context unavailable)"

    def _build_ssot_system_prompt(
        self, ssot_chunks: str, worker_role: str
    ) -> dict[str, str]:
        """Construct the mandatory system prompt with SSOT context header."""
        persona_label = {
            "architect": "Architect (reasoning specialist)",
            "coder": "Coder (code generation specialist)",
            "auditor": "Auditor (code review specialist)",
            "tester": "Tester (testing specialist)",
            "vision_auditor": "Vision Auditor (visual inspection specialist)",
            "pr_writer": "PR Writer (pull request specialist)",
        }.get(worker_role, f"Agent ({worker_role})")

        system_content = (
            "[CRITICAL SYSTEM DIRECTIVE]\n"
            "You are Agent Robber (ایجنٹ روبر), an autonomous software engineering agent.\n"
            f"Your current role: {persona_label}.\n"
            "You MUST operate strictly using the provided SSOT workspace context below.\n"
            "NEVER ask generic introductory questions if the context or files are available in SSOT.\n"
            "NEVER respond with 'what kind of app are you looking to build' or similar when context is provided.\n"
            "ALWAYS reference specific file paths, symbols, and code structures from the SSOT context.\n"
            "If you need clarification, ask about SPECIFIC implementation details, not general intent.\n"
            "\n"
            "[ACTIVE SSOT CONTEXT CHUNKS]:\n"
            f"{ssot_chunks}\n"
            "\n"
            "Analyze the workspace context above and respond with specific, actionable guidance.\n"
            "Reference actual file paths and code structures from the SSOT context."
        )
        return {"role": "system", "content": system_content}

    def _get_session_history(self, session_id: str) -> list[dict[str, str]]:
        """Retrieve conversation history for the given session."""
        try:
            from robber.memory.session_history import get_session_history

            return get_session_history().get_messages(session_id)
        except Exception:
            return []

    def _record_turn(
        self,
        session_id: str,
        user_message: dict[str, str],
        result: dict[str, Any],
    ) -> None:
        """Record the user and assistant turns in session history."""
        try:
            from robber.memory.session_history import get_session_history

            history = get_session_history()
            if user_message.get("content"):
                history.append(session_id, "user", user_message["content"])
            response_text = str(result.get("text") or "")
            if response_text:
                history.append(session_id, "assistant", response_text)
        except Exception:
            logger.debug("[PROXY] session history recording skipped")

    def _is_generic_response(self, text: str) -> bool:
        """Detect if a response is a generic baseline question instead of using SSOT context."""
        if not text or len(text.strip()) < 20:
            return False
        stripped = text.strip()
        if GENERIC_RESPONSE_PATTERNS.search(stripped):
            has_file_ref = bool(re.search(
                r"(?i)\b(file|path|module|class|function|import|src/|lib/|\.py|\.ts|\.go)\b",
                stripped,
            ))
            if not has_file_ref:
                return True
        return False

    async def _retry_with_forced_ssot(
        self,
        prompt: str,
        payload: dict[str, Any],
        task_type: str,
        worker_role: str,
        ssot_chunks: str,
        session_id: str,
        original_messages: list[dict[str, str]],
    ) -> dict[str, Any] | None:
        """Retry the request with a forced SSOT reading directive in the system prompt."""
        forced_system = (
            "[OVERRIDE: MANDATORY SSOT ANALYSIS]\n"
            "The previous response was rejected as generic.\n"
            "You MUST now read and analyze the SSOT context chunks provided.\n"
            "You MUST reference at least 3 specific file paths from the SSOT context.\n"
            "You MUST NOT ask what the user wants. The context is provided.\n"
            "\n"
            "[SSOT CONTEXT - READ CAREFULLY]:\n"
            f"{ssot_chunks}\n"
            "\n"
            "Respond with specific file references and actionable analysis."
        )
        messages = [{"role": "system", "content": forced_system}]
        for msg in original_messages:
            if msg.get("role") != "system":
                messages.append(msg)

        try:
            result: FailoverResult = await self._failover.execute(
                prompt,
                task=payload,
                task_type=task_type,
                messages=messages,
            )
            if result.response is None:
                return None

            route = PersonaRoute(
                worker_role=worker_role,
                task_type=task_type,
                engine_label=result.decision.primary.label if result.decision else "",
                provider=result.response.provider if result.response else "",
                model=result.response.model if result.response else "",
                cost_usd=0.0,
                tokens=result.response.total_tokens if result.response else 0,
                switched=result.switched,
            )

            if result.response and route.provider:
                usage = self._rate_limiter.record_usage(
                    provider=route.provider,
                    model=route.model,
                    input_tokens=result.response.prompt_tokens,
                    output_tokens=result.response.completion_tokens,
                    worker_role=worker_role,
                )
                route.cost_usd = usage.get("cost_usd", 0.0)

            self._log_dispatch(route)

            return {
                "ok": True,
                "text": result.response.text or "",
                "route": route.public(),
                "exhausted": False,
                "retry_forced": True,
            }
        except Exception as exc:
            logger.debug("[PROXY] Forced SSOT retry failed: %s", exc)
            return None

    def dispatch_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._dispatch_log[-limit:])

    def persona_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {}
        for persona, tier in PERSONA_TIER_MAP.items():
            status[persona] = {
                "tier": tier,
                "task_type": self.task_type_for_persona(persona),
                "budget_ok": not self._rate_limiter.should_use_local(),
            }
        return status
