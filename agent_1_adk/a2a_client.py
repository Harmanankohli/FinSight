import asyncio
import json
import logging
import os
import re
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class A2ADiscoveryError(Exception):
    pass


class A2ADiscoverer:
    def __init__(self, seed_urls: list[str] | None = None):
        self._seed_urls = seed_urls or []
        self._skill_registry: dict[str, dict] = {}
        self._agent_cards: dict[str, dict] = {}

    @classmethod
    def from_env(cls, env_var: str = "AGENT_SEED_URLS") -> "A2ADiscoverer":
        raw = os.environ.get(env_var, "")
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        return cls(seed_urls=urls)

    async def discover(self) -> None:
        for url in self._seed_urls:
            try:
                card_url = url.rstrip("/") + "/.well-known/agent-card.json"
                async with httpx.AsyncClient(timeout=10) as c:
                    resp = await c.get(card_url, headers={"A2A-Version": "1.0"})
                    resp.raise_for_status()
                    card = resp.json()
                    self._agent_cards[url] = card
                    for skill in card.get("skills", []):
                        skill_id = skill.get("id")
                        if skill_id:
                            self._skill_registry[skill_id] = {
                                "agent_url": url,
                                "agent_name": card.get("name"),
                                "skill": skill,
                            }
                    logger.info(
                        "Discovered agent '%s' at %s with %d skills",
                        card.get("name"), url, len(card.get("skills", [])),
                    )
            except Exception as e:
                logger.warning("Failed to discover agent at %s: %s", url, e)

    def find_agent(self, skill_id: str) -> dict | None:
        return self._skill_registry.get(skill_id)

    def find_agent_by_query(self, query: str) -> dict | None:
        query_lower = query.lower()
        best_match = None
        best_score = 0
        for skill_id, entry in self._skill_registry.items():
            desc = entry["skill"].get("description", "").lower()
            name = entry["skill"].get("name", "").lower()
            score = 0
            for word in re.findall(r"\w+", query_lower):
                if word in desc or word in name:
                    score += 1
                if word in skill_id:
                    score += 2
            if "ticker" in query_lower or "stock" in query_lower:
                if "sec" in skill_id or "filing" in skill_id or "earnings" in skill_id:
                    score += 1
                if "quant" in skill_id or "metrics" in skill_id or "risk" in skill_id:
                    score += 1
                if "sentiment" in skill_id or "narrative" in skill_id:
                    score += 1
            if score > best_score:
                best_score = score
                best_match = (skill_id, entry)
        return best_match

    def list_agents(self) -> dict[str, dict]:
        return dict(self._agent_cards)

    def list_skills(self) -> dict[str, dict]:
        return dict(self._skill_registry)

    def get_agent_card(self, url: str) -> dict | None:
        return self._agent_cards.get(url)


class A2AClient:
    def __init__(self, timeout: float = 30.0, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries
        self._discoverer: A2ADiscoverer | None = None

    def with_discoverer(self, discoverer: A2ADiscoverer) -> "A2AClient":
        self._discoverer = discoverer
        return self

    def _resolve_url(self, agent_url_or_skill: str) -> tuple[str, str]:
        if agent_url_or_skill.startswith("http"):
            return agent_url_or_skill, ""
        if self._discoverer:
            entry = self._discoverer.find_agent(agent_url_or_skill)
            if entry:
                return entry["agent_url"], agent_url_or_skill
            match = self._discoverer.find_agent_by_query(agent_url_or_skill)
            if match:
                skill_id, entry = match
                return entry["agent_url"], skill_id
        raise A2ADiscoveryError(
            f"Cannot resolve '{agent_url_or_skill}' — no discoverer or matching agent"
        )

    async def send_task(
        self,
        agent_url_or_skill: str,
        skill_id: str | None = None,
        query: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        url, resolved_skill = self._resolve_url(agent_url_or_skill)
        skill = skill_id or resolved_skill

        task_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "method": "SendMessage",
            "id": task_id,
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"text": query}],
                },
                "metadata": {
                    **(metadata or {}),
                    "correlation_id": task_id,
                },
            },
        }

        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{url}/a2a",
                        json=body,
                        headers={"A2A-Version": "1.0"},
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    task = result.get("result", {}).get("task", {})
                    if task.get("status", {}).get("state") == "TASK_STATE_FAILED":
                        err_text = "Unknown error"
                        for art in task.get("artifacts", []):
                            for part in art.get("parts", []):
                                if "text" in part:
                                    err_text = part["text"]
                        raise RuntimeError(err_text)
                    return result
            except Exception as e:
                logger.warning(
                    "A2A call to %s attempt %d/%d failed: %s",
                    url, attempt + 1, self.max_retries + 1, e,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
                else:
                    raise

    def _extract_data(self, response: dict) -> Any:
        task = response.get("result", {}).get("task", {})
        for art in task.get("artifacts", []):
            for part in art.get("parts", []):
                if "data" in part:
                    data = part["data"]
                    if isinstance(data, dict):
                        return data
                    try:
                        return json.loads(str(data))
                    except (json.JSONDecodeError, TypeError):
                        return {"data": str(data)}
                if "text" in part:
                    return {"text": part["text"]}
        return {}

    async def query_rag(
        self, url_or_skill: str, ticker: str, query_text: str,
        filing_types: list[str] | None = None,
    ) -> Any:
        resp = await self.send_task(
            url_or_skill, query=query_text,
            metadata={"ticker": ticker, "filing_types": filing_types or ["10-K", "10-Q"]},
        )
        return self._extract_data(resp)

    async def query_quant(
        self, url_or_skill: str, ticker: str, period: str = "5y"
    ) -> Any:
        resp = await self.send_task(
            url_or_skill,
            query=f"Analyze {ticker} quantitative metrics",
            metadata={"ticker": ticker, "period": period},
        )
        return self._extract_data(resp)

    async def query_sentiment(
        self, url_or_skill: str, ticker: str
    ) -> Any:
        resp = await self.send_task(
            url_or_skill,
            query=f"Analyze sentiment for {ticker}",
            metadata={"ticker": ticker, "sources": ["reddit", "twitter"]},
        )
        return self._extract_data(resp)
