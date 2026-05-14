import json
import logging
from typing import Any, Optional

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)


def _build_args_schema(name: str, input_schema: dict) -> type[BaseModel]:
    fields = {}
    props = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    for prop_name, prop_def in props.items():
        ptype = prop_def.get("type", "string")
        python_type = str
        default = prop_def.get("default", ...)
        if prop_name not in required and "default" in prop_def:
            default = str(prop_def["default"]) if isinstance(prop_def["default"], (int, float)) else prop_def["default"]
        fields[prop_name] = (str, Field(default=default, description=prop_def.get("description", "")))
    return create_model(f"{name}Arguments", **fields)


class DynamicMCPTool(BaseTool):
    name: str = ""
    description: str = ""

    def __init__(self, tool_name: str, tool_description: str, input_schema: dict, mcp_wrapper: "MCPClientWrapper"):
        args_schema = _build_args_schema(tool_name, input_schema)
        super().__init__(name=tool_name, description=tool_description, args_schema=args_schema)
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
            schema = t.inputSchema if hasattr(t, "inputSchema") else {}
            tool = DynamicMCPTool(name, desc, schema, self)
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
