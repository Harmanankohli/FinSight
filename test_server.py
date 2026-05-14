"""Quick smoke test for the unified finsight MCP server."""
import json
import logging

logging.basicConfig(level=logging.INFO)

from mcp_servers.finsight_server import app, _df_registry, find_agent

print("=== Agent Registry ===")
print(f"Cards loaded: {len(_df_registry)}")
cards = list(_df_registry["agent_card"])
for c in cards:
    print(f"  - {c['name']} ({c['url']})")

print()
print("=== find_agent test ===")
result = find_agent("I need to analyze stock risk metrics like Sharpe ratio and beta")
card = json.loads(result)
print(f"Best match: {card['name']}")

result2 = find_agent("What is the market sentiment for a stock?")
card2 = json.loads(result2)
print(f"Best match: {card2['name']}")

print()
print("=== Available Tools ===")
try:
    tools = app._tool_manager._tools
    for name, tool in tools.items():
        desc = tool.description[:80] if tool.description else ""
        print(f"  - {name}: {desc}")
except Exception as e:
    print(f"  (tool enumeration: {e})")
    print(f"  Tool count: {len(app._tool_manager._tools)}")

print()
print("Server module validated OK")
