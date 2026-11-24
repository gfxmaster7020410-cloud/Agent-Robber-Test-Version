"""AST-aware 3-way merge and conventional commit messages.



Python path is the testable default. When the indexer cdylib exports

`robber_ast_merge`, that native Tree-Sitter implementation is preferred.

Git porcelain uses the local `git` binary so GNU Windows builds stay free of

libgit2 / PyO3 compile cost on 8GB RAM.

"""



from __future__ import annotations



import ast

import os

import shutil

import subprocess

from dataclasses import dataclass

from pathlib import Path

from typing import Any



from robber.jail import workspace_root





class ConflictError(ValueError):

    def __init__(self, symbol: str, detail: str = "") -> None:

        self.symbol = symbol

        super().__init__(detail or f"conflict on {symbol}")





@dataclass

class MergeResult:

    text: str

    ok: bool = True

    conflicts: list[str] | None = None



    def public(self) -> dict[str, Any]:

        return {"ok": self.ok, "text": self.text[:400], "conflicts": list(self.conflicts or [])}





def ast_merge(base: str, ours: str, theirs: str) -> str:

    try:

        from robber.indexer_bridge import ast_merge as native



        packed = native(base, ours, theirs)

        if isinstance(packed, dict):

            if packed.get("ok"):

                return str(packed.get("merged") or packed.get("text") or "")

            raise ConflictError(str(packed.get("symbol") or "module"), str(packed.get("error") or "conflict"))

        if isinstance(packed, str) and packed:

            return packed

    except FileNotFoundError:

        pass

    except ConflictError:

        raise

    except Exception:

        pass

    return python_ast_merge(base, ours, theirs)





def python_ast_merge(base: str, ours: str, theirs: str) -> str:

    left = _split_py(ours)

    right = _split_py(theirs)

    ancestor = _split_py(base)

    imports = sorted(set(ancestor.imports) | set(left.imports) | set(right.imports), key=str.lower)

    order: list[str] = []

    seen: set[str] = set()

    for name in (*left.order, *right.order):

        if name in seen:

            continue

        seen.add(name)

        order.append(name)

    blocks: dict[str, str] = {}

    for name in order:

        ours_b = left.blocks.get(name)

        theirs_b = right.blocks.get(name)

        base_b = ancestor.blocks.get(name)

        if ours_b is None and theirs_b is None:

            continue

        if ours_b is None:

            blocks[name] = theirs_b or ""

            continue

        if theirs_b is None:

            blocks[name] = ours_b

            continue

        if ours_b == theirs_b:

            blocks[name] = ours_b

            continue

        if base_b is not None and ours_b == base_b:

            blocks[name] = theirs_b

            continue

        if base_b is not None and theirs_b == base_b:

            blocks[name] = ours_b

            continue

        raise ConflictError(name, "both sides changed this symbol")

    doc = left.doc or right.doc or ancestor.doc

    parts: list[str] = []

    if doc:

        parts.append(f'"""{doc}"""')

        parts.append("")

    parts.extend(imports)

    if imports:

        parts.append("")

    for name in order:

        body = blocks.get(name)

        if not body:

            continue

        parts.append(body.rstrip())

        parts.append("")

    return "\n".join(parts).rstrip() + "\n"





def conventional_message(base: str, ours: str) -> str:

    try:

        from robber.indexer_bridge import commit_message as native



        text = native(base, ours)

        if text:

            return text

    except Exception:

        pass

    ancestor = _split_py(base)

    current = _split_py(ours)

    added = [name for name in current.order if name not in ancestor.blocks]

    changed = [

        name

        for name in current.order

        if name in ancestor.blocks and current.blocks.get(name) != ancestor.blocks.get(name)

    ]

    if added and all(name.startswith("test_") for name in added) and not changed:

        return f"test: add {', '.join(added[:3])}"

    if added and not changed:

        return f"feat: add {', '.join(added[:3])}"

    if changed and not added:

        kind = "fix" if any("fix" in (current.blocks.get(name) or "").lower() for name in changed) else "refactor"

        return f"{kind}: update {', '.join(changed[:3])}"

    if changed or added:

        return f"refactor: update {', '.join((changed + added)[:3])}"

    return "refactor: sync sources"





class GitEngine:

    """Workspace git helper. Never writes outside `root`."""



    def __init__(self, root: Path | str | None = None) -> None:

        self.root = Path(root).resolve() if root else workspace_root()



    def ast_merge(self, base: str, ours: str, theirs: str) -> str:

        return ast_merge(base, ours, theirs)



    def commit_message(self, base: str, ours: str) -> str:

        return conventional_message(base, ours)



    def commit_paths(self, paths: list[str], *, message: str, branch: str = "") -> dict[str, Any]:

        git = shutil.which("git")

        if not git:

            return {"ok": False, "error": "git not installed"}

        if not (self.root / ".git").is_dir():

            self._run(git, ["init", "-q"])

        rels = [item.replace("\\", "/") for item in paths if item and ".." not in Path(item).parts]

        if rels:

            self._run(git, ["add", "--", *rels])

        else:

            self._run(git, ["add", "-A"])

        env = os.environ.copy()

        env.setdefault("GIT_AUTHOR_NAME", "Agent Robber")

        env.setdefault("GIT_AUTHOR_EMAIL", "robber@local")

        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])

        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])

        code, out = self._run(

            git,

            [

                "-c",

                "user.name=Agent Robber",

                "-c",

                "user.email=robber@local",

                "commit",

                "--no-gpg-sign",

                "-m",

                message,

            ],

            env=env,

            check=False,

        )

        if branch and code == 0:

            self._run(git, ["branch", "-M", branch], check=False)

        return {"ok": code == 0, "message": message, "output": out[-800:]}



    def _run(

        self,

        git: str,

        args: list[str],

        *,

        env: dict[str, str] | None = None,

        check: bool = True,

    ) -> tuple[int, str]:

        proc = subprocess.run(

            [git, *args],

            cwd=str(self.root),

            env=env,

            capture_output=True,

            text=True,

            timeout=20,

        )

        blob = (proc.stdout or "") + (proc.stderr or "")

        if check and proc.returncode != 0:

            raise RuntimeError(blob.strip() or "git failed")

        return int(proc.returncode), blob





@dataclass

class _Module:

    imports: list[str]

    blocks: dict[str, str]

    order: list[str]

    doc: str





def _split_py(src: str) -> _Module:

    text = src or ""

    tree = ast.parse(text)

    lines = text.splitlines()

    imports: list[str] = []

    blocks: dict[str, str] = {}

    order: list[str] = []

    doc = ast.get_docstring(tree) or ""

    for node in tree.body:

        if isinstance(node, (ast.Import, ast.ImportFrom)):

            stmt = _segment(lines, node)

            if stmt and stmt not in imports:

                imports.append(stmt)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):

            name = node.name

            order.append(name)

            blocks[name] = _segment(lines, node)

    return _Module(imports=imports, blocks=blocks, order=order, doc=doc)





def _segment(lines: list[str], node: ast.AST) -> str:

    start = int(getattr(node, "lineno", 1) or 1)

    for deco in getattr(node, "decorator_list", []) or []:

        start = min(start, int(getattr(deco, "lineno", start) or start))

    end = int(getattr(node, "end_lineno", start) or start)

    start = max(1, start)

    end = max(start, end)

    return "\n".join(lines[start - 1 : end]).rstrip()

