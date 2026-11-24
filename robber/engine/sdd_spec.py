"""Spec-driven design: OpenAPI, DDL, and DIP contracts before code lands."""



from __future__ import annotations



import json

import re

import time

import uuid

from dataclasses import dataclass, field

from pathlib import Path

from typing import Any



from robber.jail import workspace_root

from robber.state_manager import StateManager



SPEC_META_KEY = "sdd_spec"

_REST = re.compile(r"\b(rest|api|endpoint|openapi|session|auth|login|user)\b", re.I)

_SQL = re.compile(r"\b(sqlite|postgres|sql|storage|database|persist)\b", re.I)





@dataclass

class SoftwareSpec:

    spec_id: str

    goal: str

    openapi: dict[str, Any] = field(default_factory=dict)

    ddl: str = ""

    postgres_ddl: str = ""

    contracts: list[dict[str, str]] = field(default_factory=list)

    dip: list[dict[str, str]] = field(default_factory=list)

    created_at: float = 0.0



    def public(self) -> dict[str, Any]:

        return {

            "id": self.spec_id,

            "goal": self.goal,

            "openapi": self.openapi,

            "ddl": self.ddl,

            "postgres_ddl": self.postgres_ddl,

            "contracts": list(self.contracts),

            "dip": list(self.dip),

            "created_at": self.created_at,

        }



    def worker_brief(self) -> str:

        paths = list((self.openapi.get("paths") or {}).keys())[:8]

        ports = [str(item.get("port") or "") for item in self.dip if item.get("port")]

        return (

            f"spec {self.spec_id} routes={','.join(paths) or '-'} "

            f"ports={','.join(ports) or '-'} ddl=sqlite"

        )





class SpecDrivenEngine:

    """Turn a high-level requirement into SSOT-backed architecture contracts."""



    def __init__(

        self,

        root: Path | str | None = None,

        *,

        manager: StateManager | None = None,

    ) -> None:

        self.root = Path(root).resolve() if root else workspace_root()

        self.manager = manager



    def plan(self, goal: str) -> SoftwareSpec:

        spec = build_spec(goal)

        self.store(spec)

        return spec



    def store(self, spec: SoftwareSpec) -> SoftwareSpec:

        manager = self.manager

        if manager is not None:

            manager.merge_metadata({SPEC_META_KEY: spec.public()})

            try:

                manager.add_memory(

                    json.dumps(

                        {

                            "id": spec.spec_id,

                            "goal": spec.goal[:400],

                            "routes": list((spec.openapi.get("paths") or {}).keys())[:12],

                        },

                        ensure_ascii=True,

                    )[:2000],

                    kind="spec",

                    source="SpecDrivenEngine",

                    token_est=80,

                )

                manager.add_artifact(

                    f"sdd:{spec.spec_id}",

                    body=json.dumps(spec.public(), ensure_ascii=True)[:8000],

                    kind="spec",

                    exit_code=0,

                )

                manager.record_decision(

                    "sdd spec",

                    rationale=spec.worker_brief()[:800],

                    branch="sdd",

                    payload=spec.spec_id,

                )

            except Exception:

                pass

        self._write_sidecar(spec)

        return spec



    def load(self) -> SoftwareSpec | None:

        manager = self.manager

        if manager is None:

            return None

        row = manager.db.get_project(manager.project_id)

        if row is None:

            return None

        meta = _as_dict(row.get("metadata_json"))

        blob = meta.get(SPEC_META_KEY)

        if not isinstance(blob, dict):

            return None

        return spec_from_public(blob)



    def _write_sidecar(self, spec: SoftwareSpec) -> None:

        folder = self.root / ".robber" / "specs"

        try:

            folder.mkdir(parents=True, exist_ok=True)

            (folder / "openapi.json").write_text(

                json.dumps(spec.openapi, indent=2, ensure_ascii=True)[:32_000],

                encoding="utf-8",

            )

            (folder / "schema.sql").write_text((spec.ddl + "\n" + spec.postgres_ddl).strip() + "\n", encoding="utf-8")

            (folder / "contracts.json").write_text(

                json.dumps({"contracts": spec.contracts, "dip": spec.dip}, indent=2, ensure_ascii=True)[:16_000],

                encoding="utf-8",

            )

        except OSError:

            return





def build_spec(goal: str) -> SoftwareSpec:

    text = (goal or "").strip() or "workspace service"

    session = bool(_REST.search(text))

    _ = bool(_SQL.search(text)) or session

    spec_id = uuid.uuid4().hex[:12]

    if session:

        openapi = _session_openapi()

        ddl, postgres = _session_ddl()

        contracts = [

            {"name": "SessionStore", "kind": "interface", "role": "port"},

            {"name": "SqliteSessionStore", "kind": "class", "role": "adapter"},

            {"name": "SessionService", "kind": "class", "role": "application"},

            {"name": "AuthRouter", "kind": "class", "role": "interface"},

        ]

        dip = [

            {"port": "SessionStore", "adapter": "SqliteSessionStore", "injects": "SessionService"},

            {"port": "PasswordHasher", "adapter": "Pbkdf2Hasher", "injects": "SessionService"},

        ]

    else:

        slug = _slug(text)

        openapi = {

            "openapi": "3.0.3",

            "info": {"title": slug.replace("-", " ").title(), "version": "0.1.0"},

            "paths": {

                f"/api/{slug}": {

                    "get": {"operationId": f"list_{slug.replace('-', '_')}", "responses": {"200": {"description": "ok"}}},

                    "post": {"operationId": f"create_{slug.replace('-', '_')}", "responses": {"201": {"description": "created"}}},

                }

            },

        }

        table = slug.replace("-", "_")[:24] or "items"

        ddl = (

            f"CREATE TABLE IF NOT EXISTS {table} (\n"

            "    id TEXT PRIMARY KEY,\n"

            "    payload TEXT NOT NULL DEFAULT '',\n"

            "    created_at REAL NOT NULL\n"

            ");"

        )

        postgres = (

            f"-- postgresql\nCREATE TABLE IF NOT EXISTS {table} (\n"

            "    id TEXT PRIMARY KEY,\n"

            "    payload TEXT NOT NULL DEFAULT '',\n"

            "    created_at DOUBLE PRECISION NOT NULL\n"

            ");"

        )

        contracts = [

            {"name": f"{_type_name(slug)}Store", "kind": "interface", "role": "port"},

            {"name": f"Sqlite{_type_name(slug)}Store", "kind": "class", "role": "adapter"},

        ]

        dip = [{"port": f"{_type_name(slug)}Store", "adapter": f"Sqlite{_type_name(slug)}Store", "injects": f"{_type_name(slug)}Service"}]

    return SoftwareSpec(

        spec_id=spec_id,

        goal=text[:800],

        openapi=openapi,

        ddl=ddl,

        postgres_ddl=postgres,

        contracts=contracts,

        dip=dip,

        created_at=time.time(),

    )





def spec_from_public(row: dict[str, Any]) -> SoftwareSpec:

    return SoftwareSpec(

        spec_id=str(row.get("id") or ""),

        goal=str(row.get("goal") or ""),

        openapi=row.get("openapi") if isinstance(row.get("openapi"), dict) else {},

        ddl=str(row.get("ddl") or ""),

        postgres_ddl=str(row.get("postgres_ddl") or ""),

        contracts=[item for item in (row.get("contracts") or []) if isinstance(item, dict)],

        dip=[item for item in (row.get("dip") or []) if isinstance(item, dict)],

        created_at=float(row.get("created_at") or 0.0),

    )





def _session_openapi() -> dict[str, Any]:

    return {

        "openapi": "3.0.3",

        "info": {"title": "Authenticated Session API", "version": "0.1.0"},

        "paths": {

            "/api/sessions": {

                "post": {

                    "operationId": "createSession",

                    "requestBody": {

                        "required": True,

                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Login"}}},

                    },

                    "responses": {"201": {"description": "session created"}, "401": {"description": "invalid credentials"}},

                }

            },

            "/api/sessions/current": {

                "get": {

                    "operationId": "currentSession",

                    "security": [{"sessionCookie": []}],

                    "responses": {"200": {"description": "ok"}, "401": {"description": "unauthorized"}},

                }

            },

            "/api/sessions/{id}": {

                "delete": {

                    "operationId": "revokeSession",

                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],

                    "responses": {"204": {"description": "revoked"}},

                }

            },

        },

        "components": {

            "securitySchemes": {"sessionCookie": {"type": "apiKey", "in": "cookie", "name": "sid"}},

            "schemas": {

                "Login": {

                    "type": "object",

                    "required": ["email", "password"],

                    "properties": {"email": {"type": "string"}, "password": {"type": "string"}},

                }

            },

        },

    }





def _session_ddl() -> tuple[str, str]:

    sqlite = """CREATE TABLE IF NOT EXISTS users (

    id TEXT PRIMARY KEY,

    email TEXT NOT NULL UNIQUE,

    password_hash TEXT NOT NULL,

    created_at REAL NOT NULL

);

CREATE TABLE IF NOT EXISTS sessions (

    id TEXT PRIMARY KEY,

    user_id TEXT NOT NULL,

    token_hash TEXT NOT NULL UNIQUE,

    expires_at REAL NOT NULL,

    created_at REAL NOT NULL,

    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE

);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);"""

    postgres = """-- postgresql

CREATE TABLE IF NOT EXISTS users (

    id TEXT PRIMARY KEY,

    email TEXT NOT NULL UNIQUE,

    password_hash TEXT NOT NULL,

    created_at DOUBLE PRECISION NOT NULL

);

CREATE TABLE IF NOT EXISTS sessions (

    id TEXT PRIMARY KEY,

    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    token_hash TEXT NOT NULL UNIQUE,

    expires_at DOUBLE PRECISION NOT NULL,

    created_at DOUBLE PRECISION NOT NULL

);"""

    return sqlite, postgres





def _slug(text: str) -> str:

    words = re.findall(r"[a-zA-Z][a-zA-Z0-9]+", text.lower())

    skip = {"a", "an", "the", "and", "with", "for", "from", "into", "design", "implement"}

    picked = [word for word in words if word not in skip][:4]

    return "-".join(picked) or "service"





def _type_name(slug: str) -> str:

    return "".join(part.title() for part in slug.replace("_", "-").split("-") if part) or "Service"





def _as_dict(raw: Any) -> dict[str, Any]:

    if isinstance(raw, dict):

        return dict(raw)

    if isinstance(raw, str) and raw.strip():

        try:

            loaded = json.loads(raw)

        except json.JSONDecodeError:

            return {}

        return loaded if isinstance(loaded, dict) else {}

    return {}

