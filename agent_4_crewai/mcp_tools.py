import json
import logging
from typing import Any

from crewai.tools import BaseTool

logger = logging.getLogger(__name__)


class DynamicMCPTool(BaseTool):
    name: str = ""
    description: str = ""

    def __init__(self, tool_name: str, tool_description: str, mcp_wrapper: "MCPClientWrapper"):
        super().__init__(name=tool_name, description=tool_description)
        self._tool_name = tool_name
        self._mcp = mcp_wrapper

    def _run(self, **kwargs: Any) -> str:
        import asyncio
        result = asyncio.run(self._mcp.call_by_name(self._tool_name, kwargs))
        if isinstance(result, str):
            return result
        return json.dumps(result)


class MCPClientWrapper:
    def __init__(self, mcp_client: Any):
        self._client = mcp_client
        self._tool_cache: dict[str, Any] = {}

    async def discover_tools(self) -> list[DynamicMCPTool]:
        tools = await self._client.list_tools()
        discovered = []
        for t in tools:
            name = t.name if hasattr(t, "name") else str(t)
            desc = t.description if hasattr(t, "description") else ""
            tool = DynamicMCPTool(name, desc, self)
            self._tool_cache[name] = desc
            discovered.append(tool)
            logger.info("Discovered MCP tool: %s - %s", name, desc[:60])
        return discovered

    async def call_by_name(self, tool_name: str, params: dict) -> Any:
        try:
            result = await self._client.call_tool_by_name(tool_name, params)
            if hasattr(result, "content") and isinstance(result.content, list):
                texts = []
                for item in result.content:
                    if hasattr(item, "text"):
                        texts.append(item.text)
                    elif isinstance(item, dict) and "text" in item:
                        texts.append(item["text"])
                return "\n".join(texts) if texts else result.content
            if isinstance(result, dict):
                return result
            if hasattr(result, "content"):
                return result.content
            return str(result)
        except Exception as e:
            logger.warning("MCP tool call %s failed: %s", tool_name, e)
            return {"error": str(e)}

    async def call(self, server: str, tool: str, params: dict) -> Any:
        try:
            result = await self._client.call_tool(server, tool, params)
            if hasattr(result, "content") and isinstance(result.content, list):
                texts = []
                for item in result.content:
                    if hasattr(item, "text"):
                        texts.append(item.text)
                    elif isinstance(item, dict) and "text" in item:
                        texts.append(item["text"])
                return "\n".join(texts) if texts else result.content
            if isinstance(result, dict):
                return result
            if hasattr(result, "content"):
                return result.content
            return str(result)
        except Exception as e:
            logger.warning("MCP call %s/%s failed: %s", server, tool, e)
            return {"error": str(e)}
