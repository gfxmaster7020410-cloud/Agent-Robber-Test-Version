"""Account CRUD, encrypted credentials, and Playwright cookie bridge."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from robber.db.database import Database
from robber.engine.proxy_rotator import ProxyConfig, ProxyManager
from robber.jail import workspace_root

PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
ACCOUNT_STATUSES = ("idle", "active", "running", "cooldown", "disabled", "error")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_fernet() -> Fernet:
    raw = os.environ.get("ROBBER_ACCOUNT_KEY", "").strip()
    if raw:
        key = raw.encode("utf-8") if isinstance(raw, str) else raw
        return Fernet(key)
    path = workspace_root() / ".robber" / "account.key"
    if path.is_file():
        return Fernet(path.read_bytes().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    return Fernet(key)


def normalize_cookies(cookies: Any) -> list[dict[str, Any]]:
    """Accept Playwright cookie lists or a simple name→value map."""
    if cookies is None:
        return []
    if isinstance(cookies, dict) and "cookies" in cookies:
        cookies = cookies["cookies"]
    if isinstance(cookies, dict):
        if "name" in cookies and "value" in cookies:
            return [dict(cookies)]
        out: list[dict[str, Any]] = []
        domain = cookies.get("domain") if isinstance(cookies.get("domain"), str) else None
        for name, value in cookies.items():
            if name in {"url", "domain", "path"}:
                continue
            item: dict[str, Any] = {
                "name": str(name),
                "value": str(value),
                "path": "/",
            }
            if domain:
                item["domain"] = domain
            out.append(item)
        return out
    if isinstance(cookies, list):
        return [
            dict(item)
            for item in cookies
            if isinstance(item, dict) and item.get("name") is not None
        ]
    raise ValueError("cookies must be a JSON object or array")


class AccountManager:
    def __init__(self, db: Database, proxy_manager: ProxyManager) -> None:
        self.db = db
        self.proxy_manager = proxy_manager
        self._fernet = _load_fernet()

    def _encrypt(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode("utf-8")).decode("utf-8")

    def _decrypt(self, blob: str) -> str:
        if not blob:
            return ""
        try:
            return self._fernet.decrypt(blob.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("stored credential could not be decrypted") from exc

    @staticmethod
    def _validate_profile_id(profile_id: str) -> str:
        value = profile_id.strip()
        if not PROFILE_ID_RE.match(value):
            raise ValueError("profile_id must be 1-64 letters, numbers, or . _ : -")
        return value

    async def list_accounts(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT a.profile_id, a.username, a.status, a.assigned_proxy_id,
                   a.created_at, a.last_active,
                   CASE WHEN c.profile_id IS NULL THEN 0 ELSE 1 END AS has_session
            FROM accounts a
            LEFT JOIN session_cookies c ON c.profile_id = a.profile_id
            ORDER BY a.created_at ASC
            """
        )
        return [self._public_row(row) for row in rows]

    async def get_account(self, profile_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT a.profile_id, a.username, a.status, a.assigned_proxy_id,
                   a.created_at, a.last_active,
                   CASE WHEN c.profile_id IS NULL THEN 0 ELSE 1 END AS has_session
            FROM accounts a
            LEFT JOIN session_cookies c ON c.profile_id = a.profile_id
            WHERE a.profile_id = ?
            """,
            (self._validate_profile_id(profile_id),),
        )
        return self._public_row(row) if row else None

    def _public_row(self, row: dict[str, Any]) -> dict[str, Any]:
        proxy_id = row.get("assigned_proxy_id")
        proxy = self.proxy_manager.public_proxy(str(proxy_id)) if proxy_id else None
        return {
            "profile_id": row["profile_id"],
            "username": row["username"],
            "status": row["status"],
            "assigned_proxy_id": proxy_id,
            "proxy": proxy,
            "has_session": bool(row.get("has_session")),
            "created_at": row["created_at"],
            "last_active": row.get("last_active"),
        }

    async def upsert_account(
        self,
        *,
        username: str,
        password: str | None = None,
        profile_id: str | None = None,
        status: str = "idle",
        proxy_id: str | None = None,
        proxy: dict[str, Any] | ProxyConfig | None = None,
    ) -> dict[str, Any]:
        username = username.strip()
        if not username:
            raise ValueError("username is required")
        status_value = (status or "idle").strip().lower()
        if status_value not in ACCOUNT_STATUSES:
            raise ValueError("invalid account status")

        assigned_proxy_id: str | None = None
        if isinstance(proxy, ProxyConfig):
            registered = self.proxy_manager.register_proxy(proxy)
            assigned_proxy_id = registered.id
        elif isinstance(proxy, dict) and proxy.get("host"):
            registered = self.proxy_manager.register_proxy(ProxyConfig.from_mapping(proxy))
            assigned_proxy_id = registered.id
        elif proxy_id:
            assigned_proxy_id = str(proxy_id).strip()

        existing = None
        if profile_id:
            existing = await self.db.fetchone(
                "SELECT profile_id, encrypted_password FROM accounts WHERE profile_id = ?",
                (self._validate_profile_id(profile_id),),
            )

        if existing is None:
            if not password or not password.strip():
                raise ValueError("password is required for a new account")
            profile_id = self._validate_profile_id(profile_id or f"acc-{uuid.uuid4().hex[:10]}")
            if assigned_proxy_id is None:
                assigned = self.proxy_manager.assign_to_profile(profile_id)
                assigned_proxy_id = assigned.id if assigned else None
            else:
                self.proxy_manager.attach_saved(profile_id, assigned_proxy_id)
            now = _utc_now()
            await self.db.execute(
                """
                INSERT INTO accounts (
                    profile_id, username, encrypted_password, status,
                    assigned_proxy_id, created_at, last_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    username,
                    self._encrypt(password.strip()),
                    status_value,
                    assigned_proxy_id,
                    now,
                    now,
                ),
            )
        else:
            profile_id = str(existing["profile_id"])
            secret = (
                self._encrypt(password.strip())
                if password and password.strip()
                else str(existing["encrypted_password"])
            )
            if assigned_proxy_id is None:
                assigned_proxy_id = await self._existing_proxy(profile_id)
                if assigned_proxy_id:
                    self.proxy_manager.attach_saved(profile_id, assigned_proxy_id)
                else:
                    assigned = self.proxy_manager.assign_to_profile(profile_id)
                    assigned_proxy_id = assigned.id if assigned else None
            else:
                self.proxy_manager.attach_saved(profile_id, assigned_proxy_id)
            await self.db.execute(
                """
                UPDATE accounts
                SET username = ?, encrypted_password = ?, status = ?,
                    assigned_proxy_id = ?, last_active = ?
                WHERE profile_id = ?
                """,
                (username, secret, status_value, assigned_proxy_id, _utc_now(), profile_id),
            )

        row = await self.get_account(profile_id)
        if row is None:
            raise RuntimeError("account upsert failed")
        await self.log_execution(profile_id, "upsert_account", "ok", "account saved")
        return row

    async def _existing_proxy(self, profile_id: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT assigned_proxy_id FROM accounts WHERE profile_id = ?",
            (profile_id,),
        )
        value = (row or {}).get("assigned_proxy_id")
        return str(value) if value else None

    async def delete_account(self, profile_id: str) -> bool:
        profile_id = self._validate_profile_id(profile_id)
        existing = await self.db.fetchone(
            "SELECT profile_id FROM accounts WHERE profile_id = ?",
            (profile_id,),
        )
        if existing is None:
            return False
        await self.db.execute("DELETE FROM session_cookies WHERE profile_id = ?", (profile_id,))
        await self.db.execute("DELETE FROM execution_logs WHERE profile_id = ?", (profile_id,))
        await self.db.execute("DELETE FROM accounts WHERE profile_id = ?", (profile_id,))
        self.proxy_manager.unassign(profile_id)
        return True

    async def load_credentials(self, profile_id: str) -> tuple[str, str] | None:
        """Return (username, password) for the local login flow. Never log the secret."""
        row = await self.db.fetchone(
            "SELECT username, encrypted_password FROM accounts WHERE profile_id = ?",
            (self._validate_profile_id(profile_id),),
        )
        if row is None:
            return None
        return str(row["username"]), self._decrypt(str(row["encrypted_password"] or ""))

    async def save_session_cookies(self, profile_id: str, cookies_dict: Any) -> None:
        profile_id = self._validate_profile_id(profile_id)
        account = await self.db.fetchone(
            "SELECT profile_id FROM accounts WHERE profile_id = ?",
            (profile_id,),
        )
        if account is None:
            raise ValueError(f"unknown profile '{profile_id}'")
        payload = json.dumps(normalize_cookies(cookies_dict), separators=(",", ":"))
        await self.db.execute(
            """
            INSERT INTO session_cookies (profile_id, cookies_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                cookies_json = excluded.cookies_json,
                updated_at = excluded.updated_at
            """,
            (profile_id, self._encrypt(payload), _utc_now()),
        )
        await self.db.execute(
            "UPDATE accounts SET last_active = ? WHERE profile_id = ?",
            (_utc_now(), profile_id),
        )

    async def load_session_cookies(self, profile_id: str) -> list[dict[str, Any]]:
        row = await self.db.fetchone(
            "SELECT cookies_json FROM session_cookies WHERE profile_id = ?",
            (self._validate_profile_id(profile_id),),
        )
        if row is None:
            return []
        raw = str(row["cookies_json"] or "")
        try:
            text = self._decrypt(raw)
        except ValueError:
            text = raw
        try:
            parsed = json.loads(text or "[]")
        except json.JSONDecodeError:
            return []
        return normalize_cookies(parsed)

    async def set_status(self, profile_id: str, status: str) -> None:
        status_value = status.strip().lower()
        if status_value not in ACCOUNT_STATUSES:
            raise ValueError("invalid account status")
        await self.db.execute(
            "UPDATE accounts SET status = ?, last_active = ? WHERE profile_id = ?",
            (status_value, _utc_now(), self._validate_profile_id(profile_id)),
        )

    async def log_execution(
        self,
        profile_id: str | None,
        action_name: str,
        status: str,
        log_message: str = "",
    ) -> None:
        bound: str | None = None
        if profile_id:
            exists = await self.db.fetchone(
                "SELECT profile_id FROM accounts WHERE profile_id = ?",
                (profile_id,),
            )
            if exists is not None:
                bound = profile_id
        await self.db.execute(
            """
            INSERT INTO execution_logs (profile_id, action_name, status, log_message, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                bound,
                action_name[:120],
                status[:40],
                (log_message or "")[:2000],
                _utc_now(),
            ),
        )

    async def sync_proxies(self) -> int:
        """Re-attach each stored assigned_proxy_id onto ProxyManager at boot."""
        rows = await self.db.fetchall(
            "SELECT profile_id, assigned_proxy_id FROM accounts WHERE assigned_proxy_id IS NOT NULL"
        )
        attached = 0
        for row in rows:
            proxy_id = str(row["assigned_proxy_id"] or "").strip()
            profile_id = str(row["profile_id"])
            if not proxy_id:
                continue
            self.proxy_manager.attach_saved(profile_id, proxy_id)
            attached += 1
        return attached
