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

    async def list_tools(self, server_name: str) -> list[Any]:
        session = self._sessions.get(server_name)
        if not session:
            raise MCPClientError(f"Not connected to MCP server: {server_name}")
        return await session.list_tools()

    async def disconnect_all(self) -> None:
        for name in list(self._sessions):
            try:
                await self._sessions[name].__aexit__(None, None, None)
            except Exception as e:
                logger.warning("Error closing session %s: %s", name, e)
            try:
                await self._cleanup_ctxs[name].__aexit__(None, None, None)
            except Exception as e:
                logger.warning("Error closing transport %s: %s", name, e)
        self._sessions.clear()
        self._cleanup_ctxs.clear()
