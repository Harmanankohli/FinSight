# API Reference

Complete reference for all HTTP endpoints exposed by FinSight services.

## Orchestrator (port 8001)

The orchestrator is the primary entry point. It exposes A2A protocol endpoints, REST API routes for the frontend, AG-UI streaming endpoints, and health checks.

### Health

```
GET /health
```

**Response**: `200 OK`

```json
{"status": "ok", "agent": "orchestrator"}
```

---

### A2A Protocol

#### Agent Card Discovery

```
GET /.well-known/agent-card.json
```

Returns the orchestrator's A2A agent card (protobuf-encoded JSON). Used by `A2ACardResolver` for standard agent discovery.

**Response**: `200 OK` — AgentCard JSON with name, description, version, capabilities, supported interfaces, and skills.

#### JSON-RPC Endpoint

```
POST /a2a
Headers: A2A-Version: 1.0
```

Standard A2A JSON-RPC endpoint for inter-agent communication. Accepts `SendMessage` method calls from sub-agents or external A2A clients.

**Request body**:

```json
{
  "jsonrpc": "2.0",
  "method": "SendMessage",
  "id": "abc123",
  "params": {
    "message": {
      "messageId": "def456",
      "role": "ROLE_USER",
      "parts": [{"text": "Should I invest in NVDA?"}]
    }
  }
}
```

**Streaming response**: Yields `task`, `status_update`, and `artifact_update` events. Intermediate `SUBMITTED`/`WORKING` events are non-terminal; only `artifact_update` data and terminal `COMPLETED`/`FAILED` states carry results.

---

### AG-UI Endpoints

#### AG-UI Bridge (Production)

```
POST /a2a-agui
Content-Type: application/json
```

Primary endpoint for the Next.js CopilotKit frontend. Streams AG-UI-compatible events through the ADK runner with full guardrails (off-topic filter, today's brief cache, memory context injection, sub-agent tracking).

**Request body** (`RunAgentInput`):

```json
{
  "thread_id": "optional-thread-id",
  "run_id": "optional-run-id",
  "messages": [
    {"role": "user", "content": "Analyze NVDA for long-term hold"}
  ],
  "forwarded_props": {"user_id": "user-123"}
}
```

**Headers**:
- `X-FinSight-User-Id` (optional) — user identity; falls back to `forwarded_props.user_id`, then auto-generated `anon-{uuid}`
- `Content-Type: application/json`

**Response**: `200 OK` — `text/event-stream` (SSE)

Streams AG-UI events:
- `RunStartedEvent` — run begins
- `StateSnapshotEvent` — initial state `{"active_agents": [], "active_agent": null}`
- `StepStartedEvent` — orchestrator step begins
- `StateDeltaEvent` — updates `active_agents` array as sub-agents are invoked
- `ToolCallStartEvent` / `ToolCallArgsEvent` / `ToolCallEndEvent` — sub-agent delegation events
- `ToolCallResultEvent` — sub-agent response received
- `TextMessageStartEvent` / `TextMessageContentEvent` / `TextMessageEndEvent` — streaming synthesis
- `RunFinishedEvent` — run completes

**CORS**: Allowed origins `http://localhost:3000`, `http://127.0.0.1:3000`.

#### AG-UI Eval Endpoint

```
POST /agentic_chat
Content-Type: application/json
```

Simplified AG-UI endpoint for RAGAS evaluation. Runs the orchestrator without guardrails or memory injection — useful for offline batch evaluation.

**Request body**: Same `RunAgentInput` format as `/a2a-agui`.

**Response**: `200 OK` — `text/event-stream` (SSE). Same event types as `/a2a-agui` but without state tracking or auto-save.

---

### REST API Routes

All REST routes are read-only JSON endpoints. User identity is read from the `X-FinSight-User-Id` request header.

#### Memory — Ticker Briefs

```
GET /api/memory/ticker/{symbol}
GET /api/memory/ticker/{symbol}/latest
GET /api/memory/ticker/{symbol}/changed
```

| Endpoint | Parameters | Returns |
|---|---|---|
| `/api/memory/ticker/{symbol}` | `?limit=10` (query) | Array of brief objects (history) |
| `/api/memory/ticker/{symbol}/latest` | — | Single brief object or `404` |
| `/api/memory/ticker/{symbol}/changed` | — | `{"changed": bool, "direction": "upgrade"|"downgrade"|"unchanged"}` or `{"changed": false, "reason": "insufficient_history"}` |

**Brief object shape**:

```json
{
  "id": 1,
  "ticker": "NVDA",
  "recommendation": "BUY",
  "confidence": 0.85,
  "response_text": "Full analysis text...",
  "analysis_date": "2026-06-07",
  "created_at": "2026-06-07T14:30:00+05:30"
}
```

#### Sessions

```
GET /api/sessions
GET /api/sessions/{id}/events
```

| Endpoint | Parameters | Returns |
|---|---|---|
| `/api/sessions` | `?user_id=...` (query, optional) | Array of session objects, max 50, sorted by `last_update_time` DESC |
| `/api/sessions/{id}/events` | — | `{"session_id": "...", "events": [...]}` or `404` |

**Session object shape**:

```json
{
  "id": "session-uuid",
  "user_id": "user-123",
  "app_name": "orchestrator",
  "last_update_time": "2026-06-07T14:30:00",
  "event_count": 12
}
```

**Event object shape**:

```json
{
  "id": "event-uuid",
  "author": "orchestrator",
  "timestamp": "2026-06-07T14:30:00",
  "content": [
    {"type": "text", "text": "BUY recommendation for NVDA..."},
    {"type": "function_call", "name": "send_message", "args": {"agent_name": "rag", "task": "..."}},
    {"type": "function_response", "name": "send_message", "response": "..."}
  ]
}
```

#### Agents

```
GET /api/agents
GET /api/agents/{name}/health
```

| Endpoint | Returns |
|---|---|
| `/api/agents` | Array of discovered sub-agent objects with `name`, `description`, `skills`, `url` |
| `/api/agents/{name}/health` | Fan-out health check: `{"status": "ok"|"degraded"|"unreachable", "detail": {...}}` |

#### Reports

```
GET /api/reports/ticker/{symbol}/latest/{format}
GET /api/reports/{brief_id}/{format}
```

| Endpoint | Parameters | Returns |
|---|---|---|
| `/api/reports/ticker/{symbol}/latest/{format}` | `format`: `pptx`, `docx`, or `html` | Binary file download or HTML string |
| `/api/reports/{brief_id}/{format}` | `format`: `pptx`, `docx`, or `html` | Binary file download or HTML string for specified brief |

**Response headers**:

```
Content-Type: application/vnd.openxmlformats-officedocument.presentationml.presentation (PPTX)
              application/vnd.openxmlformats-officedocument.wordprocessingml.document (DOCX)
              text/html (HTML)
Content-Disposition: attachment; filename="FinSight_{ticker}_{date}.{format}" (PPTX/DOCX)
```

**Report format endpoints** are served via `generate_pptx()`, `generate_docx()`, and `generate_html()` in the `shared/reports/` package (split from `shared/report_generator.py` in v1.41; the monolithic shim was removed in v2.0). The HTML format uses a Jinja2 template (`shared/templates/investment_deck.html`) with an embedded `deck-stage.js` web component for slide navigation. All three formats share a common `_extract_deck_data()` extraction pipeline in `shared/reports/extraction.py`.

---

### Static Files

```
GET /reports/{filename}
```

Serves generated report files (PPTX/DOCX) from `db/reports/` as static downloads.

---

## Sub-Agent Servers

Each sub-agent exposes the same endpoint pattern.

### Endpoints (ports 8002–8004)

| Endpoint | Port 8002 (RAG) | Port 8003 (Quant) | Port 8004 (Market Context) |
|---|---|---|---|
| `GET /health` | `{"status":"ok","agent":"rag"}` | `{"status":"ok","agent":"quant"}` | `{"status":"ok","agent":"market_context"}` |
| `POST /a2a` | A2A JSON-RPC | A2A JSON-RPC | A2A JSON-RPC |
| `GET /.well-known/agent-card.json` | Agent card | Agent card | Agent card |
| `POST /release-evals` | `{"released": N}` | `{"released": N}` | `{"released": N}` |

#### Release Evals

```
POST /release-evals
```

Triggers deferred RAGAS evaluation coroutines. Called by the orchestrator's `after_agent_callback` after synthesis completes. Includes a 120s safety-net auto-release if the orchestrator crashes.

**Response**: `200 OK`

```json
{"released": 3}
```

---

## MCP Server (port 8010)

### Health

```
GET /health
```

**Response**: `200 OK`

```json
{"status": "ok", "agent": "mcp"}
```

### MCP Tools

The MCP server exposes tools via SSE transport at `/sse`. Tools are consumed by agents via the MCP client library, not via direct HTTP calls. See [MCP_SERVERS.md](MCP_SERVERS.md) for the full tool catalog.

### Agent Registry

| Endpoint | Description |
|---|---|
| `find_agent(query)` | Semantic search via embedding dot-product over agent cards |
| `resource://agent_cards/list` | Lists all available agent card URIs |
| `resource://agent_cards/{name}` | Retrieves specific agent card |

---

## Next.js Frontend (port 3000)

### API Routes

| Route | Method | Purpose |
|---|---|---|
| `/api/copilotkit` | POST | CopilotKit runtime — proxies to orchestrator `/a2a-agui` via AG-UI protocol |
| `/api/traces` | GET | Langfuse trace proxy — `?traceId=X` for single trace, omit for list |
| `/api/health` | GET | Backend health proxy — `?svc=orchestrator\|rag\|quant\|market\|mcp` |

### Rewrites

| Pattern | Target | Purpose |
|---|---|---|
| `/api/orch/:path*` | `http://localhost:8001/:path*` | Transparent proxy to orchestrator REST API |
| `/reports/:path*` | `http://localhost:8001/reports/:path*` | Report file download proxy |

---

## Error Responses

All endpoints return errors as JSON:

```json
{"error": "Description of the error"}
```

Standard HTTP status codes:

| Code | Meaning |
|---|---|
| `200` | Success |
| `400` | Bad request (invalid parameters, missing body) |
| `404` | Resource not found (ticker, session, brief) |
| `500` | Internal server error |

---

## Authentication

FinSight implements two authentication modes, controlled by `AUTH_ENABLED` in `.env`:

### Auth Disabled (`AUTH_ENABLED=false`, default)

User identity is read from the `X-FinSight-User-Id` request header — a convention for multi-user differentiation without security. Falls back to `forwarded_props.user_id` in AG-UI requests, then auto-generated `anon-{uuid}`. Suitable for local/single-user deployment.

### Auth Enabled (`AUTH_ENABLED=true`)

Full bearer JWT authentication with three principal kinds:

| Kind | Token | Routes |
|---|---|---|
| **User** | JWT from `/auth/login` (Argon2 password) | Orchestrator `/api/*`, AG-UI bridge, reports |
| **Service** | Static `SERVICE_AUTH_TOKEN` env var | A2A `/a2a`, `/release-evals`, MCP SSE |
| **Public** | None | `/health`, `/.well-known/*`, `/auth/login\|refresh\|logout` |

#### Login

```
POST /auth/login
Content-Type: application/json

{"username": "admin", "password": "secret123"}
```

**Response**: `200 OK`

```json
{
  "access_token": "eyJhbG...",
  "expires_in": 900,
  "token_type": "Bearer"
}
```

Also sets `refresh_token` httpOnly cookie (7-day TTL, rotated on use).

#### Refresh

```
POST /auth/refresh
Cookie: refresh_token=<token>
```

**Response**: `200 OK` — same response shape as login, new access token + rotated refresh cookie.

#### Logout

```
POST /auth/logout
Cookie: refresh_token=<token>
```

**Response**: `200 OK` — deletes refresh token from DB.

#### Rate-Limited Lockout

- 5 failed login attempts per username+IP → 60s cooldown
- IP from direct socket address unless peer is in `TRUSTED_PROXIES` (Docker Compose: set to Next.js container IP)
- **Known tradeoff**: username-keyed lockout allows DoS of a known username; accepted for single-host deployment

#### Token Generation

```bash
# User JWT signing secret (32+ hex characters)
openssl rand -hex 32

# Service-to-service shared secret (16+ characters)
openssl rand -hex 16
```

---

## CORS

The orchestrator allows cross-origin requests from:

- `http://localhost:3000`
- `http://127.0.0.1:3000`

Allowed methods: `GET`, `POST`, `OPTIONS`. Exposed headers: `X-FinSight-User-Id`.

---

## Rate Limiting

The MCP server applies internal rate limits to upstream data sources:

| Source | Rate | Burst | Applies to |
|---|---|---|---|
| SEC EDGAR | 8 req/s | 10 | Ticker map, submissions, filing content, search |
| yfinance | 4 req/s | 8 | Prices, financials, options, earnings |
| RSS feeds | 2 req/s | 4 | News sentiment, Yahoo fallback |

These limits are applied per-process via `TokenBucket` in `shared/rate_limiter.py` and are not exposed to API consumers.
