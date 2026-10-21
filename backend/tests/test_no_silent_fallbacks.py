"""Spec P2 lint-as-test: no `except` handler in engines/** or routes.py may silently swallow an error."""

from __future__ import annotations

import ast
from pathlib import Path
import tempfile

_BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
_SCAN_TARGETS = [
    *sorted((_BACKEND_APP / "engines").rglob("*.py")),
    _BACKEND_APP / "api" / "routes.py",
    _BACKEND_APP / "evaluation" / "evaluator.py",
    _BACKEND_APP / "evaluation" / "judges.py",
    _BACKEND_APP / "ingestion" / "jobs.py",
]


def _is_silent_statement(stmt: ast.stmt) -> bool:
    if isinstance(stmt, (ast.Pass, ast.Continue)):
        return True
    if isinstance(stmt, ast.Return):
        return stmt.value is None or (isinstance(stmt.value, ast.Constant) and stmt.value.value is None)
    return False


def _silent_handlers(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if body and all(_is_silent_statement(s) for s in body):
            findings.append(f"{path.name}:{node.lineno}")
    return findings


def test_scan_targets_exist_and_include_parked_engines() -> None:
    assert any(p.name == "level1_naive.py" for p in _SCAN_TARGETS)
    assert any("parked" in p.parts for p in _SCAN_TARGETS)
    assert all(p.is_file() for p in _SCAN_TARGETS)


def test_no_except_handler_is_only_pass_return_none_or_continue() -> None:
    findings = [f for path in _SCAN_TARGETS for f in _silent_handlers(path)]
    assert findings == [], f"Silent except handlers found (spec P2): {findings}"


def test_detector_flags_known_silent_patterns() -> None:
    sample = Path(tempfile.mkdtemp(prefix="rag_arena_ast_")) / "sample.py"
    sample.write_text(
        "def f():\n"
        "    try:\n        pass\n    except Exception:\n        pass\n"
        "    try:\n        pass\n    except ValueError:\n        return None\n"
        "    for _ in range(1):\n        try:\n            pass\n        except KeyError:\n            continue\n"
        "    try:\n        pass\n    except OSError as exc:\n        raise RuntimeError('x') from exc\n",
        encoding="utf-8",
    )
    found = _silent_handlers(sample)
    assert len(found) == 3
