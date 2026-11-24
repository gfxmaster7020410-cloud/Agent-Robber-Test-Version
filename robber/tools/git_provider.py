"""Zero-dependency GitHub / GitLab REST client for automated pull requests.

Prefers the `gh` CLI when present, then vault/env tokens (`GITHUB_TOKEN`,
`GH_TOKEN`, `GITLAB_TOKEN`) over HTTPS. Never logs secrets.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robber.jail import workspace_root
from robber.tools.git_manager import GitManager

logger = logging.getLogger(__name__)

DEFAULT_LABELS = ("automated-pr", "agent-robber", "needs-review")


@dataclass
class PRResult:
    ok: bool
    url: str = ""
    number: int = 0
    provider: str = ""
    branch: str = ""
    title: str = ""
    draft: bool = False
    error: str = ""
    labels: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return asdict(self)


class GitProvider:
    """Push a task branch and open a GitHub PR or GitLab MR."""

    def __init__(self, root: Path | str | None = None, *, manager: Any = None) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.git = GitManager(self.root, manager=manager)
        self.manager = manager

    def push_and_create_pr(
        self,
        branch_name: str,
        pr_title: str,
        pr_body: str,
        draft: bool = False,
        *,
        labels: list[str] | None = None,
        dry_run: bool = False,
    ) -> PRResult:
        branch = (branch_name or self.git.current_branch()).strip()
        title = (pr_title or branch).strip()
        body = pr_body or ""
        tags = list(labels or DEFAULT_LABELS)
        if not branch:
            return PRResult(ok=False, error="no branch to push")
        if dry_run:
            return PRResult(
                ok=True,
                url="",
                provider=self.detect_provider() or "dry-run",
                branch=branch,
                title=title,
                draft=draft,
                labels=tags,
            )
        push = self._push(branch)
        if not push[0]:
            return PRResult(ok=False, error=push[1] or "git push failed", branch=branch, title=title, draft=draft)
        provider = self.detect_provider()
        if provider == "gitlab":
            result = self._create_gitlab_mr(branch, title, body, draft=draft, labels=tags)
        else:
            result = self._create_github_pr(branch, title, body, draft=draft, labels=tags)
        result.branch = branch
        result.title = title
        result.draft = draft
        result.labels = tags
        if result.ok and self.manager is not None:
            try:
                self.manager.add_artifact(
                    "pr:create",
                    body=json.dumps(result.public(), ensure_ascii=True)[:4000],
                    kind="pr",
                    task_id=None,
                    exit_code=0,
                )
            except Exception:
                logger.debug("pr artifact skipped")
        return result

    def detect_provider(self) -> str:
        remote = self._remote_url()
        lower = remote.lower()
        if "gitlab" in lower:
            return "gitlab"
        if "github" in lower or lower:
            return "github"
        return ""

    def _push(self, branch: str) -> tuple[bool, str]:
        code, blob = self.git._run(["push", "-u", "origin", branch], check=False)
        return code == 0, blob

    def _create_github_pr(
        self,
        branch: str,
        title: str,
        body: str,
        *,
        draft: bool,
        labels: list[str],
    ) -> PRResult:
        gh = shutil.which("gh")
        if gh:
            argv = [gh, "pr", "create", "--title", title, "--body", body, "--head", branch]
            if draft:
                argv.append("--draft")
            for label in labels:
                argv.extend(["--label", label])
            proc = subprocess.run(
                argv,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=45,
            )
            blob = ((proc.stdout or "") + (proc.stderr or "")).strip()
            url = _first_url(blob)
            if proc.returncode == 0 and url:
                return PRResult(ok=True, url=url, provider="github", branch=branch)
            if proc.returncode == 0:
                return PRResult(ok=True, url=blob.splitlines()[-1] if blob else "", provider="github", branch=branch)

        owner_repo = self._github_owner_repo()
        token = self._token("github")
        if not owner_repo or not token:
            return PRResult(
                ok=False,
                error="github credentials missing (set GITHUB_TOKEN or install gh)",
                provider="github",
                branch=branch,
            )
        owner, repo = owner_repo
        payload = {
            "title": title,
            "head": branch,
            "base": self.git.default_branch(),
            "body": body,
            "draft": bool(draft),
        }
        data, err, status = _json_request(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            payload,
            token=token,
            accept="application/vnd.github+json",
        )
        if err or not isinstance(data, dict):
            return PRResult(ok=False, error=err or "github pr failed", provider="github", branch=branch)
        number = int(data.get("number") or 0)
        url = str(data.get("html_url") or "")
        if labels and number:
            _json_request(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/labels",
                {"labels": labels},
                token=token,
                accept="application/vnd.github+json",
            )
        return PRResult(ok=bool(url), url=url, number=number, provider="github", branch=branch)

    def _create_gitlab_mr(
        self,
        branch: str,
        title: str,
        body: str,
        *,
        draft: bool,
        labels: list[str],
    ) -> PRResult:
        host, project = self._gitlab_project()
        token = self._token("gitlab")
        if not host or not project or not token:
            return PRResult(
                ok=False,
                error="gitlab credentials missing (set GITLAB_TOKEN)",
                provider="gitlab",
                branch=branch,
            )
        encoded = urllib.parse.quote(project, safe="")
        payload = {
            "source_branch": branch,
            "target_branch": self.git.default_branch(),
            "title": f"Draft: {title}" if draft else title,
            "description": body,
            "labels": ",".join(labels),
        }
        data, err, _status = _json_request(
            f"https://{host}/api/v4/projects/{encoded}/merge_requests",
            payload,
            token=token,
            token_header="PRIVATE-TOKEN",
        )
        if err or not isinstance(data, dict):
            return PRResult(ok=False, error=err or "gitlab mr failed", provider="gitlab", branch=branch)
        url = str(data.get("web_url") or "")
        return PRResult(
            ok=bool(url),
            url=url,
            number=int(data.get("iid") or 0),
            provider="gitlab",
            branch=branch,
        )

    def _token(self, provider: str) -> str:
        env_names = {
            "github": ("GITHUB_TOKEN", "GH_TOKEN"),
            "gitlab": ("GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"),
        }
        for name in env_names.get(provider, ()):
            secret = (os.environ.get(name) or "").strip()
            if secret:
                return secret
        try:
            from robber.vault import get_key

            secret = (get_key(provider) or "").strip()
            if secret:
                return secret
        except Exception:
            pass
        try:
            from robber.security.vault import get_vault

            secret = (get_vault(self.root).get(provider) or "").strip()
            if secret:
                return secret
        except Exception:
            logger.debug("vault token skipped")
        return ""

    def _remote_url(self) -> str:
        return self.git._out(["remote", "get-url", "origin"], check=False).strip()

    def _github_owner_repo(self) -> tuple[str, str] | None:
        remote = self._remote_url()
        match = _GITHUB_REMOTE.search(remote)
        if not match:
            return None
        return match.group("owner"), match.group("repo")

    def _gitlab_project(self) -> tuple[str, str]:
        remote = self._remote_url()
        match = _GITLAB_REMOTE.search(remote)
        if not match:
            return "", ""
        return match.group("host"), match.group("project")


_GITHUB_REMOTE = re.compile(
    r"(?:github\.com[:/])(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
    re.I,
)
_GITLAB_REMOTE = re.compile(
    r"(?:https?://|git@)(?P<host>[^/:]+)[:/](?P<project>.+?)(?:\.git)?$",
    re.I,
)


def _first_url(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("http://") or line.startswith("https://"):
            return line
    return ""


def _json_request(
    url: str,
    payload: dict[str, Any],
    *,
    token: str,
    token_header: str = "Authorization",
    accept: str = "application/json",
) -> tuple[Any, str, int]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": accept,
        "Content-Type": "application/json",
        "User-Agent": "agent-robber",
    }
    if token_header == "Authorization":
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers[token_header] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            status = int(getattr(resp, "status", 200) or 200)
            return json.loads(raw or "{}"), "", status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        return None, f"HTTP {exc.code}: {detail}", int(exc.code)
    except Exception as exc:
        return None, str(exc), 0
