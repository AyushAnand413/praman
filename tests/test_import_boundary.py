"""The architectural invariant, enforced.

`vyapaari/` may not reach `kernel.payments`. The LLM layer must be structurally
incapable of touching money — not merely trusted not to.

This walks the AST of every first-party module, builds the import graph, and
fails if `kernel.payments` is reachable from anything under `vyapaari/` by ANY
path, not just a direct import. "Reachable" is the operative word, and a
two-hop leak (`vyapaari → some_helper → kernel.payments`) is exactly as bad as
a one-hop one.

The test is intentionally green on an empty `vyapaari/` package — it is a
tripwire installed up front, so that the moment the Gemini client lands the
boundary is already being checked.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_TARGET = "kernel.payments"
GUARDED_PACKAGE = "vyapaari"

FIRST_PARTY_PACKAGES = ("kernel", "vyapaari", "api", "store", "mandate", "harness")
FIRST_PARTY_MODULES = ("settings",)

#: Credential env var names. The rule: vyapaari holds no credentials.
CREDENTIAL_NAMES = ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET")


def _module_name(path: Path) -> str:
    relative = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _first_party_files() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for package in FIRST_PARTY_PACKAGES:
        for path in (REPO_ROOT / package).rglob("*.py"):
            modules[_module_name(path)] = path
    for name in FIRST_PARTY_MODULES:
        path = REPO_ROOT / f"{name}.py"
        if path.exists():
            modules[name] = path
    return modules


def _is_first_party(name: str) -> bool:
    root = name.split(".")[0]
    return root in FIRST_PARTY_PACKAGES or root in FIRST_PARTY_MODULES


def _imports_of(module: str, path: Path) -> set[str]:
    """First-party modules imported by `module`, as dotted names."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module.rsplit(".", 1)[0] if "." in module else ""
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_first_party(alias.name):
                    found.add(alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: from . import x / from .mod import y
                base = package
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                origin = f"{base}.{node.module}" if node.module else base
            else:
                origin = node.module or ""
            if not origin or not _is_first_party(origin):
                continue
            found.add(origin)
            # `from kernel import payments` names a MODULE, not an attribute.
            for alias in node.names:
                found.add(f"{origin}.{alias.name}")

    return found


def _build_graph() -> dict[str, set[str]]:
    files = _first_party_files()
    known = set(files)
    graph: dict[str, set[str]] = {}
    for module, path in files.items():
        # Keep only edges to modules that actually exist, so
        # `from kernel import payments` resolves to kernel.payments while
        # `from settings import secret` (an attribute) is dropped.
        graph[module] = {dep for dep in _imports_of(module, path) if dep in known}
    return graph


def _paths_to_target(graph: dict[str, set[str]], start: str, target: str) -> list[str] | None:
    """BFS. Returns the import chain that reaches `target`, or None."""
    queue: deque[list[str]] = deque([[start]])
    seen = {start}
    while queue:
        chain = queue.popleft()
        for dep in sorted(graph.get(chain[-1], set())):
            if dep == target:
                return chain + [dep]
            if dep not in seen:
                seen.add(dep)
                queue.append(chain + [dep])
    return None


def test_kernel_payments_module_exists():
    """Guard against the test silently passing because the target moved."""
    assert (REPO_ROOT / "kernel" / "payments.py").exists(), (
        "kernel/payments.py is missing — this test would pass vacuously"
    )


def test_vyapaari_cannot_reach_kernel_payments():
    graph = _build_graph()
    guarded = [m for m in graph if m == GUARDED_PACKAGE or m.startswith(f"{GUARDED_PACKAGE}.")]
    assert guarded, "no vyapaari modules found — the walker is misconfigured"

    violations = []
    for module in sorted(guarded):
        chain = _paths_to_target(graph, module, FORBIDDEN_TARGET)
        if chain:
            violations.append(" → ".join(chain))

    assert not violations, (
        "boundary violated — kernel.payments is reachable from vyapaari:\n  "
        + "\n  ".join(violations)
    )


def test_vyapaari_holds_no_payment_credentials():
    """The LLM layer has no credentials, not even indirectly named."""
    offenders = []
    for path in (REPO_ROOT / GUARDED_PACKAGE).rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for name in CREDENTIAL_NAMES:
            if name in source:
                offenders.append(f"{path.relative_to(REPO_ROOT)} references {name}")
    assert not offenders, "boundary violated:\n  " + "\n  ".join(offenders)


def test_vyapaari_does_not_reference_payments_by_attribute():
    """Catches `import kernel` followed by `kernel.payments.…`."""
    offenders = []
    for path in (REPO_ROOT / GUARDED_PACKAGE).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "payments"
                and isinstance(node.value, ast.Name)
                and node.value.id == "kernel"
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, (
        "boundary violated — kernel.payments accessed at " + ", ".join(offenders)
    )
