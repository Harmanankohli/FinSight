import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

HTTPXClientInstrumentor().instrument()

from a2a.client import (
    A2ACardResolver,
    ClientConfig,
    ClientFactory,
)
from a2a.helpers import (
    get_artifact_text,
    get_data_parts,
    get_message_text,
    new_text_message,
)
from a2a.types.a2a_pb2 import (
    AgentCard,
    SendMessageRequest,
    TaskState,
)

_TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_INPUT_REQUIRED,
}

from shared.config import AGENT_SEED_URLS, A2A_TIMEOUT
from shared.observability import get_langfuse_client
from shared.trace_context import inject_trace_context

logger = logging.getLogger(__name__)

_EVAL_TRACE_ENABLED = os.environ.get("EVAL_TRACE_ENABLED", "false").lower() == "true"
_EVAL_TRACES_DIR = Path(__file__).parent.parent / "tests" / "evaluation" / "eval_results" / "orchestrator_traces"


class SubAgentClient:
    """Discovers and communicates with sub-agents via the A2A protocol.

    Uses ``A2ACardResolver`` for standard-compliant agent discovery and
    ``ClientFactory`` for creating A2A client connections, matching the
    patterns used in the official A2A samples.
    """

    def __init__(self) -> None:
        self._agents: dict[str, dict[str, Any]] = {}

    async def discover(self, retries: int = 3, delay: float = 5.0) -> None:
        """Fetch agent cards from all seed URLs using ``A2ACardResolver``.

        Retries failed URLs up to ``retries`` times with ``delay``
        seconds between attempts, to handle slow-starting agents.
        """
        urls = [
            u.strip()
            for u in AGENT_SEED_URLS.split(",")
            if u.strip()
        ]
        pending = list(urls)
        http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        try:
            for attempt in range(retries):
                if not pending:
                    break
                still_pending = []
                for url in pending:
                    try:
                        resolver = A2ACardResolver(http, url)
                        card: AgentCard = await resolver.get_agent_card()
                        self._register(card, url)
                        logger.info(
                            "Discovered '%s' at %s with %d skills",
                            card.name,
                            url,
                            len(card.skills),
                        )
                    except Exception as e:
                        still_pending.append(url)
                        if attempt < retries - 1:
                            logger.info(
                                "Agent at %s not ready yet "
                                "(attempt %d/%d): %s",
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
                    await asyncio.sleep(delay)
        finally:
            await http.aclose()

        if not self._agents:
            logger.warning("No sub-agents discovered from any source")

    def _register(self, card: AgentCard, url: str) -> None:
        self._agents[card.name] = {
            "url": url.rstrip("/"),
            "card": card,
            "_client": None,
            "_http": None,
        }

    def resolve_agent_name(self, name: str) -> str | None:
        """Return the registered agent name that best matches `name`.

        Checks in order:
        1. Exact match
        2. Case-insensitive exact match
        3. Case-insensitive substring match (name is contained in registered key or vice-versa)
        Returns None if nothing matches.
        """
        if name in self._agents:
            return name
        lower = name.lower()
        for key in self._agents:
            if key.lower() == lower:
                return key
        for key in self._agents:
            kl = key.lower()
            if lower in kl or kl in lower:
                return key
        return None

    def list_agents(self) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "description": a["card"].description,
                "skills": ", ".join(
                    s.name or s.id for s in a["card"].skills
                ),
            }
            for name, a in self._agents.items()
        ]

    def list_skills(self) -> list[dict[str, str]]:
        return [
            {
                "agent_name": name,
                "skill_id": s.id,
                "skill_name": s.name,
                "description": s.description,
            }
            for name, a in self._agents.items()
            for s in a["card"].skills
        ]

    async def _get_a2a_client(self, agent_name: str) -> Any:
        entry = self._agents.get(agent_name)
        if not entry:
            return None
        if entry.get("_client") is not None:
            return entry["_client"]

        http = httpx.AsyncClient(timeout=httpx.Timeout(A2A_TIMEOUT))
        config = ClientConfig(
            streaming=True,
            httpx_client=http,
            supported_protocol_bindings=["JSONRPC", "HTTP+JSON"],
        )
        factory = ClientFactory(config)
        entry["_client"] = factory.create(entry["card"])
        entry["_http"] = http
        return entry["_client"]

    async def send_message(self, agent_name: str, task_str: str) -> str:
        """Send a task to a remote agent and return its text response.

        Follows the A2A sample pattern:
        - ``Message`` events → return immediately
        - ``TaskArtifactUpdateEvent`` events → capture result (data or text)
        - ``TaskStatusUpdateEvent`` (WORKING/SUBMITTED) → skip
        - terminal ``TaskStatusUpdateEvent`` or ``Task`` → extract from artifacts
        """
        entry = self._agents.get(agent_name)
        if not entry:
            return json.dumps({"error": f"No agent found: '{agent_name}'"})

        client = await self._get_a2a_client(agent_name)
        if client is None:
            return json.dumps(
                {"error": f"Failed to create client for '{agent_name}'"}
            )

        # --- Inject distributed trace context ---
        lf = get_langfuse_client()
        trace_id = lf.get_current_trace_id()
        parent_span_id = lf.get_current_observation_id()
        if trace_id and parent_span_id:
            task_str = inject_trace_context(task_str, trace_id, parent_span_id)
            logger.debug(
                "Injected trace context into task for '%s': trace=%s",
                agent_name, trace_id[:8]
            )
        # ----------------------------------------

        message = new_text_message(task_str, role=1)
        req = SendMessageRequest(message=message)

        t0 = time.monotonic()
        result_text = json.dumps({"error": "No response from agent"})
        try:
            async for event in client.send_message(req):
                # 1. Direct message response (immediate, stateless)
                if event.HasField("message"):
                    result_text = get_message_text(event.message)
                    break

                # 2. Artifact update — streaming result data
                if event.HasField("artifact_update"):
                    parts = event.artifact_update.artifact.parts
                    data_parts = get_data_parts(parts)
                    if data_parts:
                        result_text = json.dumps(data_parts[0])
                        break
                    art_text = get_artifact_text(
                        event.artifact_update.artifact
                    )
                    if art_text:
                        result_text = art_text
                        break
                    continue

                # 3. Status update — skip intermediate, handle terminal
                if event.HasField("status_update"):
                    state = event.status_update.status.state
                    if state not in _TERMINAL_STATES:
                        continue  # SUBMITTED, WORKING → skip
                    result_text = self._extract_terminal_result(event.status_update)
                    break

                # 4. Full task — only process if terminal (skip SUBMITTED/WORKING)
                if event.HasField("task"):
                    if event.task.status.state not in _TERMINAL_STATES:
                        continue
                    result_text = self._extract_task_result(event.task)
                    break

        except Exception as e:
            logger.exception("Error sending message to '%s'", agent_name)
            result_text = json.dumps({"error": str(e)})

        latency_ms = round((time.monotonic() - t0) * 1000)

        # Record trace for RAGAS evaluation when enabled
        if _EVAL_TRACE_ENABLED:
            try:
                import uuid as _uuid
                _EVAL_TRACES_DIR.mkdir(parents=True, exist_ok=True)
                trace = {
                    "agent_name": agent_name,
                    "task_sent": task_str,
                    "response": result_text,
                    "latency_ms": latency_ms,
                }
                trace_path = _EVAL_TRACES_DIR / f"{_uuid.uuid4().hex}.json"
                trace_path.write_text(json.dumps(trace, indent=2))
            except Exception as _te:
                logger.debug("Eval trace write failed: %s", _te)

        return result_text

    def _extract_terminal_result(
        self, status_update: Any
    ) -> str:
        state = status_update.status.state
        if state == TaskState.TASK_STATE_FAILED:
            msg = (
                status_update.status.message.text
                if status_update.status.HasField("message")
                and status_update.status.message.parts
                else None
            )
            return json.dumps(
                {"error": f"Agent failed: {msg or 'Unknown'}"}
            )
        if state == TaskState.TASK_STATE_INPUT_REQUIRED:
            msg = (
                status_update.status.message.text
                if status_update.status.HasField("message")
                and status_update.status.message.parts
                else None
            )
            return json.dumps(
                {
                    "input_required": msg or "Additional input needed",
                }
            )
        # COMPLETED — extract message text if present
        if status_update.status.HasField("message"):
            return get_message_text(status_update.status.message)
        return ""

    def _extract_task_result(self, task: Any) -> str:
        state = task.status.state
        if state == TaskState.TASK_STATE_FAILED:
            msg = (
                task.status.message.text
                if task.status.HasField("message")
                and task.status.message.parts
                else None
            )
            return json.dumps(
                {"error": f"Agent failed: {msg or 'Unknown'}"}
            )

        # Data parts from task artifacts
        for art in task.artifacts:
            data_parts = get_data_parts(art.parts)
            if data_parts:
                return json.dumps(data_parts[0])

        # Text from task artifacts
        for art in task.artifacts:
            art_text = get_artifact_text(art)
            if art_text:
                return art_text

        # Message from task status
        if task.status.HasField("message"):
            return get_message_text(task.status.message)

        return json.dumps({"id": task.id, "state": state})
