"""Security tests for the hardened Python sandbox.

Unit tests verify the AST-level _check_code_safety gate directly — no
subprocess is spawned so they run fast and work offline.

Integration tests (marked @pytest.mark.integration) spawn the actual
subprocess and require the full Python environment.
"""

import pytest

from shared.sandbox import _check_code_safety

# ── Negative cases — every listed pattern MUST be blocked ────────────────────


@pytest.mark.parametrize(
    "code,expected_fragment",
    [
        # Import blocking
        ("import os", "Restricted import"),
        ("import subprocess", "Restricted import"),
        ("import socket", "Restricted import"),
        ("import pathlib", "Restricted import"),
        ("import io", "Restricted import"),
        ("import urllib", "Restricted import"),
        ("import http", "Restricted import"),
        ("import requests", "Restricted import"),
        ("import base64", "Restricted import"),
        ("from os import path", "Restricted import"),
        ("from os.path import join", "Restricted import"),
        ("from sys import argv", "Restricted import"),
        ("from urllib.request import urlopen", "Restricted import"),
        ("from pathlib import Path", "Restricted import"),
        ("from io import StringIO", "Restricted import"),
        # Builtin function blocking
        ("__import__('os')", "Restricted function"),
        ("open('/etc/passwd')", "Restricted function"),
        ("eval('1+1')", "Restricted function"),
        ("exec('print(1)')", "Restricted function"),
        ("compile('', '', 'exec')", "Restricted function"),
        ("globals()", "Restricted function"),
        ("locals()", "Restricted function"),
        ("vars()", "Restricted function"),
        ("dir(x)", "Restricted function"),
        ("setattr(x, 'a', 1)", "Restricted function"),
        ("delattr(x, 'a')", "Restricted function"),
        # Dunder attribute access (class-walking escape)
        ("x.__class__", "Restricted attribute"),
        ("x.__bases__", "Restricted attribute"),
        ("x.__mro__", "Restricted attribute"),
        ("x.__globals__", "Restricted attribute"),
        ("x.__builtins__", "Restricted attribute"),
        ("x.__code__", "Restricted attribute"),
        ("().__class__.__bases__[0].__subclasses__()", "Restricted attribute"),
        # getattr with dunder string
        ("getattr(x, '__class__')", "Restricted getattr"),
        ("getattr(x, '__builtins__')", "Restricted getattr"),
        ("getattr(x, '__import__')", "Restricted getattr"),
        ("getattr(x, '__reduce__')", "Restricted getattr"),
        # Subscript with dunder key
        ("x['__class__']", "Restricted subscript key"),
        ("x['__builtins__']", "Restricted subscript key"),
        ("x['__globals__']", "Restricted subscript key"),
        # OS method calls through an attribute chain
        ("os.system('ls')", "Restricted attribute"),
        ("os.popen('ls')", "Restricted attribute"),
    ],
)
def test_blocked(code, expected_fragment):
    safe, reason = _check_code_safety(code)
    assert not safe, f"Expected {code!r} to be blocked, but was allowed"
    assert expected_fragment.lower() in reason.lower(), (
        f"Wrong block reason for {code!r}: expected fragment {expected_fragment!r}, got {reason!r}"
    )


# ── Positive cases — must NOT be blocked ─────────────────────────────────────


@pytest.mark.parametrize(
    "code",
    [
        "result = 2 + 2",
        "result = [x * x for x in range(10)]",
        "result = math.sqrt(4.0)",
        "result = sum([1, 2, 3])",
        "result = {'a': 1}['a']",  # normal dict subscript
        "result = sorted([3, 1, 2])",
        "result = str(42)",
        "result = round(3.14159, 2)",
        "import math\nresult = math.pi",  # math is not restricted
        "import json\nresult = json.dumps({'x': 1})",  # json is not restricted
        "import statistics\nresult = statistics.mean([1, 2, 3])",
        "result = list(range(5))",
        "result = isinstance(42, int)",
    ],
)
def test_allowed(code):
    safe, reason = _check_code_safety(code)
    assert safe, f"Expected {code!r} to be allowed, but was blocked: {reason!r}"


# ── Syntax error ──────────────────────────────────────────────────────────────


def test_syntax_error_rejected():
    safe, reason = _check_code_safety("def (")
    assert not safe
    assert "syntax error" in reason.lower()


def test_empty_code_allowed():
    safe, _ = _check_code_safety("")
    assert safe


# ── Specific hardening additions (pathlib, io, urllib) ───────────────────────


def test_pathlib_blocked():
    safe, reason = _check_code_safety("from pathlib import Path\nPath('/etc').read_text()")
    assert not safe
    assert "restricted import" in reason.lower()


def test_io_stringio_blocked():
    safe, reason = _check_code_safety("import io\nio.StringIO('data')")
    assert not safe
    assert "restricted import" in reason.lower()


def test_urllib_urlopen_blocked():
    safe, reason = _check_code_safety("import urllib.request\nurllib.request.urlopen('http://x')")
    assert not safe
    assert "restricted import" in reason.lower()


# ── Integration: full subprocess execution ────────────────────────────────────


@pytest.mark.integration
async def test_exec_simple_arithmetic():
    from shared.sandbox import run_sandbox

    out = await run_sandbox("result = 2 + 2")
    assert out["success"], f"stderr: {out['stderr']}"
    assert out["result"]["value"] == "4"


@pytest.mark.integration
async def test_exec_list_comprehension():
    from shared.sandbox import run_sandbox

    out = await run_sandbox("result = [x*x for x in range(5)]")
    assert out["success"], f"stderr: {out['stderr']}"
    assert "25" in out["result"]["value"]


@pytest.mark.integration
async def test_exec_import_os_blocked_at_runtime():
    from shared.sandbox import run_sandbox

    out = await run_sandbox("import os")
    assert not out["success"]


@pytest.mark.integration
async def test_exec_timeout():
    from shared.sandbox import run_sandbox

    out = await run_sandbox("while True: pass", timeout=2)
    assert not out["success"]
    assert "timed out" in out["stderr"].lower()
