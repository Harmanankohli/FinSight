"""FastMCP application singleton.

All tool modules import ``app`` from here so that ``@app.tool()`` decorators
register on the same instance.  This module has no other imports — keep it
as slim as possible to avoid circular imports during package initialisation.
"""

import logging

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

app = FastMCP("finsight-mcp")
logger.info("FastMCP app created: %s", app.name)
