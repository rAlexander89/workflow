"""Import rules for the layered architecture (see docs/architecture-plan.md).

Layers, inside src/wf:
  domain       imports only wf.domain and the stdlib; no I/O, clock, or environment
  application  imports wf.domain and wf.application; no I/O libraries
  adapters     imports application and domain, never entrypoints or bootstrap
  entrypoints  imports application, domain, and bootstrap, never adapters
  bootstrap.py the composition root: the one module that knows which adapter implements each port

Every module lives in a layer, or is bootstrap.py. The layers and the rules are the same in the tests below.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "wf"
LAYERS = ("domain", "application", "adapters", "entrypoints")

# Libraries that do I/O. The domain and application layers must not import them.
IO_MODULES = {"sqlite3", "subprocess", "socket", "http", "httpx", "requests", "fastapi", "starlette",
              "uvicorn", "jinja2", "langgraph", "shutil", "tempfile", "asyncio"}
# urllib.parse only splits strings, so it is allowed; the rest of urllib does network I/O.
IO_SUBMODULES = {"urllib.request", "urllib.error", "urllib.response"}
# Attribute uses that read the clock or the environment.
IMPURE_ATTRS = {("time", "time"), ("time", "monotonic"), ("time", "sleep"), ("os", "environ"), ("os", "getenv"),
                ("datetime", "now"), ("datetime", "utcnow")}

# What each layer may import from within wf.
ALLOWED_WF = {
    "domain": ("wf.domain",),
    "application": ("wf.domain", "wf.application"),
    "adapters": ("wf.domain", "wf.application", "wf.adapters"),
    "entrypoints": ("wf.domain", "wf.application", "wf.bootstrap", "wf.entrypoints"),
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
            if not _under(name, allowed):
                found.append(f"imports {name}, outside {', '.join(allowed)}")
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
    assert violations("adapters", "from wf import bootstrap\n")
    assert not violations("entrypoints", "from wf.application import use_cases\nfrom wf.bootstrap import Container\n")
    assert violations("entrypoints", "from wf.adapters.sqlite import Repo\n")
    assert violations("application", "from wf.bootstrap import Container\n")


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


def test_every_module_is_in_a_layer():
    outside = sorted(str(p.relative_to(SRC)) for p in _modules()
                     if _layer_of(p) is None and p.relative_to(SRC) not in (Path("__init__.py"), Path("bootstrap.py")))
    assert not outside, f"put code in domain/, application/, adapters/ or entrypoints/: {outside}"


def test_only_entrypoints_may_import_the_composition_root():
    # the layers' own rules already forbid it; this names the reason
    for path in _modules():
        layer = _layer_of(path)
        if layer in ("domain", "application", "adapters"):
            assert "wf.bootstrap" not in _imports(ast.parse(path.read_text())), str(path.relative_to(SRC))


@pytest.mark.parametrize("layer", LAYERS)
def test_each_layer_package_exists(layer):
    assert (SRC / layer / "__init__.py").exists()


# --- bounded contexts ---------------------------------------------------------------------------

CONTEXTS = ("board", "delivery", "execution", "tracker", "workspace")


def _context_of(path: Path) -> str | None:
    parts = path.relative_to(SRC).parts
    return parts[1] if parts[0] == "domain" and parts[1] in CONTEXTS else None


def test_domain_contexts_depend_only_on_the_shared_kernel():
    """Each bounded context stands alone: it may import wf.domain.shared and itself, never another context."""
    breaches = {}
    for path in _modules():
        ctx = _context_of(path)
        if ctx is None:
            continue
        for name in _imports(ast.parse(path.read_text())):
            if name.startswith("wf.domain.") and name.split(".")[2] not in (ctx, "shared"):
                breaches.setdefault(str(path.relative_to(SRC)), []).append(name)
    assert not breaches, breaches


def test_the_shared_kernel_imports_nothing_from_a_context():
    for path in _modules():
        if path.relative_to(SRC).parts[:2] == ("domain", "shared"):
            for name in _imports(ast.parse(path.read_text())):
                assert not (name.startswith("wf.domain.") and name.split(".")[2] != "shared"), (path, name)


def test_every_domain_package_is_a_known_context():
    packages = {p.name for p in (SRC / "domain").iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert packages == set(CONTEXTS) | {"shared"}


# --- MVVM ---------------------------------------------------------------------------------------

def test_web_routes_hold_no_logic_a_view_model_or_command_owns():
    """A route creates a view model, or calls a command, and renders. It does not open a board session or call a
    use case: that is what a view model is for."""
    routes = [SRC / "entrypoints/web/app.py", SRC / "entrypoints/web/board_views.py",
              SRC / "entrypoints/web/agents_views.py"]
    for path in routes:
        text = path.read_text()
        assert ".session()" not in text, f"{path.name} opens a board session; move that into a view model"
        assert "container.settings()" not in text, f"{path.name} reads settings; move that into a view model"


def test_templates_are_bound_to_view_models_not_built_in_python():
    for path in (SRC / "entrypoints/web").rglob("*.py"):
        text = path.read_text()
        for marker in ("<div", "<span", "<li>", "<ul>", "<b "):
            assert marker not in text, f"{path.name} builds HTML ({marker}); put it in a template"
