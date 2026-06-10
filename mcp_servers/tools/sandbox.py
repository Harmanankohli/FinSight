"""Python sandbox MCP tool: execute_python."""

from __future__ import annotations

import logging

from langfuse import observe

from mcp_servers._app import app
from shared.sandbox import run_sandbox as _run_sandbox

logger = logging.getLogger(__name__)


@app.tool()
@observe()
async def execute_python(code: str, timeout: int = 30) -> dict:
    """Execute Python code in a hardened sandbox subprocess.

    Available libraries: pandas (pd), numpy (np), math, json, datetime,
    random, statistics, itertools, collections, functools, typing.
    NOT available: os, sys, subprocess, open, exec, eval, dunder tricks.
    Set ``result = <value>`` to return data.

    Resource limits (Unix): 25 s CPU, 512 MB RAM, 0 open file descriptors.

    Args:
        code:    Python code string to execute.
        timeout: Wall-clock timeout in seconds (default 30).

    Returns:
        dict with keys: success, stdout, stderr, result ({type, value})
    """
    return await _run_sandbox(code, timeout, principal="mcp-server")
