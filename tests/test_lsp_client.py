"""LSP config loader and STDIO JSON-RPC client."""



from __future__ import annotations



import json

import sys

from pathlib import Path

from unittest.mock import patch



from robber.config import LSP_CONFIG_PATH, LSP_MEMORY_BOUND_MB

from robber.diagnostics.lsp_security import LspClient, boot_lsp_config, load_lsp_config, server_spec_for



STUB = Path(__file__).resolve().parent / "lsp_stdio_stub.py"





def test_load_lsp_config_reads_servers(tmp_path: Path) -> None:

    path = tmp_path / "lsp_servers.json"

    path.write_text(

        json.dumps(

            {

                "version": "1.0",

                "memory_bound_mb": 512,

                "servers": {

                    "python": {

                        "enabled": True,

                        "command": ["pyright-langserver", "--stdio"],

                        "languages": ["python"],

                    }

                },

            }

        ),

        encoding="utf-8",

    )

    cfg = load_lsp_config(path)

    spec = server_spec_for(Path("mod.py"), cfg)

    assert spec is not None

    name, row = spec

    assert name == "python"

    assert row["command"][0] == "pyright-langserver"

    assert boot_lsp_config(path)["memory_bound_mb"] == LSP_MEMORY_BOUND_MB





def test_pytest_does_not_spawn_stdio() -> None:

    cfg = {

        "version": "1.0",

        "memory_bound_mb": 512,

        "servers": {

            "python": {

                "enabled": True,

                "command": [sys.executable, str(STUB)],

                "languages": ["python"],

            }

        },

    }

    with patch("robber.diagnostics.lsp_security.subprocess.Popen") as popen:

        diags = LspClient(config=cfg).diagnose("util.py", "def add(a, b):\n    return a + b\n")

        popen.assert_not_called()

    assert not any(item.severity == "error" for item in diags)





def test_stdio_jsonrpc_stub(tmp_path: Path) -> None:

    cfg = {

        "version": "1.0",

        "memory_bound_mb": 512,

        "servers": {

            "python": {

                "enabled": True,

                "command": [sys.executable, str(STUB)],

                "languages": ["python"],

            }

        },

    }

    client = LspClient(allow_spawn=True, config=cfg)

    try:

        clean = client.diagnose(tmp_path / "ok.py", "def add(a, b):\n    return a + b\n")

        assert clean == []

        broken = client.diagnose(tmp_path / "bad.py", "broken = 1\n")

        assert any(item.message == "stub error" for item in broken)

    finally:

        client.close()





def test_setup_lsp_writes_json(tmp_path: Path) -> None:

    root = Path(__file__).resolve().parents[1]

    sys.path.insert(0, str(root / "scripts"))

    from setup_lsp import generate_lsp_config  # type: ignore



    dest = tmp_path / "lsp_servers.json"

    generate_lsp_config("pyright-langserver", "typescript-language-server", "gopls", dest=dest)

    data = json.loads(dest.read_text(encoding="utf-8"))

    assert data["servers"]["python"]["enabled"] is True

    assert data["servers"]["python"]["command"][0] == "pyright-langserver"

    assert "--stdio" in data["servers"]["python"]["command"]

    assert data["servers"]["go"]["command"] == ["gopls"]

    assert LSP_CONFIG_PATH.name == "lsp_servers.json"

