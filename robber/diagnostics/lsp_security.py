"""Lightweight LSP client + OWASP-oriented AST security auditor."""



from __future__ import annotations



import ast

import json

import os

import queue

import re

import shutil

import subprocess

import threading

import time

from dataclasses import dataclass

from pathlib import Path

from typing import Any, Callable



from robber.config import LSP_CONFIG_PATH, LSP_DIAGNOSE_TIMEOUT, LSP_MEMORY_BOUND_MB

from robber.jail import workspace_root



Transport = Callable[[dict[str, Any]], dict[str, Any] | None]





_SQLI = re.compile(

    r"""(?:execute|executemany)\s*\(\s*(?:f["']|["'][^"']*%|[^)]*\.format\s*\()""",

    re.I,

)

_SQLI_PLUS = re.compile(r"""(?:execute|executemany)\s*\(\s*[^)]*(?:\+|%)""", re.I)

_SECRET = re.compile(

    r"""\b(api_key|apikey|password|secret|token|private_key)\s*=\s*['"][^'"]{8,}['"]""",

    re.I,

)

_UNSAFE = re.compile(r"""\b(?:eval|exec|pickle\.loads|yaml\.load)\s*\(|\bunsafe\s*\{""", re.I)

_INPUT = re.compile(r"""request\.(?:GET|POST|args|form|json)\b""", re.I)



_SUFFIX_LANGUAGE = {

    ".py": "python",

    ".pyi": "python",

    ".ts": "typescript",

    ".tsx": "typescriptreact",

    ".js": "javascript",

    ".jsx": "javascriptreact",

    ".go": "go",

    ".rs": "rust",

}



_TEXT_CAP = 400_000

_BODY_CAP = 2_000_000





@dataclass

class Diagnostic:

    path: str

    line: int

    message: str

    severity: str = "error"

    source: str = "lsp"



    def public(self) -> dict[str, Any]:

        return {

            "path": self.path,

            "line": self.line,

            "message": self.message,

            "severity": self.severity,

            "source": self.source,

        }





@dataclass

class SecurityFinding:

    rule: str

    owasp: str

    path: str

    line: int

    snippet: str



    def public(self) -> dict[str, Any]:

        return {

            "rule": self.rule,

            "owasp": self.owasp,

            "path": self.path,

            "line": self.line,

            "snippet": self.snippet[:160],

        }





def load_lsp_config(path: Path | str | None = None) -> dict[str, Any]:

    """Read robber/config/lsp_servers.json (or ROBBER_LSP_CONFIG)."""

    empty: dict[str, Any] = {"version": "1.0", "memory_bound_mb": LSP_MEMORY_BOUND_MB, "servers": {}}

    raw = path or os.environ.get("ROBBER_LSP_CONFIG") or LSP_CONFIG_PATH

    candidate = Path(raw)

    if not candidate.is_file():

        return empty

    try:

        data = json.loads(candidate.read_text(encoding="utf-8"))

    except (OSError, json.JSONDecodeError, UnicodeError):

        return empty

    if not isinstance(data, dict):

        return empty

    servers = data.get("servers")

    if not isinstance(servers, dict):

        data["servers"] = {}

    return data





def server_spec_for(path: Path, config: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]] | None:

    cfg = config if isinstance(config, dict) else load_lsp_config()

    language = _SUFFIX_LANGUAGE.get(path.suffix.lower())

    if not language:

        return None

    servers = cfg.get("servers") if isinstance(cfg.get("servers"), dict) else {}

    for name, spec in servers.items():

        if not isinstance(spec, dict) or not spec.get("enabled"):

            continue

        langs = spec.get("languages") if isinstance(spec.get("languages"), list) else []

        langs_l = {str(item).lower() for item in langs}

        if language.lower() in langs_l or str(name).lower() == language.lower():

            return str(name), spec

    return None





def boot_lsp_config(path: Path | str | None = None) -> dict[str, Any]:

    """Load LSP JSON on daemon startup. Does not spawn processes."""

    return load_lsp_config(path)





class LspClient:

    """JSON-RPC LSP client over STDIO. Pytest never spawns unless allow_spawn=True."""



    SERVERS = {

        ".py": ("pyright-langserver", ["--stdio"]),

        ".go": ("gopls", []),

        ".rs": ("rust-analyzer", []),

        ".ts": ("typescript-language-server", ["--stdio"]),

        ".tsx": ("typescript-language-server", ["--stdio"]),

        ".js": ("typescript-language-server", ["--stdio"]),

        ".jsx": ("typescript-language-server", ["--stdio"]),

    }



    def __init__(

        self,

        *,

        transport: Transport | None = None,

        allow_spawn: bool | None = None,

        config: dict[str, Any] | None = None,

        config_path: Path | str | None = None,

    ) -> None:

        self.transport = transport

        self.config = config if isinstance(config, dict) else load_lsp_config(config_path)

        self.allow_spawn = bool(allow_spawn) if allow_spawn is not None else _default_allow_spawn(self.config)

        self._session: _StdioSession | None = None

        self._session_key = ""



    def diagnose(self, path: str | Path, text: str) -> list[Diagnostic]:

        rel = str(path)

        if self.transport is not None:

            payload = self.transport({"method": "textDocument/diagnostic", "path": rel, "text": text})

            return _from_lsp(rel, payload)

        if self.allow_spawn:

            spawned = self._stdio_diagnose(Path(path), text)

            if spawned is not None:

                return spawned

        return diagnose_python(rel, text) if Path(rel).suffix.lower() == ".py" or _looks_py(text) else []



    def close(self) -> None:

        session = self._session

        self._session = None

        self._session_key = ""

        if session is not None:

            session.close()



    def _stdio_diagnose(self, path: Path, text: str) -> list[Diagnostic] | None:

        spec = server_spec_for(path, self.config)

        command: list[str] | None = None

        language_id = _SUFFIX_LANGUAGE.get(path.suffix.lower()) or "plaintext"

        env: dict[str, str] | None = None

        key = ""

        if spec is not None:

            key, row = spec

            command = _resolve_command(row.get("command"))

            env = _server_env(self.config, row)

            langs = row.get("languages") if isinstance(row.get("languages"), list) else []

            if langs:

                language_id = str(langs[0])

                mapped = _SUFFIX_LANGUAGE.get(path.suffix.lower())

                if mapped:

                    language_id = mapped

        if command is None:

            fallback = self.SERVERS.get(path.suffix.lower())

            if fallback is None:

                return None

            binary, extra = fallback

            found = shutil.which(binary)

            if not found:

                return None

            command = [found, *extra]

            key = binary

            env = _server_env(self.config, {"env": {"NODE_OPTIONS": f"--max-old-space-size={LSP_MEMORY_BOUND_MB}"}})

        try:

            session = self._session_for(key, command, env or os.environ.copy())

            items = session.diagnose_buffer(path, text, language_id)

        except (OSError, subprocess.SubprocessError, RuntimeError):

            self.close()

            return None

        return _from_lsp(str(path), {"diagnostics": items})



    def _session_for(self, key: str, command: list[str], env: dict[str, str]) -> "_StdioSession":

        if self._session is not None and self._session_key == key and self._session.alive:

            return self._session

        self.close()

        self._session = _StdioSession.start(command, env, timeout=LSP_DIAGNOSE_TIMEOUT)

        self._session_key = key

        return self._session





class _StdioSession:

    def __init__(self, proc: subprocess.Popen[bytes], timeout: float) -> None:

        self.proc = proc

        self.timeout = timeout

        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue()

        self._id = 0

        self._initialized = False

        self._alive = True

        self._thread = threading.Thread(target=self._read_loop, name="robber-lsp-stdio", daemon=True)

        self._thread.start()



    @classmethod

    def start(cls, command: list[str], env: dict[str, str], timeout: float) -> "_StdioSession":

        kwargs: dict[str, Any] = {

            "stdin": subprocess.PIPE,

            "stdout": subprocess.PIPE,

            "stderr": subprocess.DEVNULL,

            "env": env,

            "bufsize": 0,

        }

        if os.name == "nt":

            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        proc = subprocess.Popen(_spawn_argv(command), **kwargs)

        if proc.stdin is None or proc.stdout is None:

            proc.kill()

            raise RuntimeError("lsp stdio pipes missing")

        return cls(proc, timeout)



    @property

    def alive(self) -> bool:

        return self._alive and self.proc.poll() is None



    def diagnose_buffer(self, path: Path, text: str, language_id: str) -> list[dict[str, Any]]:

        if not self._initialized:

            root = path.parent.resolve() if path.parent.as_posix() not in {".", ""} else workspace_root()

            try:

                root_uri = root.as_uri()

            except ValueError:

                root_uri = Path(".").resolve().as_uri()

            self._request(

                "initialize",

                {

                    "processId": os.getpid(),

                    "rootUri": root_uri,

                    "capabilities": {

                        "textDocument": {

                            "publishDiagnostics": {"relatedInformation": False},

                            "synchronization": {"didSave": False},

                        }

                    },

                    "clientInfo": {"name": "agent-robber", "version": "1.0"},

                },

            )

            self._notify("initialized", {})

            self._initialized = True

        try:

            uri = path.resolve().as_uri()

        except ValueError:

            uri = path.as_posix()

        self._notify(

            "textDocument/didOpen",

            {

                "textDocument": {

                    "uri": uri,

                    "languageId": language_id,

                    "version": 1,

                    "text": (text or "")[:_TEXT_CAP],

                }

            },

        )

        items = self._wait_diagnostics(uri)

        if items is None:

            pulled = self._request(

                "textDocument/diagnostic",

                {"textDocument": {"uri": uri}},

                optional=True,

            )

            items = _pull_items(pulled)

        self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})

        return items or []



    def close(self) -> None:

        self._alive = False

        try:

            if self.proc.poll() is None:

                self._request("shutdown", None, optional=True)

                self._notify("exit", None)

        except Exception:

            pass

        try:

            if self.proc.stdin:

                self.proc.stdin.close()

        except Exception:

            pass

        try:

            self.proc.terminate()

            self.proc.wait(timeout=1.0)

        except Exception:

            try:

                self.proc.kill()

            except Exception:

                pass



    def _read_loop(self) -> None:

        stream = self.proc.stdout

        if stream is None:

            self._q.put(None)

            return

        try:

            while self._alive and self.proc.poll() is None:

                msg = _read_lsp_message(stream)

                if msg is None:

                    break

                self._q.put(msg)

        except Exception:

            pass

        finally:

            self._q.put(None)



    def _next_id(self) -> int:

        self._id += 1

        return self._id



    def _send(self, payload: dict[str, Any]) -> None:

        if self.proc.stdin is None:

            raise RuntimeError("lsp stdin closed")

        raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")

        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")

        self.proc.stdin.write(header + raw)

        self.proc.stdin.flush()



    def _notify(self, method: str, params: Any) -> None:

        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}

        if params is not None:

            msg["params"] = params

        self._send(msg)



    def _request(self, method: str, params: Any, optional: bool = False) -> dict[str, Any] | None:

        mid = self._next_id()

        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": mid, "method": method}

        if params is not None:

            payload["params"] = params

        try:

            self._send(payload)

        except Exception:

            if optional:

                return None

            raise

        deadline = time.time() + self.timeout

        leftover: list[dict[str, Any]] = []

        matched: dict[str, Any] | None = None

        while time.time() < deadline:

            try:

                msg = self._q.get(timeout=0.05)

            except queue.Empty:

                if self.proc.poll() is not None:

                    break

                continue

            if msg is None:

                break

            if msg.get("id") == mid:

                matched = msg

                break

            leftover.append(msg)

        for item in leftover:

            self._q.put(item)

        if matched is None and not optional:

            raise RuntimeError(f"lsp {method} timed out")

        return matched



    def _wait_diagnostics(self, uri: str) -> list[dict[str, Any]] | None:

        deadline = time.time() + min(2.5, self.timeout)

        found: list[dict[str, Any]] | None = None

        leftover: list[dict[str, Any]] = []

        while time.time() < deadline:

            try:

                msg = self._q.get(timeout=0.08)

            except queue.Empty:

                if found is not None:

                    break

                if self.proc.poll() is not None:

                    break

                continue

            if msg is None:

                break

            if msg.get("method") == "textDocument/publishDiagnostics":

                params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

                msg_uri = str(params.get("uri") or "")

                if not msg_uri or msg_uri == uri or Path(uri).name in msg_uri:

                    found = list(params.get("diagnostics") or [])

                    continue

            leftover.append(msg)

        for item in leftover:

            self._q.put(item)

        return found





def diagnose_python(path: str, text: str) -> list[Diagnostic]:

    out: list[Diagnostic] = []

    try:

        tree = ast.parse(text or "")

    except SyntaxError as exc:

        out.append(

            Diagnostic(path, int(exc.lineno or 1), str(exc.msg or exc), "error", "py-ast")

        )

        return out

    assigned: set[str] = set()

    imported: dict[str, int] = {}

    used: set[str] = set()

    for node in tree.body:

        if isinstance(node, ast.Import):

            for alias in node.names:

                imported[alias.asname or alias.name.split(".", 1)[0]] = int(node.lineno or 1)

        elif isinstance(node, ast.ImportFrom):

            for alias in node.names:

                imported[alias.asname or alias.name] = int(node.lineno or 1)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)):

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):

                assigned.add(node.name)

            elif isinstance(node, ast.Assign):

                for target in node.targets:

                    if isinstance(target, ast.Name):

                        assigned.add(target.id)

            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):

                assigned.add(node.target.id)

    for node in ast.walk(tree):

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):

            used.add(node.id)

        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):

            used.add(node.value.id)

    builtins = set(dir(__builtins__)) if isinstance(__builtins__, dict) else set(dir(__builtins__))

    for name, line in imported.items():

        if name not in used and name not in {"annotations"}:

            out.append(Diagnostic(path, line, f"unused import {name}", "warning", "py-ast"))

    for node in ast.walk(tree):

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):

            if (

                node.id not in assigned

                and node.id not in imported

                and node.id not in builtins

                and node.id not in {"True", "False", "None"}

            ):

                if node.id[:1].islower() or node.id.isupper():

                    continue

    return out





class AstSecurityAuditor:

    """Scan buffers for OWASP Top 10 patterns. Prefers native Rust when present."""



    def scan_text(self, source: str, path: str = "") -> list[SecurityFinding]:

        try:

            from robber.indexer_bridge import security_audit as native



            packed = native(source, path)

            if isinstance(packed, list):

                return [_finding_from_row(path, item) for item in packed if isinstance(item, dict)]

            if isinstance(packed, dict) and isinstance(packed.get("findings"), list):

                return [_finding_from_row(path, item) for item in packed["findings"] if isinstance(item, dict)]

        except FileNotFoundError:

            pass

        except Exception:

            pass

        return python_security_scan(source, path)



    def scan_file(self, path: Path) -> list[SecurityFinding]:

        try:

            text = path.read_text(encoding="utf-8")

        except OSError:

            return []

        return self.scan_text(text, path.as_posix())



    def scan_paths(self, root: Path, rels: list[str]) -> list[SecurityFinding]:

        out: list[SecurityFinding] = []

        for rel in rels[:24]:

            candidate = root / rel

            if candidate.is_file():

                out.extend(self.scan_file(candidate))

        return out





def python_security_scan(source: str, path: str = "") -> list[SecurityFinding]:

    findings: list[SecurityFinding] = []

    for index, line in enumerate((source or "").splitlines(), start=1):

        stripped = line.strip()

        if stripped.startswith("#"):

            continue

        if _SQLI.search(line) or _SQLI_PLUS.search(line):

            findings.append(SecurityFinding("sqli", "A03:Injection", path, index, stripped))

        if _SECRET.search(line) and "os.environ" not in line:

            findings.append(SecurityFinding("secret", "A07:Identification", path, index, stripped))

        if _UNSAFE.search(line):

            findings.append(SecurityFinding("unsafe", "A08:Integrity", path, index, stripped))

        if _INPUT.search(line) and "int(" not in line and "escape" not in line.lower():

            findings.append(SecurityFinding("input", "A04:Insecure Design", path, index, stripped))

    return findings





def _default_allow_spawn(config: dict[str, Any]) -> bool:

    if os.environ.get("PYTEST_CURRENT_TEST"):

        return False

    flag = (os.environ.get("ROBBER_LSP") or "").strip().lower()

    if flag in {"0", "false", "no", "off"}:

        return False

    if flag in {"1", "true", "yes", "on"}:

        return True

    servers = config.get("servers") if isinstance(config.get("servers"), dict) else {}

    return any(isinstance(row, dict) and row.get("enabled") and row.get("command") for row in servers.values())





def _spawn_argv(command: list[str]) -> list[str]:

    if not command:

        return command

    first = Path(command[0])

    if first.suffix.lower() == ".cmd":

        cli = first.parent / "node_modules" / first.stem / "lib" / "cli.mjs"

        node = shutil.which("node")

        if node and cli.is_file():

            return [node, str(cli), *command[1:]]

        if os.name == "nt":

            return ["cmd.exe", "/c", *command]

    return command





def _resolve_command(command: Any) -> list[str] | None:

    if not isinstance(command, list) or not command:

        return None

    parts = [str(item) for item in command if str(item).strip()]

    if not parts:

        return None

    binary = parts[0]

    if os.path.isfile(binary):

        return parts

    found = shutil.which(binary)

    if found:

        return [found, *parts[1:]]

    return None





def _server_env(config: dict[str, Any], spec: dict[str, Any]) -> dict[str, str]:

    env = os.environ.copy()

    bound = int(config.get("memory_bound_mb") or LSP_MEMORY_BOUND_MB)

    extra = spec.get("env") if isinstance(spec.get("env"), dict) else {}

    env.update({str(key): str(value) for key, value in extra.items()})

    if "NODE_OPTIONS" not in extra:

        env["NODE_OPTIONS"] = f"--max-old-space-size={max(64, bound)}"

    return env





def _read_lsp_message(stream: Any) -> dict[str, Any] | None:

    header = b""

    while b"\r\n\r\n" not in header and b"\n\n" not in header:

        chunk = stream.read(1)

        if not chunk:

            return None

        header += chunk

        if len(header) > 8192:

            return None

    sep = b"\r\n\r\n" if b"\r\n\r\n" in header else b"\n\n"

    head, rest = header.split(sep, 1)

    length = 0

    for line in head.replace(b"\r\n", b"\n").split(b"\n"):

        if line.lower().startswith(b"content-length:"):

            try:

                length = int(line.split(b":", 1)[1].strip())

            except ValueError:

                return None

    if length <= 0 or length > _BODY_CAP:

        return None

    body = rest

    while len(body) < length:

        chunk = stream.read(length - len(body))

        if not chunk:

            return None

        body += chunk

    try:

        data = json.loads(body[:length].decode("utf-8"))

    except (json.JSONDecodeError, UnicodeError):

        return None

    return data if isinstance(data, dict) else None





def _pull_items(response: dict[str, Any] | None) -> list[dict[str, Any]]:

    if not isinstance(response, dict):

        return []

    result = response.get("result")

    if isinstance(result, dict):

        items = result.get("items") or result.get("diagnostics") or []

        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    return []





def _looks_py(text: str) -> bool:

    return "def " in (text or "") or "class " in (text or "") or "import " in (text or "")





def _from_lsp(path: str, payload: dict[str, Any] | None) -> list[Diagnostic]:

    if not isinstance(payload, dict):

        return []

    items = payload.get("items") or payload.get("diagnostics") or []

    out: list[Diagnostic] = []

    if not isinstance(items, list):

        return out

    for item in items:

        if not isinstance(item, dict):

            continue

        rng = item.get("range") if isinstance(item.get("range"), dict) else {}

        start = rng.get("start") if isinstance(rng.get("start"), dict) else {}

        line = int(start.get("line") or 0) + 1

        sev = item.get("severity")

        level = "error" if sev in {1, "error", None} else "warning"

        out.append(Diagnostic(path, line, str(item.get("message") or "diagnostic"), level, "lsp"))

    return out





def _finding_from_row(path: str, row: dict[str, Any]) -> SecurityFinding:

    return SecurityFinding(

        rule=str(row.get("rule") or "issue"),

        owasp=str(row.get("owasp") or ""),

        path=str(row.get("path") or path),

        line=int(row.get("line") or 1),

        snippet=str(row.get("snippet") or ""),

    )





def scan_workspace(root: Path | str | None, rels: list[str] | None = None) -> dict[str, Any]:

    base = Path(root).resolve() if root else workspace_root()

    auditor = AstSecurityAuditor()

    client = LspClient()

    findings: list[SecurityFinding] = []

    diags: list[Diagnostic] = []

    targets = list(rels or [])

    if not targets:

        for path in list(base.glob("*.py"))[:12]:

            targets.append(path.name)

    try:

        for rel in targets[:16]:

            path = base / rel

            if not path.is_file():

                continue

            try:

                text = path.read_text(encoding="utf-8")

            except OSError:

                continue

            findings.extend(auditor.scan_text(text, rel.replace("\\", "/")))

            diags.extend(client.diagnose(rel, text))

    finally:

        client.close()

    return {

        "findings": [item.public() for item in findings],

        "diagnostics": [item.public() for item in diags],

        "errors": sum(1 for item in diags if item.severity == "error")

        + sum(1 for item in findings if item.rule in {"sqli", "secret", "unsafe"}),

    }

