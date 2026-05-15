import json
import logging
import uuid
from typing import Any

import httpx
from google.protobuf.json_format import MessageToDict

from a2a.client import create_client
from a2a.client.base_client import ClientCallContext
from a2a.client.client_factory import ClientConfig
from a2a.helpers import new_text_message
from a2a.types import SendMessageRequest

from shared.config import AGENT_SEED_URLS, A2A_TIMEOUT

logger = logging.getLogger(__name__)


class SubAgentClient:
    """Discovers and communicates with sub-agents via the A2A protocol.

    Discovery happens synchronously (``discover_sync()`` at module level).
    A2A clients for communicating with sub-agents are created lazily on first
    ``send_message()``, ensuring they use the correct async event loop.
    """

    def __init__(self) -> None:
        self._agents: dict[str, dict[str, Any]] = {}

    # ── Sync discovery (no running event loop needed) ──────────────

    def discover_sync(self, retries: int = 3, delay: float = 5.0) -> None:
        """Fetch agent cards from all seed URLs using sync HTTP.

        Retries failed URLs up to ``retries`` times with ``delay``
        seconds between attempts, to handle slow-starting agents.
        """
        import time

        urls = [
            u.strip()
            for u in AGENT_SEED_URLS.split(",")
            if u.strip()
        ]
        pending = list(urls)
        for attempt in range(retries):
            if not pending:
                break
            still_pending = []
            for url in pending:
                try:
                    with httpx.Client(
                        timeout=httpx.Timeout(10.0)
                    ) as h:
                        resp = h.get(
                            f"{url.rstrip('/')}/.well-known/agent-card.json"
                        )
                        resp.raise_for_status()
                        card = resp.json()
                        self._register(card, url)
                        logger.info(
                            "Discovered '%s' at %s with %d skills",
                            card.get("name", ""),
                            url,
                            len(card.get("skills", [])),
                        )
                except Exception as e:
                    still_pending.append(url)
                    if attempt < retries - 1:
                        logger.info(
                            "Agent at %s not ready yet (attempt %d/%d): %s",
                            url,
                            attempt + 1,
                            retries,
                            e,
                        )
                    else:
                        logger.warning(
                            "Failed to discover agent at %s "
                            "after %d attempts: %s",
                            url,
                            retries,
                            e,
                        )
            pending = still_pending
            if pending and attempt < retries - 1:
                logger.info(
                    "Waiting %ds before retrying %d agent(s)...",
                    delay,
                    len(pending),
                )
                time.sleep(delay)

        if not self._agents:
            logger.warning("No sub-agents discovered from any source")

    def _register(self, card: dict, url: str) -> None:
        name = card.get("name", "")
        self._agents[name] = {
            "url": url.rstrip("/"),
            "card_data": card,
            "skills": [
                {
                    "id": s.get("id", ""),
                    "name": s.get("name", ""),
                    "description": s.get("description", ""),
                }
                for s in card.get("skills", [])
            ],
            "_client": None,
        }

    # ── List available agents for the LLM instruction ──────────────

    def list_agents(self) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "description": a["card_data"].get("description", ""),
                "skills": ", ".join(
                    s["name"] or s["id"] for s in a["skills"]
                ),
            }
            for name, a in self._agents.items()
        ]

    def list_skills(self) -> list[dict[str, str]]:
        return [
            {
                "agent_name": name,
                "skill_id": s["id"],
                "skill_name": s["name"],
                "description": s["description"],
            }
            for name, a in self._agents.items()
            for s in a["skills"]
        ]

    # ── Lazy async A2A client (created in the current event loop) ──

    async def _get_a2a_client(self, agent_name: str) -> Any:
        entry = self._agents.get(agent_name)
        if not entry:
            return None
        if entry.get("_client") is not None:
            return entry["_client"]

        http = httpx.AsyncClient(
            timeout=httpx.Timeout(A2A_TIMEOUT)
        )
        entry["_client"] = await create_client(
            entry["url"],
            client_config=ClientConfig(
                streaming=False,
                httpx_client=http,
            ),
            resolver_http_kwargs={
                "timeout": httpx.Timeout(A2A_TIMEOUT)
            },
        )
        return entry["_client"]

    # ── Send a message to a specific agent ─────────────────────────

    async def send_message(
        self, agent_name: str, task: str
    ) -> str:
        """Send a task to a remote agent and return its text response.

        Args:
            agent_name: The name of the agent to send the task to.
            task: The task description for the agent.

        Returns:
            The text response from the agent, or an error message.
        """
        entry = self._agents.get(agent_name)
        if not entry:
            return json.dumps(
                {"error": f"No agent found: '{agent_name}'"}
            )

        client = await self._get_a2a_client(agent_name)
        if client is None:
            return json.dumps(
                {
                    "error": f"Failed to create client for '{agent_name}'"
                }
            )

        message_id = uuid.uuid4().hex
        req = SendMessageRequest(
            message=new_text_message(task, role=1),
        )
        ctx = ClientCallContext(timeout=A2A_TIMEOUT)

        async for event in client.send_message(req, context=ctx):
            if hasattr(event, "task") and event.task:
                t = event.task
                if t.status.state in (3, 4, 5):
                    text = self._extract_text(t)
                    if text:
                        return text
                    return json.dumps({"id": t.id, "state": t.status.state})
        return json.dumps({"error": "No response from agent"})

    def _extract_text(self, task: Any) -> str | None:
        for art in task.artifacts:
            for part in art.parts:
                if part.text:
                    return part.text
        for art in task.artifacts:
            for part in art.parts:
                if part.data:
                    try:
                        d = MessageToDict(part.data)
                        return json.dumps(d)
                    except Exception:
                        return str(part.data)
        if (
            hasattr(task, "status")
            and hasattr(task.status, "message")
            and task.status.message
        ):
            for part in task.status.message.parts:
                if part.text:
                    return part.text
        return None
