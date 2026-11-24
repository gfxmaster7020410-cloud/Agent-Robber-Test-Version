"""Load a compiled plugin cdylib that exports the Robber C ABI."""

from __future__ import annotations

import json
from ctypes import CDLL, c_char_p, c_int32, c_uint32, create_string_buffer
from pathlib import Path
from typing import Any

from robber.config import PLUGIN_MAX_OUTPUT
from robber.plugins.base_plugin import (
    BaseAgentPlugin,
    PluginError,
    PluginResult,
    PluginRuntime,
    ToolSchema,
)


class RustAgentPlugin(BaseAgentPlugin):
    """Wraps robber_plugin_schema / execute / teardown from a .dll or .so."""

    def __init__(self, path: Path, dll: CDLL, schema: ToolSchema) -> None:
        self._path = path
        self._dll = dll
        self._schema = schema
        self.name = schema.name
        self.version = "native"
        self.categories = ()
        self.keywords = (schema.name,)

    def get_tool_schema(self) -> ToolSchema:
        return self._schema

    def execute(self, args: dict[str, Any], context: PluginRuntime) -> PluginResult:
        if not hasattr(self._dll, "robber_plugin_execute"):
            raise PluginError("rust plugin is missing robber_plugin_execute")
        buf = create_string_buffer(PLUGIN_MAX_OUTPUT)
        payload = json.dumps(args, ensure_ascii=True).encode("utf-8")
        ctx = json.dumps(
            {"root": str(context.root), "task_id": context.task_id or "", "plugin": self.name},
            ensure_ascii=True,
        ).encode("utf-8")
        rc = int(
            self._dll.robber_plugin_execute(
                payload,
                ctx,
                buf,
                c_uint32(len(buf)),
            )
        )
        if rc < 0:
            raise PluginError("rust plugin execute failed")
        raw = buf.value.decode("utf-8", "replace") or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PluginError("rust plugin returned invalid json") from exc
        if not isinstance(data, dict):
            raise PluginError("rust plugin returned a non-object")
        return PluginResult.model_validate(
            {
                "ok": bool(data.get("ok", True)),
                "output": str(data.get("output") or "")[:PLUGIN_MAX_OUTPUT],
                "data": data.get("data") if isinstance(data.get("data"), dict) else {},
                "exit_code": int(data.get("exit_code") or 0),
            }
        )

    def teardown(self) -> None:
        if hasattr(self._dll, "robber_plugin_teardown"):
            try:
                self._dll.robber_plugin_teardown()
            except Exception:
                pass


def load_rust_plugin(path: Path) -> RustAgentPlugin:
    try:
        dll = CDLL(str(path))
    except OSError as exc:
        raise PluginError(f"could not load rust plugin: {exc}") from exc
    if not hasattr(dll, "robber_plugin_schema"):
        raise PluginError("rust plugin is missing robber_plugin_schema")
    dll.robber_plugin_schema.restype = c_int32
    dll.robber_plugin_schema.argtypes = [c_char_p, c_uint32]
    if hasattr(dll, "robber_plugin_execute"):
        dll.robber_plugin_execute.restype = c_int32
        dll.robber_plugin_execute.argtypes = [c_char_p, c_char_p, c_char_p, c_uint32]
    if hasattr(dll, "robber_plugin_teardown"):
        dll.robber_plugin_teardown.restype = c_int32
    buf = create_string_buffer(4096)
    rc = int(dll.robber_plugin_schema(buf, c_uint32(len(buf))))
    if rc < 0:
        raise PluginError("rust plugin schema call failed")
    try:
        schema = ToolSchema.model_validate_json(buf.value.decode("utf-8") or "{}")
    except Exception as exc:
        raise PluginError("rust plugin schema is invalid") from exc
    return RustAgentPlugin(path, dll, schema)
