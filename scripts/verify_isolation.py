#!/usr/bin/env python3
"""Fail-closed source dependency check; never imports the trading application.

This checks the entire local Python source closure, including unused modules.
It is a build gate, not a replacement for OS network/file isolation. Third-party
package internals are not executed or certified here; pin/install/test them in
the separate offline acceptance environment.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

STDLIB_ALLOWLIST = frozenset({
    "__future__", "argparse", "asyncio", "collections", "contextlib", "dataclasses",
    "datetime", "decimal", "fcntl", "hashlib", "hmac", "html", "inspect", "io", "json",
    "logging", "math", "os", "pathlib", "random", "re", "secrets", "sqlite3",
    "stat", "sys", "tempfile", "threading", "time", "typing", "urllib", "uuid", "zoneinfo",
})
EXTERNAL_ALLOWLIST = frozenset({"yaml", "dotenv", "pydantic", "httpx", "websockets", "fastapi", "starlette", "uvicorn"})
IMPORT_LOCATION_ALLOWLIST = {
    "httpx": {"app/data/service.py", "app/alerts/telegram.py", "app/execution/bridge_client.py"},
    "websockets": {"app/data/service.py"},
    "fastapi": {"app/dashboard/server.py"},
    "starlette": {"app/dashboard/server.py"},
    "uvicorn": {"main.py"},
    "sqlite3": {"app/portfolio/manager.py", "app/utils/paths.py"},
}
FORBIDDEN_NAMES = frozenset({
    "__import__", "__builtins__", "__loader__", "__spec__", "eval", "exec", "compile",
    "globals", "locals", "breakpoint", "import_module", "load_module", "exec_module",
    "spec_from_file_location", "spec_from_loader", "SourceFileLoader", "ExtensionFileLoader",
})
FORBIDDEN_ATTRIBUTES = frozenset({
    "__subclasses__", "__globals__", "__builtins__", "__code__", "__loader__",
    "__getattribute__", "import_module", "load_module", "exec_module",
    "spec_from_file_location", "spec_from_loader", "SourceFileLoader", "ExtensionFileLoader",
})
PRIVATE_NETWORK_ENDPOINTS = {("http", "127.0.0.1", 8766), ("http", "localhost", 8766), ("http", "::1", 8766)}
PUBLIC_NETWORK_ENDPOINTS = {("https", "fapi.binance.com", 443), ("wss", "fstream.binance.com", 443), ("https", "api.telegram.org", 443)}
OLD_REFERENCES = re.compile(
    r"(?i)(?:mysql(?:\+\w+)?://|postgres(?:ql)?(?:\+\w+)?://|redis(?:s)?://|mongodb(?:\+srv)?://|"
    r"ts\.forttrade\.xyz|tradingskill-live-test|/srv/tradingskill(?:-test)?(?:/|\b)|"
    r"(?:^|[/'\"\\])server/(?:db|risk-gate|binance-poller|okx|_core)|"
    r"(?:^|[/'\"\\])(?:\.\./)+server/|"
    r"/Users/[^\s'\"]+/(?:Documents|\.codex/worktrees)/[^\s'\"]*TS交易系统)"
)


def _constant_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _constant_text(node.left), _constant_text(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        # Static portions still reveal hosts in f"https://old/{variable}".
        return "".join(item.value if isinstance(item, ast.Constant) and isinstance(item.value, str)
                       else "PLACEHOLDER" for item in node.values)
    return None


def _docstrings(tree: ast.AST) -> set[int]:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                if isinstance(node.body[0].value.value, str):
                    nodes.add(id(node.body[0].value))
    return nodes


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def verify(root: str | Path) -> dict:
    requested = Path(root).absolute()
    errors: list[str] = []
    if requested.is_symlink():
        return {"ok": False, "errors": ["Module root must not be a symlink"], "files": [], "imports": {}}
    root = requested.resolve()
    if not (root / "app").is_dir():
        return {"ok": False, "errors": ["Independent module must contain app/"], "files": [], "imports": {}}

    def fail(path: Path, line: int, message: str) -> None:
        errors.append(f"{path.relative_to(root)}:{line}: {message}")

    # Follow no directory symlinks and reject even currently unused source links.
    for area in (root / "app", root / "scripts"):
        if not area.exists():
            continue
        if area.is_symlink():
            errors.append(f"{area.name}: symlink forbidden")
            continue
        for path in area.rglob("*"):
            if path.is_symlink():
                fail(path, 0, "symlink forbidden in source package")
    files = sorted(path for path in (root / "app").rglob("*.py") if not path.is_symlink())
    if (root / "main.py").is_file():
        files.append(root / "main.py")
    # A new root Python entry point cannot silently escape the source closure.
    for path in root.glob("*.py"):
        if path.name != "main.py":
            fail(path, 0, "unapproved Python entry point outside app/")
    modules = {_module_name(path, root): path for path in files}
    imports: dict[str, list[str]] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        if not path.resolve().is_relative_to(root):
            fail(path, 0, "source resolves outside independent module")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (SyntaxError, UnicodeError, OSError) as exc:
            fail(path, getattr(exc, "lineno", 0) or 0, f"source cannot be parsed: {type(exc).__name__}")
            continue
        ignored_strings = _docstrings(tree)
        dependencies: set[str] = set()
        aliases = {alias.asname or alias.name: alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        module = _module_name(path, root)
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if any(alias.name == "*" for alias in node.names):
                    fail(path, node.lineno, "wildcard import forbidden")
                if node.module == "sys" and any(alias.name in {"path", "meta_path", "path_hooks", "modules", "path_importer_cache"} for alias in node.names):
                    fail(path, node.lineno, "import search/hooks alias forbidden")
                if node.module == "os" and any(alias.name in {"system", "popen", "execv", "execve", "spawnv", "spawnve", "putenv"} for alias in node.names):
                    fail(path, node.lineno, "external process/environment alias forbidden")
                if node.level:
                    parts = package.split(".") if package else []
                    if node.level > len(parts):
                        fail(path, node.lineno, "relative import escapes independent app package")
                        continue
                    prefix = parts[:len(parts) - node.level + 1]
                    target = ".".join(prefix + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                names = [target]
                for alias in node.names:
                    candidate = f"{target}.{alias.name}"
                    if candidate in modules:
                        names.append(candidate)
            for name in names:
                dependencies.add(name)
                top = name.split(".")[0]
                if top == "app":
                    if name not in modules:
                        fail(path, node.lineno, f"unresolved local dependency: {name}")
                elif top not in STDLIB_ALLOWLIST | EXTERNAL_ALLOWLIST:
                    fail(path, node.lineno, f"dependency not allowlisted: {name}")
                if top in IMPORT_LOCATION_ALLOWLIST and relative not in IMPORT_LOCATION_ALLOWLIST[top]:
                    fail(path, node.lineno, f"{top} capability not permitted in this module")
                if top == "urllib" and name != "urllib.parse":
                    fail(path, node.lineno, "only urllib.parse is allowed; alternate network transports are forbidden")
                if top in {"app", "main"} and (name.startswith("app.server") or any(part in {"db", "database", "orm", "daemon", "legacy"} for part in name.split("."))):
                    fail(path, node.lineno, f"old service/database-style dependency forbidden: {name}")
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                fail(path, node.lineno, f"dynamic execution/import capability forbidden: {node.id}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars" and not node.args:
                fail(path, node.lineno, "dynamic namespace access via vars() forbidden")
            if isinstance(node, ast.Attribute):
                if node.attr in FORBIDDEN_ATTRIBUTES:
                    fail(path, node.lineno, f"dynamic loading/introspection forbidden: {node.attr}")
                if isinstance(node.value, ast.Name):
                    owner = aliases.get(node.value.id, node.value.id)
                    if owner == "sys" and node.attr in {"path", "meta_path", "path_hooks", "modules", "path_importer_cache"}:
                        fail(path, node.lineno, f"import search/hooks mutation capability forbidden: sys.{node.attr}")
                    if owner == "os" and node.attr in {"system", "popen", "execv", "execve", "spawnv", "spawnve", "putenv"}:
                        fail(path, node.lineno, f"external process/environment mutation forbidden: os.{node.attr}")
            if id(node) in ignored_strings:
                continue
            value = _constant_text(node)
            if value is None:
                continue
            if value in FORBIDDEN_NAMES | FORBIDDEN_ATTRIBUTES:
                fail(path, getattr(node, "lineno", 0), "dynamic loading/introspection identifier string forbidden")
            if OLD_REFERENCES.search(value):
                fail(path, getattr(node, "lineno", 0), "old database/service/path reference forbidden")
            if value.startswith(("http://", "https://", "ws://", "wss://")):
                parsed = urlsplit(value)
                try:
                    port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
                except ValueError:
                    fail(path, getattr(node, "lineno", 0), "malformed network endpoint")
                    continue
                endpoint = (parsed.scheme, parsed.hostname, port)
                if parsed.username or endpoint not in PRIVATE_NETWORK_ENDPOINTS | PUBLIC_NETWORK_ENDPOINTS:
                    fail(path, getattr(node, "lineno", 0), "network endpoint not allowlisted")
        imports[relative] = sorted(dependencies)

    # The browser only talks to this backend. No import/worker/eval, CDN, old
    # execution service or arbitrary fetch target is allowed in static assets.
    assets = root / "app/dashboard/assets"
    for path in assets.glob("*") if assets.exists() else []:
        if path.suffix not in {".js", ".html"} or path.is_symlink():
            continue
        source = path.read_text(encoding="utf-8")
        if OLD_REFERENCES.search(source):
            fail(path, 0, "browser references old service/database/path")
        if re.search(r"\b(?:import\s*(?:\(|[\s{*])|require\s*\(|eval\s*\(|new\s+(?:Function|Worker|SharedWorker)\s*\()", source):
            fail(path, 0, "dynamic or external browser dependency forbidden")
        if re.search(r"(?:https?|wss?)://", source):
            fail(path, 0, "browser must not call an absolute external URL")
        if path.suffix == ".js":
            for match in re.finditer(r"\bapi\(\s*(['\"])(.*?)\1", source):
                if match.group(2) not in {"/api/snapshot", "/api/report", "/logout"}:
                    fail(path, 0, "browser API route not allowlisted")
    return {"ok": not errors, "errors": sorted(set(errors)), "files": sorted(imports), "imports": imports,
            "scope": "local Python dependency closure and dashboard assets; external packages and server ACLs require separate acceptance"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = verify(args.root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"ISOLATION_SOURCE_{'PASS' if result['ok'] else 'FAIL'}: {len(result['files'])} Python files")
        for error in result["errors"]:
            print(error)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
