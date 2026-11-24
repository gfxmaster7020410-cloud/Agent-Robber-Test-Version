"""Plugin SDK: validated schemas and the BaseAgentPlugin contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from robber.config import PLUGIN_MAX_OUTPUT
from robber.state_manager import StateManager


class PluginError(ValueError):
    """Raised when a plugin file, schema, or payload is unsafe or invalid."""


class ToolParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(default="string", max_length=32)
    description: str = Field(default="", max_length=400)
    required: bool = False
    default: Any = None

    @field_validator("type")
    @classmethod
    def _type_ok(cls, value: str) -> str:
        kind = (value or "string").strip().lower()
        if kind not in {"string", "integer", "number", "boolean", "object", "array"}:
            raise ValueError("unsupported parameter type")
        return kind


class ToolSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    parameters: dict[str, ToolParameter] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, value: str) -> str:
        text = (value or "").strip().lower()
        if not text.replace("_", "").isalnum() or not text[0].isalpha():
            raise ValueError("plugin name must be a python identifier")
        return text


class PluginResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    output: str = Field(default="", max_length=PLUGIN_MAX_OUTPUT)
    data: dict[str, Any] = Field(default_factory=dict)
    exit_code: int = 0

    @field_validator("output")
    @classmethod
    def _clip_output(cls, value: str) -> str:
        text = value or ""
        if len(text) > PLUGIN_MAX_OUTPUT:
            return text[:PLUGIN_MAX_OUTPUT]
        return text


@dataclass
class PluginRuntime:
    """SSOT-safe context. Plugins never receive a raw sqlite connection."""

    root: Path
    manager: StateManager
    plugin_name: str
    task_id: str | None = None

    def record_artifact(
        self,
        body: str = "",
        *,
        name: str | None = None,
        exit_code: int = 0,
        kind: str = "plugin",
    ) -> dict[str, Any]:
        clipped = (body or "")[:PLUGIN_MAX_OUTPUT]
        return self.manager.add_artifact(
            name or self.plugin_name,
            body=clipped,
            kind=kind or "plugin",
            task_id=self.task_id,
            exit_code=int(exit_code),
        )

    def queue_task(self, title: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = dict(payload or {})
        data["plugin"] = self.plugin_name
        data["source"] = "plugin"
        return self.manager.put_task(
            (title or self.plugin_name)[:120],
            status="queued",
            payload=data,
        )

    def list_tasks(self, *, limit: int = 16) -> list[dict[str, Any]]:
        return self.manager.list_tasks(limit=max(1, min(int(limit), 64)))


class BaseAgentPlugin(ABC):
    """Third-party tool contract. Subclass and drop the file in .robber/plugins/."""

    name: str = ""
    version: str = "1.0"
    categories: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    def initialize(self, context: PluginRuntime) -> None:
        return None

    @abstractmethod
    def get_tool_schema(self) -> ToolSchema: ...

    @abstractmethod
    def execute(self, args: dict[str, Any], context: PluginRuntime) -> PluginResult: ...

    def teardown(self) -> None:
        return None

    def matches(self, query: str, category: str = "") -> bool:
        import re

        text = (query or "").lower()
        pretty = (self.name or "").replace("_", " ")
        if pretty and pretty in text:
            return True
        for word in self.keywords:
            token = (word or "").strip().lower()
            if token and re.search(r"\b" + re.escape(token) + r"\b", text):
                return True
        return False
