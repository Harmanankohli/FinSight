"""Hardened Python sandbox for the execute_python MCP tool.

Three-layer security model:
  1. AST-level static analysis (_check_code_safety) — rejects restricted
     imports, builtin calls, and dunder attribute chains before execution.
  2. Subprocess isolation — user code runs in a separate process so any crash,
     memory error, or segfault cannot affect the parent server.
  3. OS-level resource limits (_sandbox_preexec, Unix only) — CPU time (25s),
     address space (512 MB), and file descriptor caps applied in the child.

Mode (SANDBOX_MODE):
  - ``ast`` (default): AST gate + subprocess + resource limits (Unix only)
  - ``container``: Docker container with strict isolation
  - ``disabled``: returns error immediately, code never runs
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

from shared.auth.audit import log_sandbox_execution

logger = logging.getLogger(__name__)


# ── Identifier restriction sets ───────────────────────────────────────────────

# Modules providing filesystem, network, process, or reflection capabilities.
# Keeping this as a frozenset means membership checks are O(1).
_RESTRICTED_IMPORTS: frozenset[str] = frozenset(
    [
        # OS / process control
        "os",
        "subprocess",
        "shutil",
        "shlex",
        "signal",
        "threading",
        "multiprocessing",
        "concurrent",
        "pty",
        "tty",
        "termios",
        "fcntl",
        "mmap",
        "resource",
        "pwd",
        "grp",
        "crypt",
        # Networking
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "ftplib",
        "poplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "socketserver",
        # Filesystem
        "pathlib",
        "io",
        "glob",
        "fnmatch",
        "tempfile",
        "zipfile",
        "tarfile",
        "gzip",
        "bz2",
        "lzma",
        # Reflection / introspection
        "ctypes",
        "importlib",
        "pickle",
        "inspect",
        "sys",
        "builtins",
        "gc",
        "weakref",
        "atexit",
        # Encoding tricks
        "base64",
        "codecs",
    ]
)

# Builtin-level functions that bypass the AST sandbox or leak the environment.
_RESTRICTED_CALLS: frozenset[str] = frozenset(
    [
        "exec",
        "eval",
        "open",
        "__import__",
        "compile",
        "globals",
        "locals",
        "vars",
        "dir",
        "delattr",
        "setattr",
    ]
)

# Dunder attribute chains used in well-known class-walk sandbox escapes.
_RESTRICTED_ATTRS: frozenset[str] = frozenset(
    [
        "system",
        "popen",
        "execv",
        "execve",
        "execl",
        "execvp",
        "spawn",
        "spawnl",
        "fork",
        "forkpty",
        "exec",
        "eval",
        "__class__",
        "__bases__",
        "__subclasses__",
        "__mro__",
        "__globals__",
        "__builtins__",
        "__code__",
        "__closure__",
        "__wrapped__",
    ]
)

# ── Mode resolution ───────────────────────────────────────────────────────────


def _get_sandbox_mode() -> str:
    """Read SANDBOX_MODE from settings, lazily, to avoid import-time issues."""
    from shared.settings import get_settings

    return get_settings().sandbox_mode


def _docker_available() -> bool:
    """Check whether the docker CLI is on PATH."""
    return shutil.which("docker") is not None


async def _run_container(code: str, timeout: int = 30) -> dict:
    """Run *code* in a Docker container with strict resource isolation.

    Container spec:
      - ``--network none`` — no network egress
      - ``--read-only`` + ``--tmpfs /tmp:size=64m`` — limited writable scratch
      - ``--memory 512m --cpus 1`` — resource caps
      - ``--user 65534:65534`` — nobody:nogroup, no file ownership
      - ``python:3.12-slim python -I -S -`` — isolated interpreter, code on stdin

    If Docker is not available at runtime, falls back to ``ast`` mode with a
    warning and re-dispatches to the AST sandbox.
    """
    if not _docker_available():
        logger.warning("Docker not available for container mode — falling back to AST sandbox")
        return await run_sandbox(code, timeout, principal="mcp-server")

    tag = uuid.uuid4().hex[:12]
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--read-only",
        "--tmpfs",
        "/tmp:size=64m",
        "--user",
        "65534:65534",
        "--label",
        f"finsight-sandbox={tag}",
        "python:3.12-slim",
        "python",
        "-I",
        "-S",
        "-",
    ]

    loop = asyncio.get_event_loop()

    try:
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                docker_cmd,
                input=code,
                capture_output=True,
                text=True,
                timeout=timeout,
            ),
        )
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(
                f"docker kill $(docker ps -q --filter label=finsight-sandbox={tag})",
                shell=True,
                capture_output=True,
                timeout=5,
            )
        except Exception:
            logger.debug("Docker kill fallback failed (non-fatal)")
        logger.warning("Container sandbox timed out after %ds", timeout)
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Timed out after {timeout}s",
            "result": None,
        }
    except Exception as exc:
        logger.warning("Container sandbox subprocess failed: %s", exc)
        return {"success": False, "stdout": "", "stderr": str(exc), "result": None}

    return _parse_sandbox_output(proc)


# ── Static AST safety check ───────────────────────────────────────────────────


def _check_code_safety(code: str) -> tuple[bool, str]:
    """Parse and walk the AST to detect forbidden constructs.

    Returns (True, "") when code is safe, (False, reason) when blocked.
    This is a static gate — it runs before any subprocess is spawned.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"Syntax error: {exc}"

    for node in ast.walk(tree):
        # ── Import blocking ────────────────────────────────────────────────
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _RESTRICTED_IMPORTS:
                    return False, f"Restricted import: {alias.name}"

        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _RESTRICTED_IMPORTS:
                return False, f"Restricted import from: {node.module}"

        # ── Call-level blocking ────────────────────────────────────────────
        if isinstance(node, ast.Call):
            fn = node.func

            # Direct builtin call: exec(...), eval(...), open(...)
            if isinstance(fn, ast.Name) and fn.id in _RESTRICTED_CALLS:
                return False, f"Restricted function: {fn.id}"

            # Method call on restricted attr: obj.__subclasses__()
            if isinstance(fn, ast.Attribute) and fn.attr in _RESTRICTED_ATTRS:
                return False, f"Restricted attribute: {fn.attr}"

            # getattr(obj, '__dunder__') — classic sandbox escape
            if isinstance(fn, ast.Name) and fn.id == "getattr":
                if len(node.args) >= 2:
                    attr_arg = node.args[1]
                    if (
                        isinstance(attr_arg, ast.Constant)
                        and isinstance(attr_arg.value, str)
                        and (
                            attr_arg.value in _RESTRICTED_ATTRS
                            or (attr_arg.value.startswith("__") and attr_arg.value.endswith("__"))
                        )
                    ):
                        return False, f"Restricted getattr: {attr_arg.value}"

        # ── Attribute access blocking ──────────────────────────────────────
        # Catches both reads (x.__class__) and attribute-chains
        if isinstance(node, ast.Attribute) and node.attr in _RESTRICTED_ATTRS:
            return False, f"Restricted attribute access: {node.attr}"

        # ── Subscript blocking ─────────────────────────────────────────────
        # Catches obj['__class__'] and similar dict-key escape patterns
        if isinstance(node, ast.Subscript):
            slice_node = node.slice
            if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
                val = slice_node.value
                if val in _RESTRICTED_ATTRS or (val.startswith("__") and val.endswith("__")):
                    return False, f"Restricted subscript key: {val}"

    return True, ""


# ── Subprocess runner ─────────────────────────────────────────────────────────

# Injected verbatim into the child process. __builtins__ is replaced with a
# whitelist so even if the AST gate misses something, the subprocess can't
# call open(), exec(), or use getattr tricks at runtime.
_SANDBOX_RUNNER = """\
import sys, json, math, statistics, itertools, collections, functools, \
       typing, datetime, random

_SAFE_BUILTINS = {
    "print": print, "len": len, "range": range,
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "frozenset": frozenset, "bytes": bytes,
    "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
    "any": any, "all": all, "sum": sum, "min": min, "max": max,
    "abs": abs, "round": round, "sorted": sorted, "reversed": reversed,
    "repr": repr, "format": format, "hash": hash, "id": id,
    "isinstance": isinstance,
    "True": True, "False": False, "None": None,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "StopIteration": StopIteration,
    "NotImplementedError": NotImplementedError,
}

_GLOBALS = {
    "__builtins__": _SAFE_BUILTINS,
    "math": math, "json": json, "datetime": datetime,
    "random": random, "statistics": statistics,
    "itertools": itertools, "collections": collections,
    "functools": functools, "typing": typing,
}
try:
    _GLOBALS["pd"] = __import__("pandas")
except ImportError:
    pass
try:
    _GLOBALS["np"] = __import__("numpy")
except ImportError:
    pass

code = sys.stdin.read()
_locals = {}
try:
    exec(code, _GLOBALS, _locals)
    result = _locals.get("result")
    print("__RESULT__:" + json.dumps({
        "type": type(result).__name__ if result is not None else "NoneType",
        "value": repr(result)[:2000],
    }))
except Exception:
    import traceback
    print("__ERROR__:" + traceback.format_exc(), file=sys.stderr)
"""


def _sandbox_preexec() -> None:
    """OS resource limits applied in the child before exec (Unix only).

    25s CPU cap kills infinite loops. 512 MB AS cap kills memory bombs.
    0 open FDs blocks filesystem writes even if the AST gate is bypassed.
    """
    try:
        import resource as _res

        _res.setrlimit(_res.RLIMIT_CPU, (25, 25))
        _res.setrlimit(_res.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        _res.setrlimit(_res.RLIMIT_NOFILE, (0, 0))
    except Exception:
        logger.debug("Resource limits unavailable (expected on Windows)")


async def run_sandbox(code: str, timeout: int = 30, principal: str = "mcp-server") -> dict:
    """Run *code* through the configured sandbox mode.

    Mode is read from ``SANDBOX_MODE`` at call time:
      - ``disabled`` — code never runs, returns error dict
      - ``ast`` (default) — AST gate + subprocess + resource limits (Unix only)
      - ``container`` — Docker container with strict isolation

    Audit-logs every invocation with principal, sha256(code), duration, exit status.

    Args:
        code:      Python code string to execute.
        timeout:   Wall-clock timeout in seconds (default 30).
        principal: Principal identifier for audit logging (default ``mcp-server``).

    Returns:
        dict with keys: success, stdout, stderr, result ({type, value})
    """
    mode = _get_sandbox_mode()

    if mode == "disabled":
        logger.info("Sandbox disabled — blocking code execution")
        return {
            "success": False,
            "stdout": "",
            "stderr": "execute_python disabled by SANDBOX_MODE",
            "result": None,
        }

    t0 = asyncio.get_event_loop().time()

    if mode == "container":
        result = await _run_container(code, timeout)
    else:
        result = await _run_ast(code, timeout)

    duration = asyncio.get_event_loop().time() - t0

    # Audit log
    code_sha = hashlib.sha256(code.encode()).hexdigest()
    exit_status = 0 if result.get("success") else 1
    log_sandbox_execution(principal, code_sha, duration, exit_status)

    return result


async def _run_ast(code: str, timeout: int = 30) -> dict:
    """Run *code* through the AST gate + subprocess sandbox (default mode)."""
    safe, reason = _check_code_safety(code)
    if not safe:
        logger.warning("Sandbox blocked code: %s", reason)
        return {"success": False, "stdout": "", "stderr": reason, "result": None}

    logger.info("Sandbox executing %d chars of code (timeout=%ds)", len(code), timeout)

    runner_fd, runner_path = tempfile.mkstemp(suffix=".py", text=True)
    try:
        with os.fdopen(runner_fd, "w", encoding="utf-8") as f:
            f.write(_SANDBOX_RUNNER)

        preexec = _sandbox_preexec if sys.platform != "win32" else None

        proc = subprocess.run(
            [sys.executable, "-I", "-S", runner_path],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=preexec,
        )
        return _parse_sandbox_output(proc)
    except subprocess.TimeoutExpired:
        logger.warning("Sandbox timed out after %ds", timeout)
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Timed out after {timeout}s",
            "result": None,
        }
    except Exception as exc:
        logger.warning("Sandbox subprocess failed: %s", exc)
        return {"success": False, "stdout": "", "stderr": str(exc), "result": None}
    finally:
        try:
            os.unlink(runner_path)
        except OSError:
            logger.debug("Could not unlink temp runner %s", runner_path)


def _parse_sandbox_output(proc: subprocess.CompletedProcess) -> dict:
    """Parse stdout/stderr from a sandbox subprocess into a result dict."""
    result = None
    clean_lines: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__:"):
            try:
                result = json.loads(line[len("__RESULT__:") :])
            except json.JSONDecodeError:
                result = {"raw": line[len("__RESULT__:") :]}
        else:
            clean_lines.append(line)

    cleaned_stderr = "\n".join(
        line[len("__ERROR__:") :] if line.startswith("__ERROR__:") else line
        for line in proc.stderr.splitlines()
    )

    success = proc.returncode == 0
    logger.info("Sandbox completed (success=%s, returncode=%d)", success, proc.returncode)
    return {
        "success": success,
        "stdout": "\n".join(clean_lines),
        "stderr": cleaned_stderr,
        "result": result,
    }
