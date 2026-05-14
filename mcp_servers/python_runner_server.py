import ast
import json
import logging
import subprocess
import sys
import tempfile
import uuid

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)
app = FastMCP("python-runner-mcp")

_RESTRICTED_IMPORTS = [
    "os", "subprocess", "shutil", "socket", "ctypes",
    "importlib", "pickle", "inspect", "sys",
]


def _build_script(code: str) -> str:
    return (
        'import json, sys, math, statistics, itertools, collections, functools, typing, datetime, random\n'
        '_sb = {\n'
        '    "print": print, "len": len, "range": range, "int": int, "float": float,\n'
        '    "str": str, "bool": bool, "list": list, "dict": dict, "tuple": tuple,\n'
        '    "set": set, "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,\n'
        '    "any": any, "all": all, "sum": sum, "min": min, "max": max, "abs": abs,\n'
        '    "round": round, "sorted": sorted, "reversed": reversed,\n'
        '    "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr, "type": type,\n'
        '    "True": True, "False": False, "None": None, "Exception": Exception,\n'
        '    "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,\n'
        '}\n'
        '_sg = {\n'
        '    "__builtins__": _sb,\n'
        '    "pd": __import__("pandas"),\n'
        '    "np": __import__("numpy"),\n'
        '    "math": math, "json": json, "datetime": datetime, "random": random,\n'
        '    "statistics": statistics, "itertools": itertools, "collections": collections,\n'
        '    "functools": functools, "typing": typing,\n'
        '}\n'
        'try:\n'
        '    _locals = {}\n'
        f'    exec({json.dumps(code)}, _sg, _locals)\n'
        '    _result = _locals.get("result")\n'
        '    print("__RESULT__:" + json.dumps({\n'
        '        "type": type(_result).__name__ if _result is not None else "NoneType",\n'
        '        "value": repr(_result)[:1000]\n'
        '    }))\n'
        'except Exception:\n'
        '    import traceback\n'
        '    print("__ERROR__:" + traceback.format_exc(), file=sys.stderr)\n'
    )


def _check_code_safety(code: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _RESTRICTED_IMPORTS:
                    return False, f"Restricted import: {alias.name}"
        if isinstance(node, ast.ImportFrom):
            root = node.module.split(".")[0] if node.module else ""
            if root in _RESTRICTED_IMPORTS:
                return False, f"Restricted import: {node.module}"
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in {"exec", "eval", "open", "__import__"}:
                return False, f"Restricted function: {fn.id}"
            if isinstance(fn, ast.Attribute) and fn.attr in {"system", "popen", "exec", "eval"}:
                return False, f"Restricted method: {fn.attr}"
    return True, ""


@app.tool()
async def execute_python(code: str, timeout: int = 30) -> dict:
    """Execute Python code in a sandboxed subprocess with restricted imports.

    Runs the provided Python code in an isolated subprocess with limited builtins.
    Only safe libraries (pandas, numpy, math, json, etc.) are available.
    Restricted imports: os, subprocess, shutil, socket, ctypes, importlib, pickle, inspect, sys.
    The code can return a value by assigning to a `result` variable.

    Args:
        code: Python code string to execute. Use `result = <value>` to return data.
        timeout: Maximum execution time in seconds (default 30)

    Returns:
        dict with keys: success (bool), stdout (str - captured print output), stderr (str - error messages), result (dict with type and value of the result variable, or None)
    """
    safe, reason = _check_code_safety(code)
    if not safe:
        return {"success": False, "stdout": "", "stderr": reason, "result": None}

    script = _build_script(code)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        script_path = f.name

    try:
        proc = subprocess.run(
            [sys.executable, "-I", script_path],
            capture_output=True, text=True, timeout=timeout,
        )

        result = None
        clean_stdout = []
        for line in proc.stdout.splitlines():
            if line.startswith("__RESULT__:"):
                try:
                    result = json.loads(line[len("__RESULT__:"):])
                except json.JSONDecodeError:
                    result = {"raw": line[len("__RESULT__:"):]}
            else:
                clean_stdout.append(line)

        error_detail = proc.stderr
        for line in proc.stderr.splitlines():
            if line.startswith("__ERROR__:"):
                error_detail = line[len("__ERROR__:"):]

        return {
            "success": proc.returncode == 0,
            "stdout": "\n".join(clean_stdout),
            "stderr": error_detail,
            "result": result,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False, "stdout": "", "stderr": f"Timed out after {timeout}s", "result": None,
        }
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e), "result": None}
    finally:
        import os as _os
        try:
            _os.unlink(script_path)
        except Exception:
            pass


_starlette_app = None


def get_app():
    global _starlette_app
    if _starlette_app is None:
        _starlette_app = app.sse_app()
    return _starlette_app


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(get_app(), host="0.0.0.0", port=8040)
