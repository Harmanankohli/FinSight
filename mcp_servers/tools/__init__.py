# Import all tool modules to trigger @app.tool() decorator registration.
# Order matters: edgar must come before ticker/sentiment (they depend on _edgar singleton).

from mcp_servers.tools import agent_registry  # noqa: F401
from mcp_servers.tools import market_data     # noqa: F401
from mcp_servers.tools import edgar           # noqa: F401
from mcp_servers.tools import ticker          # noqa: F401
from mcp_servers.tools import sentiment       # noqa: F401
from mcp_servers.tools import sandbox         # noqa: F401
