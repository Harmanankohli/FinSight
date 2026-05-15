import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import yaml
from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)


class MCPClientError(Exception):
    pass


@dataclass
class MCPServerConfig:
    name: str
    url: str
    auth: str | None = None
    tools: list[str] | None = None


class MCPClient:
    def __init__(
        self,
        config_path: str | None = None,
        configs: list[MCPServerConfig] | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self._sessions: dict[str, ClientSession] = {}
        self._cleanup_ctxs: dict[str, Any] = {}
        self._tool_registry: dict[str, str] = {}
        self._tool_definitions: dict[str, Any] = {}

        if config_path:
            self._load_config(config_path)
        else:
            self.servers: list[MCPServerConfig] = configs or []

    def _load_config(self, path: str) -> None:
        with open(path) as f:
            raw = yaml.safe_load(f)
        self.servers = [MCPServerConfig(**s) for s in raw.get("mcp_servers", [])]

    async def connect_all(self) -> None:
        for server in self.servers:
            await self._connect_server(server)

    async def _connect_server(self, server: MCPServerConfig) -> None:
        for attempt in range(self.max_retries):
            try:
                ctx = sse_client(url=server.url)
                streams = await ctx.__aenter__()
                read_stream, write_stream = streams
                session = await ClientSession(read_stream, write_stream).__aenter__()
                await session.initialize()
                self._sessions[server.name] = session
                self._cleanup_ctxs[server.name] = ctx
                await self._discover_tools(server.name, session)
                logger.info("Connected to MCP server: %s (%s)", server.name, server.url)
                return
            except Exception as e:
                logger.warning(
                    "MCP connect attempt %d/%d failed for %s: %s",
                    attempt + 1, self.max_retries, server.name, e,
                )
                if attempt < self.max_retries - 1:
                    wait = 2 ** attempt
                    await asyncio.sleep(wait)
        raise MCPClientError(
            f"Failed to connect to MCP server '{server.name}' after {self.max_retries} attempts"
        )

    async def _discover_tools(self, server_name: str, session: ClientSession) -> None:
        try:
            result = await session.list_tools()
            tools = result.tools if hasattr(result, "tools") else result
            for tool in tools:
                tool_name = tool.name if hasattr(tool, "name") else str(tool)
                self._tool_registry[tool_name] = server_name
                self._tool_definitions[tool_name] = tool
                logger.debug("Discovered tool '%s' on server '%s'", tool_name, server_name)
        except Exception as e:
            logger.warning("Failed to discover tools on '%s': %s", server_name, e)

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        session = self._sessions.get(server_name)
        if not session:
            raise MCPClientError(f"Not connected to MCP server: {server_name}")

        for attempt in range(self.max_retries):
            try:
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments or {}),
                    timeout=self.timeout,
                )
                return result
            except Exception as e:
                logger.warning(
                    "MCP call attempt %d/%d failed on %s/%s: %s",
                    attempt + 1, self.max_retries, server_name, tool_name, e,
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
        raise MCPClientError(
            f"Tool call '{server_name}/{tool_name}' failed after {self.max_retries} attempts"
        )

    async def call_tool_by_name(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        server_name = self._tool_registry.get(tool_name)
        if not server_name:
            available = ", ".join(sorted(self._tool_registry))
            raise MCPClientError(
                f"Tool '{tool_name}' not found. Available tools: {available}"
            )
        return await self.call_tool(server_name, tool_name, arguments)

    async def list_tools(self, server_name: str | None = None) -> list[Any]:
        if server_name:
            session = self._sessions.get(server_name)
            if not session:
                raise MCPClientError(f"Not connected to MCP server: {server_name}")
            result = await session.list_tools()
            return result.tools if hasattr(result, "tools") else result
        all_tools = []
        for name, session in self._sessions.items():
            try:
                result = await session.list_tools()
                tools = result.tools if hasattr(result, "tools") else result
                for t in tools:
                    t._server_name = name
                all_tools.extend(tools)
            except Exception as e:
                logger.warning("Failed to list tools on '%s': %s", name, e)
        return all_tools

    async def list_resources(self, server_name: str | None = None) -> list[Any]:
        sessions = (
            [(server_name, self._sessions.get(server_name))]
            if server_name
            else list(self._sessions.items())
        )
        resources = []
        for name, session in sessions:
            if not session:
                continue
            try:
                result = await session.list_resources()
                items = result.resources if hasattr(result, "resources") else result
                for r in items:
                    r._server_name = name  # type: ignore
                resources.extend(items)
            except Exception as e:
                logger.warning("Failed to list resources on '%s': %s", name, e)
        return resources

    async def read_resource(self, uri: str, server_name: str | None = None) -> Any:
        sessions = (
            [(server_name, self._sessions.get(server_name))]
            if server_name
            else list(self._sessions.items())
        )
        for name, session in sessions:
            if not session:
                continue
            try:
                return await session.read_resource(uri)
            except Exception as e:
                logger.debug("Failed to read resource '%s' on '%s': %s", uri, name, e)
        raise MCPClientError(f"Resource '{uri}' not found on any connected server")

    def get_available_tools(self) -> dict[str, str]:
        return dict(self._tool_registry)

    def get_tool_definition(self, tool_name: str) -> Any | None:
        return self._tool_definitions.get(tool_name)

    async def discover_all(self, server_urls: list[str]) -> None:
        for i, url in enumerate(server_urls):
            name = f"mcp-server-{i}"
            self.servers.append(MCPServerConfig(name=name, url=url))
        await self.connect_all()

    async def disconnect_all(self) -> None:
        for name in list(self._sessions):
            try:
                await self._sessions[name].__aexit__(None, None, None)
            except BaseException as e:
                logger.debug("Session cleanup warning %s: %s", name, e)
            try:
                await self._cleanup_ctxs[name].__aexit__(None, None, None)
            except BaseException as e:
                logger.debug("Transport cleanup warning %s: %s", name, e)
        self._sessions.clear()
        self._cleanup_ctxs.clear()
        self._tool_registry.clear()
        self._tool_definitions.clear()
