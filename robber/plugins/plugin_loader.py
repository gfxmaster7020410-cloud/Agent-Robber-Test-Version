"""Hot-reload manager for .robber/plugins Python and Rust tools."""

from __future__ import annotations

import concurrent.futures
import inspect
import json
import sys
import threading
from pathlib import Path
from typing import Any

from robber.config import PLUGIN_DIR_NAME, PLUGIN_EXECUTE_TIMEOUT, PLUGIN_MAX_ARGS_BYTES
from robber.jail import workspace_root
from robber.plugins.base_plugin import (
    BaseAgentPlugin,
    PluginError,
    PluginResult,
    PluginRuntime,
    ToolSchema,
)
from robber.plugins.rust_adapter import load_rust_plugin
from robber.plugins.safety import file_digest, validate_args, validate_plugin_file
from robber.state_manager import StateManager

_LOADERS: dict[str, PluginLoader] = {}
_LOCK = threading.Lock()


class LoadedPlugin:
    def __init__(
        self,
        plugin: BaseAgentPlugin,
        *,
        path: Path,
        digest: str,
        signed: bool,
        module_name: str = "",
    ) -> None:
        self.plugin = plugin
        self.path = path
        self.digest = digest
        self.signed = signed
        self.module_name = module_name
        self.enabled = True
        schema = plugin.get_tool_schema()
        self.schema = ToolSchema.model_validate(schema) if not isinstance(schema, ToolSchema) else schema
        plugin.name = self.schema.name

    @property
    def name(self) -> str:
        return self.schema.name

    def matches(self, query: str, category: str = "") -> bool:
        return self.plugin.matches(query, category)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.schema.name,
            "description": self.schema.description,
            "path": str(self.path),
            "digest": self.digest,
            "signed": self.signed,
            "enabled": self.enabled,
            "version": getattr(self.plugin, "version", ""),
            "kind": "native" if self.path.suffix.lower() in {".dll", ".so"} else "python",
        }


class PluginLoader:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.plugin_dir = self.root / ".robber" / PLUGIN_DIR_NAME
        self._items: dict[str, LoadedPlugin] = {}
        self._lock = threading.RLock()
        self._initialized: set[str] = set()
        self.errors: list[str] = []

    def scan(self) -> list[LoadedPlugin]:
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        disabled = set(self._disabled_names())
        found: dict[str, Path] = {}
        self.errors = []
        for path in sorted(self.plugin_dir.iterdir()) if self.plugin_dir.is_dir() else []:
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in {".py", ".so", ".dll"}:
                continue
            if path.name in {"manifest.json"}:
                continue
            found[path.stem] = path
        with self._lock:
            for name in list(self._items):
                if name not in found:
                    self._drop(name)
            for name, path in found.items():
                try:
                    digest = validate_plugin_file(self.root, path)
                    current = self._items.get(name)
                    if current is not None and current.digest == digest and current.path == path:
                        current.enabled = name not in disabled
                        continue
                    if current is not None:
                        self._drop(name)
                    loaded = self._load_file(path, digest)
                    loaded.enabled = name not in disabled
                    self._items[loaded.schema.name] = loaded
                except PluginError as exc:
                    self.errors.append(f"{path.name}: {exc}")
                    self._drop(name)
        return self.list()

    def reload(self) -> list[LoadedPlugin]:
        with self._lock:
            for name in list(self._items):
                self._drop(name)
            self._initialized.clear()
        return self.scan()

    def list(self) -> list[LoadedPlugin]:
        with self._lock:
            return [self._items[key] for key in sorted(self._items)]

    def enabled(self) -> list[BaseAgentPlugin]:
        return [item.plugin for item in self.list() if item.enabled]

    def get(self, name: str) -> BaseAgentPlugin | None:
        key = (name or "").strip().lower()
        with self._lock:
            item = self._items.get(key)
            if item is None or not item.enabled:
                return None
            return item.plugin

    def match_query(self, query: str) -> BaseAgentPlugin | None:
        text = query or ""
        for item in self.list():
            if item.enabled and item.matches(text):
                return item.plugin
        return None

    def set_enabled(self, name: str, enabled: bool) -> None:
        key = (name or "").strip().lower()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                raise PluginError(f"unknown plugin '{name}'")
            item.enabled = bool(enabled)
            names = set(self._disabled_names())
            if enabled:
                names.discard(key)
            else:
                names.add(key)
            self._write_disabled(names)

    def execute(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
        manager: StateManager | None = None,
    ) -> PluginResult:
        plugin = self.get(name)
        if plugin is None:
            raise PluginError(f"plugin '{name}' is not loaded")
        blob = json.dumps(args or {}, ensure_ascii=True)
        if len(blob.encode("utf-8")) > PLUGIN_MAX_ARGS_BYTES:
            raise PluginError("plugin args exceed size cap")
        with self._lock:
            loaded = self._items[plugin.name]
            schema = loaded.schema
        validated = validate_args(
            {key: spec.model_dump() for key, spec in schema.parameters.items()},
            args or {},
        )
        runtime = PluginRuntime(
            root=self.root,
            manager=manager or StateManager(self.root),
            plugin_name=plugin.name,
            task_id=task_id,
        )
        if plugin.name not in self._initialized:
            plugin.initialize(runtime)
            self._initialized.add(plugin.name)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(plugin.execute, validated, runtime)
            try:
                result = future.result(timeout=PLUGIN_EXECUTE_TIMEOUT)
            except concurrent.futures.TimeoutError as exc:
                raise PluginError("plugin execute timed out") from exc
        if isinstance(result, PluginResult):
            parsed = result
        else:
            parsed = PluginResult.model_validate(result)
        if parsed.ok:
            runtime.record_artifact(
                parsed.output or json.dumps(parsed.data, ensure_ascii=True)[:4000],
                exit_code=parsed.exit_code,
            )
        return parsed

    def catalog(self) -> list[dict[str, Any]]:
        return [item.public() for item in self.list()]

    def _load_file(self, path: Path, digest: str) -> LoadedPlugin:
        signed = bool(
            path.with_suffix(path.suffix + ".sha256").is_file()
            or (self.plugin_dir / "manifest.json").is_file()
        )
        if path.suffix.lower() in {".dll", ".so"}:
            plugin = load_rust_plugin(path)
            return LoadedPlugin(plugin, path=path, digest=digest, signed=signed)
        return self._load_python(path, digest, signed)

    def _load_python(self, path: Path, digest: str, signed: bool) -> LoadedPlugin:
        from types import ModuleType

        mod_name = f"robber_dynplugin_{path.stem}_{digest[:12]}"
        source = path.read_text(encoding="utf-8")
        module = ModuleType(mod_name)
        module.__file__ = str(path)
        sys.modules[mod_name] = module
        try:
            exec(compile(source, str(path), "exec"), module.__dict__)
        except PluginError:
            sys.modules.pop(mod_name, None)
            raise
        except Exception as exc:
            sys.modules.pop(mod_name, None)
            raise PluginError(f"plugin import failed: {type(exc).__name__}") from exc
        found: list[BaseAgentPlugin] = []
        for obj in vars(module).values():
            if inspect.isclass(obj) and issubclass(obj, BaseAgentPlugin) and obj is not BaseAgentPlugin:
                found.append(obj())
        if len(found) != 1:
            sys.modules.pop(mod_name, None)
            raise PluginError("plugin must export exactly one BaseAgentPlugin subclass")
        plugin = found[0]
        parsed = ToolSchema.model_validate(plugin.get_tool_schema())
        if parsed.name != path.stem:
            sys.modules.pop(mod_name, None)
            raise PluginError("plugin name must match filename")
        return LoadedPlugin(plugin, path=path, digest=digest, signed=signed, module_name=mod_name)

    def _drop(self, name: str) -> None:
        item = self._items.pop(name, None)
        if item is None:
            return
        try:
            item.plugin.teardown()
        except Exception:
            pass
        self._initialized.discard(name)
        if item.module_name:
            sys.modules.pop(item.module_name, None)

    def _disabled_names(self) -> list[str]:
        path = self.plugin_dir / "disabled.json"
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return [str(item).strip().lower() for item in data if str(item).strip()]
        return []

    def _write_disabled(self, names: set[str]) -> None:
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        dest = self.plugin_dir / "disabled.json"
        dest.write_text(json.dumps(sorted(names), indent=2), encoding="utf-8")


def get_loader(root: Path | str | None = None) -> PluginLoader:
    dest = Path(root).resolve() if root else workspace_root()
    key = str(dest)
    with _LOCK:
        loader = _LOADERS.get(key)
        if loader is None:
            loader = PluginLoader(dest)
            loader.scan()
            _LOADERS[key] = loader
        return loader


def reset_loaders(root: Path | str | None = None) -> None:
    with _LOCK:
        if root is None:
            for loader in _LOADERS.values():
                for name in list(loader._items):
                    loader._drop(name)
            _LOADERS.clear()
            return
        key = str(Path(root).resolve())
        loader = _LOADERS.pop(key, None)
        if loader is not None:
            for name in list(loader._items):
                loader._drop(name)
