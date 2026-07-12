"""
MCP client manager that discovers and exposes tools from MCP servers.

Provides a multi-server bridge with connection pooling, retry logic,
tool-based routing, and resource access across MCP protocol endpoints.
"""

import asyncio
import atexit
import json
import logging
from dataclasses import dataclass
from typing import Any, cast

import yaml
from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)

# Transient network errors that warrant automatic retries.
# httpx is optional — degrade gracefully if not installed.
try:
    import httpx as _httpx

    _TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
        asyncio.TimeoutError,
        ConnectionError,
        asyncio.IncompleteReadError,
        EOFError,
        OSError,
        _httpx.ReadError,
        _httpx.ConnectError,
        _httpx.NetworkError,
    )
except ImportError:
    _TRANSIENT_ERRORS = (
        asyncio.TimeoutError,
        ConnectionError,
        asyncio.IncompleteReadError,
        EOFError,
        OSError,
    )

# Process-wide singleton: prevents redundant SSE handshakes across the app.
_global_client: "MCPClient | None" = None
_client_lock = asyncio.Lock()


async def get_shared_mcp() -> "MCPClient":
    """
    Return the process-wide singleton MCPClient, connecting on first call.

    Double-checked locking ensures concurrent first callers collapse into a
    single connect_all() instead of each paying the SSE handshake cost.
    Injects the bearer token from settings when auth is enabled.
    """
    global _global_client
    if _global_client is not None and _global_client._connected:
        return _global_client
    async with _client_lock:
        if _global_client is None or not _global_client._connected:
            from shared.settings import MCP_SERVER_URL, MCP_TIMEOUT, get_settings

            _s = get_settings()
            _token = _s.service_auth_token if _s.auth_enabled else None
            _global_client = MCPClient(
                configs=[MCPServerConfig(name="finsight-mcp", url=MCP_SERVER_URL, auth=_token)],
                timeout=MCP_TIMEOUT,
            )
            await _global_client.connect_all()
    return _global_client


def _shutdown_mcp_sync() -> None:
    """Best-effort MCP disconnect at process exit (registered via atexit)."""
    if _global_client is None or not _global_client._connected:
        return
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_global_client.disconnect_all())
        loop.close()
    except Exception:
        pass


atexit.register(_shutdown_mcp_sync)


# Uniform type for parsed MCP tool results.
# Normalises complex MCP objects (content.text, raw data, JSON strings) into
# a plain Python type so callers don't need format-specific handling.
_MCPParsed = dict[str, Any] | list[Any] | str | int | float | bool


def parse_mcp_result(result: Any) -> _MCPParsed:
    """
    Normalise an MCP tool call result into a consistent Python object.

    Handles:
    - Objects with a .content attribute containing text/data parts
    - Plain dict, list, string, or scalar values
    - JSON-encoded strings embedded in response parts
    - None results

    Args:
        result: Raw response from an MCP tool call.

    Returns:
        A parsed dict, list, string, or scalar. On failure, returns a
        dict with an ``"error"`` key describing the problem.
    """
    if result is None:
        return {"error": "MCP result is None"}

    # Already a dict — pass through (errors included).
    if isinstance(result, dict):
        if "error" in result:
            return result
        return result

    # Plain non-dict literals — no parsing needed.
    if isinstance(result, (list, str, int, float, bool)):
        return result

    # Object-style result with .content (the common MCP SDK pattern).
    if hasattr(result, "content") and result.content:
        texts = []
        for item in result.content:
            if hasattr(item, "text") and item.text:
                texts.append(item.text)
            elif hasattr(item, "data") and item.data:
                if isinstance(item.data, dict):
                    return item.data
                texts.append(str(item.data))
            elif isinstance(item, dict):
                if "text" in item:
                    texts.append(item["text"])
                else:
                    return item
            elif hasattr(item, "json_result"):
                try:
                    return cast(_MCPParsed, json.loads(item.json_result))
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                texts.append(str(item))

        if not texts:
            return {"error": "No content in MCP result"}

        if len(texts) == 1:
            txt = texts[0]
            try:
                return cast(_MCPParsed, json.loads(txt))
            except (json.JSONDecodeError, TypeError):
                return cast(_MCPParsed, txt)

        for txt in texts:
            try:
                return cast(_MCPParsed, json.loads(txt))
            except (json.JSONDecodeError, TypeError):
                continue

        return texts[0] if texts else {"error": "Could not parse MCP content"}

    return {"error": f"Unexpected MCP result type: {type(result).__name__}"}


class MCPClientError(Exception):
    """Raised on MCP connection or tool-call failures."""
    pass


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server connection."""
    name: str
    url: str
    auth: str | None = None
    tools: list[str] | None = None


class MCPClient:
    """
    Multi-server MCP bridge.

    Maintains a session pool keyed by server name and a global tool-to-server
    registry so callers can route by tool name without knowing which server
    hosts it. Servers are loaded from a YAML file or passed programmatically.
    """

    def __init__(
        self,
        config_path: str | None = None,
        configs: list[MCPServerConfig] | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        """
        Args:
            config_path: Path to a YAML config file with server definitions.
            configs: Inline server configs (used when config_path is None).
            timeout: Default timeout (seconds) for tool calls.
            max_retries: Number of connection / transient-error retries.
        """
        self.timeout = timeout
        self.max_retries = max_retries
        self._sessions: dict[str, ClientSession] = {}
        self._cleanup_ctxs: dict[str, Any] = {}
        self._tool_registry: dict[str, str] = {}
        self._tool_definitions: dict[str, Any] = {}
        self._connected = False

        if config_path:
            self._load_config(config_path)
        else:
            self.servers: list[MCPServerConfig] = configs or []

    def _load_config(self, path: str) -> None:
        """Load server definitions from a YAML file."""
        with open(path) as f:
            raw = yaml.safe_load(f)
        self.servers = [MCPServerConfig(**s) for s in raw.get("mcp_servers", [])]

    async def connect_all(self) -> None:
        """Connect to every configured MCP server."""
        for server in self.servers:
            await self._connect_server(server)
        self._connected = True

    async def _connect_server(self, server: MCPServerConfig) -> None:
        """Connect to a single MCP server with retries and exponential backoff.

        Each attempt creates a fresh transport — stale state from prior
        failures is discarded. Populates the session pool and tool registry
        on success.
        """
        headers = None
        if server.auth:
            headers = {"Authorization": f"Bearer {server.auth}"}
        for attempt in range(self.max_retries):
            try:
                ctx = sse_client(url=server.url, headers=headers)
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
                    attempt + 1,
                    self.max_retries,
                    server.name,
                    e,
                )
                if attempt < self.max_retries - 1:
                    wait = 2**attempt
                    await asyncio.sleep(wait)
        raise MCPClientError(
            f"Failed to connect to MCP server '{server.name}' after {self.max_retries} attempts"
        )

    # Queries the server for its available tools and populates the lookup
    # registry so call_tool_by_name can route a tool name to its host server.
    async def _discover_tools(self, server_name: str, session: ClientSession) -> None:
        """Query a connected server for its tool list and populate the registry.

        The registry maps tool names to their host server so `call_tool_by_name`
        can route calls without the caller knowing the server topology.
        """
        try:
            result = await session.list_tools()
            tools = result.tools if hasattr(result, "tools") else result
            for tool in tools:
                tool_name = tool.name if hasattr(tool, "name") else str(tool)  # type: ignore[union-attr]
                self._tool_registry[tool_name] = server_name
                self._tool_definitions[tool_name] = tool
                logger.debug("Discovered tool '%s' on server '%s'", tool_name, server_name)
        except Exception as e:
            logger.warning("Failed to discover tools on '%s': %s", server_name, e)

    # Transient network errors worth retrying on tool calls; all other
    # exceptions (bad args, server errors) fail immediately.
    _TRANSIENT_EXC = _TRANSIENT_ERRORS

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Call a tool on a specific named server.

        Retries on transient network errors with exponential backoff.
        Non-transient exceptions (bad args, server errors) fail immediately.
        """
        session = self._sessions.get(server_name)
        if not session:
            raise MCPClientError(f"Not connected to MCP server: {server_name}")

        for attempt in range(self.max_retries):
            t = self.timeout
            try:
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments or {}),
                    timeout=t,
                )
                return result
            except MCPClient._TRANSIENT_EXC as e:
                if attempt < self.max_retries - 1:
                    wait = 2**attempt
                    logger.warning(
                        "MCP call attempt %d/%d transient error on %s/%s: %s — retrying in %ds",
                        attempt + 1,
                        self.max_retries,
                        server_name,
                        tool_name,
                        e,
                        wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise MCPClientError(
                        f"Tool call '{server_name}/{tool_name}' failed after {self.max_retries} attempts: {e}"  # noqa: E501
                    ) from e
            except MCPClientError:
                raise
            except Exception as e:
                # Non-transient (bad args, server error, etc.) — fail immediately
                logger.warning(
                    "MCP call non-transient error on %s/%s: %s", server_name, tool_name, e
                )
                raise
        raise MCPClientError(
            f"Tool call '{server_name}/{tool_name}' failed after {self.max_retries} attempts"
        )

    async def call_tool_by_name(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Call a tool by name, resolving the host server automatically.

        Looks up the server from the tool registry, delegates to `call_tool`,
        and on connection-level failure marks disconnected, reconnects once,
        and retries. Primary entry point for agents that only know what tool
        they need, not where it lives.
        """
        _RECONNECT_EXC = (ConnectionError, asyncio.IncompleteReadError, EOFError)
        for attempt in range(2):
            server_name = self._tool_registry.get(tool_name)
            if not server_name:
                available = ", ".join(sorted(self._tool_registry))
                raise MCPClientError(f"Tool '{tool_name}' not found. Available tools: {available}")
            try:
                return await self.call_tool(server_name, tool_name, arguments)
            except _RECONNECT_EXC as exc:
                if attempt == 1:
                    raise
                logger.warning("MCP connection lost (%s); reconnecting…", exc)
                self._connected = False
                await self.connect_all()

    async def list_tools(self, server_name: str | None = None) -> list[Any]:
        """List available tools, optionally scoped to a single server."""
        if server_name:
            session = self._sessions.get(server_name)
            if not session:
                raise MCPClientError(f"Not connected to MCP server: {server_name}")
            result = await session.list_tools()
            return result.tools if hasattr(result, "tools") else list(result)
        all_tools: list[Any] = []
        for name, session in self._sessions.items():
            try:
                result = await session.list_tools()
                tools = result.tools if hasattr(result, "tools") else result
                for t in tools:
                    t._server_name = name  # type: ignore[union-attr]
                all_tools.extend(tools)
            except Exception as e:
                logger.warning("Failed to list tools on '%s': %s", name, e)
        return all_tools

    async def list_resources(self, server_name: str | None = None) -> list[Any]:
        """List available resources, optionally scoped to a single server."""
        sessions = (
            [(server_name, self._sessions.get(server_name))]
            if server_name
            else list(self._sessions.items())
        )
        resources: list[Any] = []
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
        """Read a resource by URI, trying all connected servers if none specified."""
        sessions = (
            [(server_name, self._sessions.get(server_name))]
            if server_name
            else list(self._sessions.items())
        )
        for name, session in sessions:
            if not session:
                continue
            try:
                return await session.read_resource(uri)  # type: ignore[arg-type]
            except Exception as e:
                logger.debug("Failed to read resource '%s' on '%s': %s", uri, name, e)
        raise MCPClientError(f"Resource '{uri}' not found on any connected server")

    def get_available_tools(self) -> dict[str, str]:
        """Return a copy of the tool-to-server registry."""
        return dict(self._tool_registry)

    def get_tool_definition(self, tool_name: str) -> Any | None:
        """Return the raw definition object for a tool, or None."""
        return self._tool_definitions.get(tool_name)

    async def discover_all(self, server_urls: list[str]) -> None:
        """Register and connect to a list of servers by URL (auto-named)."""
        for i, url in enumerate(server_urls):
            name = f"mcp-server-{i}"
            self.servers.append(MCPServerConfig(name=name, url=url))
        await self.connect_all()

    # Cleans up sessions first, then transports, then in-memory state.
    # Order matters: closing the session while its transport is still live
    # causes protocol errors, so transport is torn down last.
    async def disconnect_all(self) -> None:
        """Close all sessions and transports, then clear in-memory state.

        Order matters: each session is closed before its transport to avoid
        protocol errors. State dicts are cleared last.
        """
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
        self._connected = False
