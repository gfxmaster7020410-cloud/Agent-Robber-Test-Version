"""Multimodal page inspector for vault screenshots. Classification only — no challenge solving."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from robber.jail import JailError, resolve_inside, workspace_root

logger = logging.getLogger(__name__)

PageState = Literal["CAPTCHA_CHECKPOINT", "LOGIN_BLOCKED", "UNKNOWN_POPUP", "NORMAL"]
RecommendedAction = Literal["pause_and_alert", "click_retry", "bypass_modal"]

PAGE_STATES = {"CAPTCHA_CHECKPOINT", "LOGIN_BLOCKED", "UNKNOWN_POPUP", "NORMAL"}
ACTIONS = {"pause_and_alert", "click_retry", "bypass_modal"}

VISION_PROMPT = """You are a UI state classifier for a local operator dashboard.
Inspect the screenshot. Classify the page. Do not solve CAPTCHAs, 2FA, or logins.
Return JSON only with keys:
page_state: CAPTCHA_CHECKPOINT | LOGIN_BLOCKED | UNKNOWN_POPUP | NORMAL
ocr_text_highlights: short visible strings (max 6)
recommended_action: pause_and_alert | click_retry | bypass_modal
confidence: number 0-1
Use pause_and_alert for CAPTCHA, 2FA, or login walls.
Use bypass_modal only for dismissible cookie/consent chrome — never for security checks.
"""


@dataclass
class VisionReport:
    page_state: PageState
    ocr_text_highlights: list[str]
    recommended_action: RecommendedAction
    confidence: float
    source: str = "model"
    image_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class VisionInspector:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or workspace_root()).resolve()

    def _jailed_image(self, image_path: str | Path) -> Path:
        raw = Path(image_path)
        if raw.is_absolute():
            target = raw.resolve()
            try:
                target.relative_to(self.root)
            except ValueError as exc:
                raise JailError(f"screenshot escapes workspace jail: {image_path}") from exc
        else:
            target = resolve_inside(self.root, str(raw))
        if not target.is_file():
            raise JailError(f"screenshot not found: {image_path}")
        if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise JailError("screenshot must be an image file")
        return target

    async def analyze_page(
        self,
        image_path: str | Path,
        *,
        sidecar: dict[str, Any] | None = None,
    ) -> VisionReport:
        try:
            target = self._jailed_image(image_path)
            meta = sidecar or _read_sidecar(target)
            report = await self._model_analyze(target, meta)
            if report is None:
                report = self._heuristic(target, meta)
            return self._safety_clamp(report, str(target.relative_to(self.root)))
        except Exception as exc:
            logger.info("vision inspector fell back (%s)", type(exc).__name__)
            return VisionReport(
                page_state="UNKNOWN_POPUP",
                ocr_text_highlights=[],
                recommended_action="pause_and_alert",
                confidence=0.2,
                source="fallback",
                image_path=str(image_path),
            )

    def _safety_clamp(self, report: VisionReport, rel: str) -> VisionReport:
        state = report.page_state if report.page_state in PAGE_STATES else "UNKNOWN_POPUP"
        action = report.recommended_action if report.recommended_action in ACTIONS else "pause_and_alert"
        if state in {"CAPTCHA_CHECKPOINT", "LOGIN_BLOCKED"}:
            action = "pause_and_alert"
        confidence = max(0.0, min(1.0, float(report.confidence)))
        highlights = [str(item)[:120] for item in report.ocr_text_highlights[:6]]
        return VisionReport(state, highlights, action, confidence, report.source, rel)

    async def _model_analyze(self, target: Path, meta: dict[str, Any]) -> VisionReport | None:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            return None
        try:
            from google import genai
            from google.genai import types

            mime = "image/jpeg" if target.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            data = await asyncio.to_thread(target.read_bytes)
            hint = json.dumps({"url": meta.get("url"), "error_type": meta.get("error_type")}, ensure_ascii=True)
            client = genai.Client(api_key=key)
            response = await client.aio.models.generate_content(
                model="gemini-3.6-flash",
                contents=[
                    types.Part.from_bytes(data=data, mime_type=mime),
                    types.Part.from_text(text=f"{VISION_PROMPT}\nContext: {hint}"),
                ],
                config=types.GenerateContentConfig(temperature=0.1),
            )
            text = (getattr(response, "text", None) or "").strip()
            parsed = _parse_json_object(text)
            if not parsed:
                return None
            return VisionReport(
                page_state=parsed.get("page_state", "UNKNOWN_POPUP"),  # type: ignore[arg-type]
                ocr_text_highlights=list(parsed.get("ocr_text_highlights") or []),
                recommended_action=parsed.get("recommended_action", "pause_and_alert"),  # type: ignore[arg-type]
                confidence=float(parsed.get("confidence") or 0.5),
                source="gemini",
            )
        except Exception as exc:
            logger.info("vision model skipped (%s)", type(exc).__name__)
            return None

    def _heuristic(self, target: Path, meta: dict[str, Any]) -> VisionReport:
        blob = " ".join(
            str(meta.get(key) or "")
            for key in ("error_type", "details", "url")
        ).lower()
        name = target.name.lower()
        if "captcha" in blob or "verification" in blob or "captcha" in name:
            return VisionReport("CAPTCHA_CHECKPOINT", ["Security verification required"], "pause_and_alert", 0.72, "heuristic")
        if "login" in blob or "2fa" in blob:
            return VisionReport("LOGIN_BLOCKED", ["Enter 2FA code"] if "2fa" in blob else ["Login required"], "pause_and_alert", 0.7, "heuristic")
        if "popup" in blob or "modal" in blob:
            return VisionReport("UNKNOWN_POPUP", [], "bypass_modal", 0.45, "heuristic")
        return VisionReport("NORMAL", [], "click_retry", 0.4, "heuristic")


def _read_sidecar(image: Path) -> dict[str, Any]:
    sidecar = image.with_suffix(".json")
    if not sidecar.is_file():
        return {}
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
