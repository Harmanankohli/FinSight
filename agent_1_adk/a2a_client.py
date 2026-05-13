import json
import logging
import os
import re
from typing import Any

import httpx
from google.protobuf.json_format import MessageToDict

from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client_factory import ClientFactory, ClientConfig
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    SendMessageRequest,
)

logger = logging.getLogger(__name__)


class A2ADiscoveryError(Exception):
    pass


class A2ADiscoverer:
    def __init__(self, seed_urls: list[str] | None = None):
        self._seed_urls = seed_urls or []
        self._skill_registry: dict[str, dict] = {}
        self._agent_cards: dict[str, AgentCard] = {}
        self._clients: dict[str, Any] = {}

    @classmethod
    def from_env(cls, env_var: str = "AGENT_SEED_URLS") -> "A2ADiscoverer":
        raw = os.environ.get(env_var, "")
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        return cls(seed_urls=urls)

    async def discover(self) -> None:
        if self._agent_cards:
            return
        for url in self._seed_urls:
            h = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
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
                logger.info(
                    "Discovered '%s' at %s with %d skills",
                    card.name, url, len(card.skills),
                )
            except Exception as e:
                logger.warning("Failed to discover agent at %s: %s", url, e)

    def find_agent(self, skill_id: str) -> dict | None:
        return self._skill_registry.get(skill_id)

    def find_agent_by_query(self, query: str) -> tuple[str, dict] | None:
        query_lower = query.lower()
        best = None
        best_score = 0
        for sid, entry in self._skill_registry.items():
            desc = entry["skill"].get("description", "").lower()
            name = entry["skill"].get("name", "").lower()
            score = sum(2 if w in sid else 1 for w in re.findall(r"\w+", query_lower) if w in desc or w in name or w in sid)
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

    def list_agents(self) -> dict[str, str]:
        return {url: card.name for url, card in self._agent_cards.items()}

    def list_skills(self) -> dict[str, dict]:
        return {sid: e["skill"] for sid, e in self._skill_registry.items()}


class A2AClient:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._discoverer: A2ADiscoverer | None = None

    def with_discoverer(self, discoverer: A2ADiscoverer) -> "A2AClient":
        self._discoverer = discoverer
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
            message=Message(
                role=Role.ROLE_USER,
                parts=[Part(text=query)],
            ),
        )
        if metadata:
            from google.protobuf.struct_pb2 import Struct as S
            s = S()
            s.update(metadata)
            req.metadata.CopyFrom(s)

        async for event in client.send_message(req):
            if hasattr(event, "task") and event.task:
                task = event.task
                if task.status.state in (3, 4, 5):
                    return self._task_to_dict(task)

        return {}

    def _task_to_dict(self, task: Any) -> dict:
        result = {
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

    def _extract_data(self, task_dict: dict) -> dict[str, Any]:
        data = {}
        for k, v in task_dict.items():
            if k not in ("id", "state", "text"):
                data[k] = v
        if not data and "text" in task_dict:
            data["text"] = task_dict["text"]
        return data
