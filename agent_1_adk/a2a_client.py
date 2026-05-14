import json
import logging
import os
import re
from typing import Any

import httpx
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct

from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client_factory import ClientFactory, ClientConfig
from a2a.helpers import new_text_message
from a2a.client.base_client import ClientCallContext
from a2a.types import (
    AgentCard,
    SendMessageRequest,
)

from shared.config import MCP_TIMEOUT, MCP_MAX_RETRIES

logger = logging.getLogger(__name__)


class A2ADiscoveryError(Exception):
    pass


class A2ADiscoverer:
    """Discovers agents via seed URLs (A2A cards) or MCP registry."""

    def __init__(self, seed_urls: list[str] | None = None, request_timeout: float = 120.0):
        self._seed_urls = seed_urls or []
        self._request_timeout = request_timeout
        self._skill_registry: dict[str, dict] = {}
        self._agent_cards: dict[str, AgentCard] = {}
        self._mcp_registry_url: str | None = None

    @classmethod
    def from_env(cls, env_var: str = "AGENT_SEED_URLS") -> "A2ADiscoverer":
        raw = os.environ.get(env_var, "")
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        return cls(seed_urls=urls)

    def with_mcp_registry(self, url: str) -> "A2ADiscoverer":
        self._mcp_registry_url = url
        return self

    async def discover(self) -> None:
        if self._agent_cards:
            return

        if self._seed_urls:
            await self._discover_via_seed_urls()
            if self._skill_registry:
                return

        if self._mcp_registry_url:
            try:
                await self._discover_via_mcp_registry()
                return
            except Exception as e:
                logger.warning("MCP registry discovery failed: %s", e)

        if not self._skill_registry:
            raise A2ADiscoveryError("No agents discovered from any source")

    async def _discover_via_seed_urls(self) -> None:
        for url in self._seed_urls:
            h = httpx.AsyncClient(timeout=httpx.Timeout(read=self._request_timeout, connect=10.0, write=self._request_timeout, pool=self._request_timeout))
            try:
                resolver = A2ACardResolver(h, url)
                card = await resolver.get_agent_card()
                self._agent_cards[url] = card
                factory = ClientFactory(ClientConfig(streaming=False, httpx_client=h))
                client = factory.create(card)
                for skill in card.skills:
                    sid = skill.id
                    self._skill_registry[sid] = {
                        "agent_url": url,
                        "agent_name": card.name,
                        "client": client,
                        "skill": {
                            "id": sid,
                            "name": skill.name,
                            "description": skill.description,
                            "input_modes": list(skill.input_modes),
                            "output_modes": list(skill.output_modes),
                        },
                    }
                logger.info("Discovered '%s' at %s with %d skills", card.name, url, len(card.skills))
            except Exception as e:
                logger.warning("Failed to discover agent at %s: %s", url, e)

    async def _discover_via_mcp_registry(self) -> None:
        from shared.mcp_client import MCPClient, MCPServerConfig

        mcp = MCPClient(configs=[MCPServerConfig(name="registry", url=f"{self._mcp_registry_url}/sse")])
        try:
            await mcp.connect_all()
        except Exception as e:
            logger.warning("MCP registry SSE connection failed: %s", e)
            return

        tool_list = await mcp.list_tools()
        logger.info("MCP registry has %d tools", len(tool_list))

        result = await mcp.call_tool_by_name("find_agent", {"query": "list all available agents"})
        if hasattr(result, "content"):
            for item in result.content:
                raw = item.text if hasattr(item, "text") else str(item)
                try:
                    import json
                    card_json = json.loads(raw) if isinstance(raw, str) else raw
                    agent_url = card_json.get("url", "").rstrip("/")
                    if agent_url and agent_url not in self._agent_cards:
                        h = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
                        resolver = A2ACardResolver(h, agent_url)
                        card = await resolver.get_agent_card()
                        self._agent_cards[agent_url] = card
                        factory = ClientFactory(ClientConfig(streaming=False, httpx_client=h))
                        client = factory.create(card)
                        for skill in card.skills:
                            sid = skill.id
                            self._skill_registry[sid] = {
                                "agent_url": agent_url,
                                "agent_name": card.name,
                                "client": client,
                                "skill": {
                                    "id": sid,
                                    "name": skill.name,
                                    "description": skill.description,
                                    "input_modes": list(skill.input_modes),
                                    "output_modes": list(skill.output_modes),
                                },
                            }
                        logger.info("MCP-discovered '%s' at %s", card.name, agent_url)
                except Exception as e:
                    logger.warning("Failed to process MCP-discovered card: %s", e)

        await mcp.disconnect_all()

    def find_agent(self, skill_id: str) -> dict | None:
        return self._skill_registry.get(skill_id)

    def find_agent_by_query(self, query: str) -> tuple[str, dict] | None:
        query_lower = query.lower()
        best = None
        best_score = 0
        for sid, entry in self._skill_registry.items():
            desc = entry["skill"].get("description", "").lower()
            name = entry["skill"].get("name", "").lower()
            score = sum(
                2 if w in sid else 1
                for w in re.findall(r"\w+", query_lower)
                if w in desc or w in name or w in sid
            )
            if any(w in query_lower for w in ["ticker", "stock", "sec", "filing"]):
                if "sec" in sid or "filing" in sid or "earnings" in sid:
                    score += 1
            if any(w in query_lower for w in ["risk", "sharpe", "beta", "vol"]):
                if "quant" in sid:
                    score += 1
            if any(w in query_lower for w in ["sentiment", "reddit", "news"]):
                if "sentiment" in sid:
                    score += 1
            if score > best_score:
                best_score = score
                best = (sid, entry)
        return best

    def get_client(self, skill_id: str) -> Any | None:
        entry = self._skill_registry.get(skill_id)
        return entry["client"] if entry else None

    def get_agent_url(self, skill_id: str) -> str | None:
        entry = self._skill_registry.get(skill_id)
        return entry["agent_url"] if entry else None

    def list_agents(self) -> dict[str, str]:
        return {url: card.name for url, card in self._agent_cards.items()}

    def list_skills(self) -> dict[str, dict]:
        return {sid: e["skill"] for sid, e in self._skill_registry.items()}


class A2AClient:
    """Sends A2A messages to discovered agents."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._discoverer: A2ADiscoverer | None = None

    def with_discoverer(self, discoverer: A2ADiscoverer) -> "A2AClient":
        self._discoverer = discoverer
        discoverer._request_timeout = max(discoverer._request_timeout, self.timeout)
        return self

    async def send_message(
        self, skill_id: str, query: str = "", metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._discoverer:
            raise A2ADiscoveryError("No discoverer configured")
        client = self._discoverer.get_client(skill_id)
        if not client:
            raise A2ADiscoveryError(f"No client for skill '{skill_id}'")

        req = SendMessageRequest(
            message=new_text_message(query, role=1),
        )
        if metadata:
            s = Struct()
            s.update(metadata)
            req.metadata.CopyFrom(s)

        ctx = ClientCallContext(timeout=self.timeout)
        async for event in client.send_message(req, context=ctx):
            if hasattr(event, "task") and event.task:
                task = event.task
                if task.status.state in (3, 4, 5):
                    return self._task_to_dict(task)

        return {}

    def _task_to_dict(self, task: Any) -> dict:
        result: dict[str, Any] = {
            "id": task.id,
            "state": task.status.state,
        }
        for art in task.artifacts:
            for part in art.parts:
                if part.text:
                    result["text"] = part.text
                elif part.data:
                    try:
                        d = MessageToDict(part.data)
                        result.update(d)
                    except Exception:
                        result["data"] = str(part.data)
        return result
