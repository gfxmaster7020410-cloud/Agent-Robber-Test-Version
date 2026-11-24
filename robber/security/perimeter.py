"""Filesystem perimeter that blocks Agent Robber from rewriting its own engine."""



from __future__ import annotations



import os

from pathlib import Path



ALLOW_SELF_EDIT_ENV = "ROBBER_ALLOW_SELF_EDIT"

AGENT_ROOT_ENV = "ROBBER_AGENT_ROOT"



_ROOT_CONFIG_FILES = (

    "server.py",

    "api_server.py",

    "pyproject.toml",

    "requirements.txt",

    ".env",

    ".env.example",

)



_ENGINE_DIRS = ("robber", "indexer", "gateway", "ui", "gui")





class SelfModificationProtectedError(PermissionError):

    """Raised when an automated task tries to mutate Agent Robber engine files."""





def agent_install_root() -> Path:

    raw = (os.environ.get(AGENT_ROOT_ENV) or "").strip()

    if raw:

        return Path(raw).expanduser().resolve()

    return Path(__file__).resolve().parents[2]





def protected_agent_roots(install: Path | None = None) -> tuple[Path, ...]:

    base = (install or agent_install_root()).resolve()

    roots: list[Path] = [base / name for name in _ENGINE_DIRS]

    roots.extend(base / name for name in _ROOT_CONFIG_FILES)

    roots.append(base / "data" / "ssot.db")

    roots.append(base / ".robber" / "ssot.db")

    return tuple(roots)





def __getattr__(name: str) -> tuple[Path, ...]:

    if name == "PROTECTED_AGENT_ROOTS":

        return protected_agent_roots()

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")





def self_edit_allowed() -> bool:

    return (os.environ.get(ALLOW_SELF_EDIT_ENV) or "").strip() in {"1", "true", "yes", "on"}





def is_protected_path(target: str | Path, *, install: Path | None = None) -> bool:

    try:

        resolved = Path(target).expanduser().resolve()

    except OSError:

        return False

    for root in protected_agent_roots(install):

        if _covers(resolved, root):

            return True

    return False





def validate_file_mutation(target_path: str, is_dev_mode: bool = False) -> bool:

    """Return True if the write/append/delete is allowed.



    Automated tests, slash commands, swarm workers, and self-heal always hit this

    gate. Engine files require ROBBER_ALLOW_SELF_EDIT=1. is_dev_mode does not

    bypass the flag.

    """

    del is_dev_mode

    if not str(target_path or "").strip():

        return True

    if not is_protected_path(target_path):

        return True

    if self_edit_allowed():

        return True

    raise SelfModificationProtectedError(

        "blocked self-modification of Agent Robber engine file: "

        f"{target_path}. Set {ALLOW_SELF_EDIT_ENV}=1 to allow."

    )





def _covers(child: Path, parent: Path) -> bool:

    try:

        left = child.resolve()

        right = parent.resolve()

    except OSError:

        return False

    if os.path.normcase(str(left)) == os.path.normcase(str(right)):

        return True

    if not right.exists() or right.is_file():

        return os.path.normcase(str(left)) == os.path.normcase(str(right))

    left_s = os.path.normcase(str(left))

    right_s = os.path.normcase(str(right))

    return left_s.startswith(right_s.rstrip("\\/") + os.sep)

