"""Import rules for the layered architecture (see docs/architecture-plan.md).

Layers, inside src/wf:
  domain       imports only wf.domain and the stdlib; no I/O, clock, or environment
  application  imports wf.domain and wf.application; no I/O libraries
  adapters     imports application and domain, never entrypoints
  entrypoints  imports application and domain, never adapters
  bootstrap.py (the composition root) may import anything

Every other module is "legacy": it has not been moved into a layer yet. The list of legacy
modules is tests/architecture_legacy.json. It may only shrink: a new module must go in a layer,
and a module that was moved or deleted must be removed from the list.
"""
import ast
import json
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "wf"
LEGACY_FILE = Path(__file__).with_name("architecture_legacy.json")
LAYERS = ("domain", "application", "adapters", "entrypoints")

# Libraries that do I/O. The domain and application layers must not import them.
IO_MODULES = {"sqlite3", "subprocess", "socket", "http", "httpx", "requests", "fastapi", "starlette",
              "uvicorn", "jinja2", "langgraph", "shutil", "tempfile", "asyncio"}
# urllib.parse only splits strings, so it is allowed; the rest of urllib does network I/O.
IO_SUBMODULES = {"urllib.request", "urllib.error", "urllib.response"}
# Attribute uses that read the clock or the environment.
IMPURE_ATTRS = {("time", "time"), ("time", "monotonic"), ("time", "sleep"), ("os", "environ"), ("os", "getenv"),
                ("datetime", "now"), ("datetime", "utcnow")}

# What each layer may import from within wf. None means unrestricted.
ALLOWED_WF = {
    "domain": ("wf.domain",),
    "application": ("wf.domain", "wf.application"),
    "adapters": None,
    "entrypoints": None,
}
FORBIDDEN_WF = {
    "adapters": ("wf.entrypoints",),
    "entrypoints": ("wf.adapters",),
}


def _imports(tree: ast.AST) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "wf":  # "from wf import board": the imported name is the module
                names += [f"wf.{a.name}" for a in node.names]
            else:
                names.append(node.module)
    return names


def _under(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def violations(layer: str, source: str) -> list[str]:
    """The rule breaches in one module's source, as short messages."""
    tree = ast.parse(source)
    found = []
    pure = layer in ("domain", "application")
    for name in _imports(tree):
        top = name.split(".")[0]
        if pure and (top in IO_MODULES or any(name == m or name.startswith(m + ".") for m in IO_SUBMODULES)):
            found.append(f"imports the I/O library {name}")
        if top == "wf":
            allowed = ALLOWED_WF[layer]
            if allowed is not None and not _under(name, allowed):
                found.append(f"imports {name}, outside {', '.join(allowed)}")
            if _under(name, FORBIDDEN_WF.get(layer, ())):
                found.append(f"imports {name}, which a {layer} module may not")
    if pure:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and (node.value.id, node.attr) in IMPURE_ATTRS):
                found.append(f"uses {node.value.id}.{node.attr}")
    return found


def _modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _layer_of(path: Path) -> str | None:
    first = path.relative_to(SRC).parts[0]
    return first if first in LAYERS else None


# --- the checker itself ----------------------------------------------------------------

def test_a_domain_module_may_not_import_io_or_adapters():
    assert violations("domain", "import sqlite3\n")
    assert violations("domain", "import urllib.request\n")
    assert not violations("domain", "from urllib.parse import urlsplit\n")
    assert violations("domain", "import subprocess as sp\n")
    assert violations("domain", "from wf.adapters import sqlite\n")
    assert violations("domain", "from wf import board\n")
    assert violations("domain", "from wf.application.ports import x\n")
    assert violations("domain", "import time\nx = time.time()\n")
    assert violations("domain", "import os\nx = os.environ['A']\n")
    assert not violations("domain", "from wf.domain.board import Ticket\nimport dataclasses\n")


def test_an_application_module_may_use_the_domain_and_its_ports_only():
    assert not violations("application", "from wf.domain.board import Ticket\nfrom wf.application.ports import x\n")
    assert violations("application", "from wf.adapters.sqlite import Repo\n")
    assert violations("application", "import httpx\n")


def test_adapters_and_entrypoints_keep_their_sides():
    assert not violations("adapters", "import sqlite3\nfrom wf.application.ports import x\n")
    assert violations("adapters", "from wf.entrypoints.web import app\n")
    assert not violations("entrypoints", "from wf.application import use_cases\n")
    assert violations("entrypoints", "from wf.adapters.sqlite import Repo\n")


# --- the code ---------------------------------------------------------------------------

def test_every_layered_module_follows_the_import_rules():
    breaches = {}
    for path in _modules():
        layer = _layer_of(path)
        if layer is None:
            continue
        found = violations(layer, path.read_text())
        if found:
            breaches[str(path.relative_to(SRC))] = found
    assert not breaches, breaches


def _legacy_now() -> set[str]:
    return {str(p.relative_to(SRC)) for p in _modules()
            if _layer_of(p) is None and p.relative_to(SRC) not in (Path("__init__.py"), Path("bootstrap.py"))}


def test_no_new_module_is_added_outside_a_layer():
    allowed = set(json.loads(LEGACY_FILE.read_text()))
    new = _legacy_now() - allowed
    assert not new, f"put new code in domain/, application/, adapters/ or entrypoints/: {sorted(new)}"


def test_the_legacy_list_has_no_stale_entries():
    allowed = set(json.loads(LEGACY_FILE.read_text()))
    stale = allowed - _legacy_now()
    assert not stale, f"remove from tests/architecture_legacy.json: {sorted(stale)}"


@pytest.mark.parametrize("layer", LAYERS)
def test_each_layer_package_exists(layer):
    assert (SRC / layer / "__init__.py").exists()
