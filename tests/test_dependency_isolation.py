"""Pure offline import/source fixtures. Never connect to DB/HTTP/exchanges."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

MODULE = Path(__file__).resolve().parents[1]
CHECKER = MODULE / "scripts/verify_isolation.py"


def check(root: Path):
    # Execute only the standalone standard-library checker, not the fixtures.
    completed = subprocess.run([sys.executable, str(CHECKER), "--root", str(root), "--json"],
                               capture_output=True, text=True, timeout=15, check=False)
    result = json.loads(completed.stdout)
    assert completed.returncode == (0 if result["ok"] else 1)
    return result


@pytest.fixture
def isolated(tmp_path):
    root = tmp_path / "sol-ai-trading-system"
    (root / "app").mkdir(parents=True)
    (root / "app/__init__.py").write_text("")
    (root / "app/pure.py").write_text("from decimal import Decimal\ndef quantity(): return Decimal('1')\n")
    (root / "main.py").write_text("from app.pure import quantity\n")
    return root


def test_current_module_passes_complete_local_source_check():
    result = check(MODULE)
    assert result["ok"], result["errors"]
    assert "app/execution/bridge_client.py" in result["imports"]
    assert "app.dashboard.server" in result["imports"]["app/dashboard/__init__.py"]


def test_independent_checker_needs_no_old_repo_or_business_import(isolated):
    # If checker imported business code, this file would explode. AST parse only.
    (isolated / "app/pure.py").write_text("raise RuntimeError('MUST_NOT_EXECUTE')\n")
    destination = isolated / "verify.py"
    shutil.copyfile(CHECKER, destination)
    # Place checker outside scanned module so arbitrary root entrypoint stays forbidden.
    relocated = isolated.parent / "verify.py"
    destination.rename(relocated)
    completed = subprocess.run([sys.executable, str(relocated), "--root", str(isolated), "--json"],
                               text=True, capture_output=True, timeout=15, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["ok"]


@pytest.mark.parametrize("source", [
    "import pymysql\n", "import sqlalchemy\n", "from server.db import pool\n",
    "from ..server.db import pool\n", "from app.missing import pool\n",
    "from importlib import import_module as load\nload('server.db')\n",
    "fn = __import__\nfn('server.db')\n", "exec('import server.db')\n",
    "eval('__import__')('server.db')\n", "from builtins import __import__ as load\n",
    "import sys as system\nsystem.path.append('../../')\n",
    "from sys import path as search\nsearch.append('../../')\n",
    "getattr(__builtins__, '__import__')('server.db')\n",
    "globals()['__import__']('server.db')\n",
    "getattr(object, '__subclasses__')()\n",
    "import urllib.request\n", "from urllib import request\n",
    "import os\nos.system('python ../../server/main.py')\n",
])
def test_rejects_unapproved_dynamic_and_search_path_imports(isolated, source):
    (isolated / "app/pure.py").write_text(source)
    assert not check(isolated)["ok"]


def test_transitive_dependency_cannot_hide_in_unused_module(isolated):
    (isolated / "app/pure.py").write_text("from app.helper import calculate\n")
    (isolated / "app/helper.py").write_text("from app.hidden import pool\n")
    (isolated / "app/hidden.py").write_text("import psycopg2\n")
    result = check(isolated)
    assert not result["ok"]
    assert any("app/hidden.py" in item and "psycopg2" in item for item in result["errors"])
    (isolated / "app/pure.py").write_text("value = 1\n")
    assert not check(isolated)["ok"]  # even unreachable sources must be clean


@pytest.mark.parametrize("value", [
    "mysql://old-db.internal/copy", "postgresql://copy.internal/account",
    "/srv/tradingskill-test/data.sqlite", "../../server/db.ts",
    "https://ts.forttrade.xyz/api/trpc/order", "http://127.0.0.1:3002/api/orders",
    "http://old-execution.internal/order", "https://unapproved.example/query",
])
def test_rejects_old_data_paths_and_http_execution_backdoors(isolated, value):
    (isolated / "app/pure.py").write_text(f"endpoint = {value!r}\n")
    assert not check(isolated)["ok"]


def test_concatenated_endpoint_rejected(isolated):
    (isolated / "app/pure.py").write_text("endpoint = 'http://' + 'old-execution.internal' + '/orders'\n")
    assert not check(isolated)["ok"]


def test_http_capability_is_not_allowed_in_arbitrary_module(isolated):
    (isolated / "app/pure.py").write_text("import httpx\n")
    assert not check(isolated)["ok"]


def test_source_symlink_cannot_escape_package(isolated, tmp_path):
    external = tmp_path / "old.py"
    external.write_text("secret = 'FAKE_SENTINEL'\n")
    (isolated / "app/leak.py").symlink_to(external)
    result = check(isolated)
    assert not result["ok"]
    assert any("symlink" in item for item in result["errors"])


def test_browser_must_not_import_old_execution_service(isolated):
    assets = isolated / "app/dashboard/assets"
    assets.mkdir(parents=True)
    (assets / "unsafe.js").write_text("fetch('https://ts.forttrade.xyz/api/execute')")
    assert not check(isolated)["ok"]
    (assets / "unsafe.js").write_text("import('/old-system/execution.js')")
    assert not check(isolated)["ok"]


def test_unapproved_root_python_entrypoint_rejected(isolated):
    (isolated / "legacy_runner.py").write_text("import pymysql\n")
    assert not check(isolated)["ok"]


def test_deployment_templates_are_offline_and_live_bridge_disabled():
    service = (MODULE / "deploy/sol-ai.service").read_text()
    bridge = (MODULE / "deploy/sol-ai-bridge.service").read_text()
    assert "Type=oneshot" in service and "--isolation-smoke --cycles 1" in service
    assert "PrivateNetwork=true" in service and "IPAddressDeny=any" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "Restart=no" in service and "WantedBy=" not in service
    assert "ExecStart=/usr/bin/false" in bridge and "RefuseManualStart=true" in bridge
    assert "WantedBy=" not in bridge and "ExecStart=/usr/bin/node" not in bridge
    nginx = (MODULE / "deploy/nginx-sol-ai.conf.example").read_text()
    assert all(not line.strip() or line.lstrip().startswith("#") for line in nginx.splitlines())
