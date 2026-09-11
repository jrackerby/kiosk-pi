"""The agent imports nothing outside the standard library. Enforced, not assumed.

WHY THIS IS A TEST AND NOT A README LINE. A dependency added here does not fail
in CI — CI has pip and a network. It fails on a Raspberry Pi during an upgrade,
on a wall with no console attached, at which point the recovery is SSH and the
symptom is a black screen. The declaration that this package is
standard-library-only is only worth anything if something enforces it, and the
enforcement has to be cheap enough to run on every commit.

STATIC, BY AST, ACROSS EVERY MODULE. Not by importing them and seeing what
happens: an import-time check passes on a runner that happens to have the
package installed, which is the one machine whose opinion does not matter.
"""

from __future__ import annotations

import ast
import pathlib
import sys

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "pikioskd"


def imported_top_level_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import: our own package, by definition.
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def test_every_module_is_covered_by_this_sweep():
    """A sweep that does not assert its own completeness is not evidence.

    Counting successful reads, never attempts: a check on attempts printed
    complete over files it never opened.
    """
    modules = sorted(PACKAGE.glob("*.py"))
    assert modules, f"no modules found under {PACKAGE}"
    read = 0
    for module in modules:
        imported_top_level_modules(module)
        read += 1
    assert read == len(modules)


def test_no_third_party_imports():
    offenders: dict[str, set[str]] = {}
    for module in sorted(PACKAGE.glob("*.py")):
        third_party = {
            name for name in imported_top_level_modules(module)
            if name not in sys.stdlib_module_names
        }
        if third_party:
            offenders[module.name] = third_party
    assert not offenders, (
        "pikioskd must import only the standard library, so that installing it "
        f"on a Pi is a file copy. Third-party imports found: {offenders}"
    )


def test_the_check_can_fail(tmp_path):
    """Prove the sweep detects what it claims to detect."""
    module = tmp_path / "bad.py"
    module.write_text("import requests\nfrom aiohttp import ClientSession\n",
                      encoding="utf-8")
    found = imported_top_level_modules(module)
    assert {"requests", "aiohttp"} <= found
    assert not {"requests", "aiohttp"} & sys.stdlib_module_names


def test_relative_imports_are_not_counted_as_dependencies(tmp_path):
    module = tmp_path / "rel.py"
    module.write_text("from . import device\nfrom .cdp import Browser\n",
                      encoding="utf-8")
    assert imported_top_level_modules(module) == set()
