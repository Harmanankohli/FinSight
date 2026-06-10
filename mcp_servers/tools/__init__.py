# Import all tool modules to trigger @app.tool() decorator registration.
# Order matters: edgar must come before ticker/sentiment (they depend on _edgar singleton).

from mcp_servers.tools import (
    agent_registry,  # noqa: F401
    edgar,  # noqa: F401
    market_data,  # noqa: F401
    sandbox,  # noqa: F401
    sentiment,  # noqa: F401
    ticker,  # noqa: F401
)
