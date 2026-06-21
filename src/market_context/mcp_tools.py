# ruff: noqa: E402
"""Dynamic MCP-to-CrewAI tool wrappers.

Provides DynamicMCPTool (a CrewAI BaseTool subclass that delegates execution
to an MCP server) and MCPClientWrapper (a caching discovery layer that
wraps the raw MCP client for use inside a CrewAI agent context).
"""

import asyncio
import json
import logging
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)

from shared.logging_config import logged, logged_sync


@logged_sync()
def _build_args_schema(name: str, input_schema: dict) -> type[BaseModel]:
    """Dynamically generate a Pydantic model from an MCP tool's JSON input schema.

    Converts the ``properties`` and ``required`` fields of the schema into
    typed Pydantic fields so CrewAI can validate tool arguments at runtime.
    """
    fields = {}
    props = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    for prop_name, prop_def in props.items():
        prop_def.get("type", "string")
        default = prop_def.get("default", ...)
        if prop_name not in required and "default" in prop_def:
            default = (
                str(prop_def["default"])
                if isinstance(prop_def["default"], (int, float))
                else prop_def["default"]
            )
        fields[prop_name] = (
            str,
            Field(default=default, description=prop_def.get("description", "")),
        )
    return create_model(f"{name}Arguments", **fields)


class DynamicMCPTool(BaseTool):
    """CrewAI BaseTool wrapper around a single MCP server tool.

    Dynamically builds a Pydantic args schema from the MCP tool's input
    schema so the CrewAI agent can call it with validated parameters.
    """

    name: str = ""
    description: str = ""

    @logged_sync(log_args=False, log_result=False)
    def __init__(
        self,
        tool_name: str,
        tool_description: str,
        input_schema: dict,
        mcp_wrapper: "MCPClientWrapper",
    ):
        """Initialise the tool with name, description, and schema
        derived from the MCP tool definition.

        The args_schema is built dynamically from the MCP tool's input_schema
        to enable CrewAI parameter validation.
        """
        args_schema = _build_args_schema(tool_name, input_schema)
        super().__init__(name=tool_name, description=tool_description, args_schema=args_schema)
        self._tool_name = tool_name
        self._mcp = mcp_wrapper

    @logged_sync()
    def _run(self, **kwargs: Any) -> str:
        """Synchronous entry point called by CrewAI from a worker thread.

        Delegates to the MCP client wrapper via asyncio.run (safe because
        CrewAI invokes _run from a thread executor, not an async loop).
        """
        result = asyncio.run(self._mcp.call_by_name(self._tool_name, kwargs))
        if isinstance(result, str):
            return result
        return json.dumps(result)


class MCPClientWrapper:
    """Caching adapter that wraps a raw MCP client for CrewAI tool discovery and invocation.

    Lazily discovers available tools from the MCP server, wraps each as a
    DynamicMCPTool, and provides async call methods accessible by tool name
    or by server/tool pair.
    """

    @logged_sync(log_args=False, log_result=False)
    def __init__(self, mcp_client: Any):
        """Store the underlying MCP client and initialise the tool-name-to-description cache."""
        self._client = mcp_client
        self._tool_cache: dict[str, Any] = {}

    @logged()
    async def discover_tools(self) -> list[DynamicMCPTool]:
        """Fetch available tools from the MCP server and wrap each as a DynamicMCPTool.

        Results are cached internally so subsequent calls return the same
        wrapper instances. Logs each discovered tool name and description.
        """
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

    @logged()
    async def call_by_name(self, tool_name: str, params: dict) -> Any:
        """Call an MCP tool by name and return a serialisable result.

        Handles content extraction from the MCP response object,
        returning either joined text, the raw content list, a dict,
        or a string representation as fallback.
        """
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

    @logged()
    async def call(self, server: str, tool: str, params: dict) -> Any:
        """Call an MCP tool by server name and tool name.

        Similar to call_by_name but uses the two-parameter server/tool
        routing provided by the underlying MCP client.
        """
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
