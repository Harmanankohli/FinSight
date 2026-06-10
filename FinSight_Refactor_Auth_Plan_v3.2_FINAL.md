# FinSight — Refactoring, Authentication & Report-Quality Implementation Plan (v3.2 — FINAL)

**Audience:** autonomous coding agents. Each work package (WP) is self-contained: objective, files, steps, skeletons at integration points, named tests, acceptance criteria. **This document supersedes v1, v2, v3, and v3.1 — delete them. It references no other plan document.**

**Changes from v3.1 (edge-case review — items E1–E4 verified by execution on 2026-06-10):**
1. **E4 (security) — empty-credential hole in the auth spec.** With `AUTH_ENABLED=true` but `SERVICE_AUTH_TOKEN` unset (legal in dev per v3.1, which validated secrets only in production), `"".split(",") == ['']` and `hmac.compare_digest("","") == True` → a request with an empty bearer authenticates as a service. Fixed: WP 1.5 validates secrets whenever `auth_enabled` (any env) with minimum lengths; WP 2.1 middleware rejects empty/short credentials outright.
2. **E1 — R.2 `_clean_item` leaked bare price figures:** `Bull case: $201.80.` yielded the item `'$201.80'` (the label is consumed by `_case_block`, so the `^(?:bull|bear) case` reject never fires), contradicting R.2's own test. Fixed with a bare-figure reject pattern.
3. **E2 — R.2 dropped bulleted cases entirely:** `Bull case:\n- item\n- item` returned zero items (every part contains `\n` or starts with `-`). Fixed: bullet-shaped blocks route through `_extract_bullets` before cleaning.
4. **Build-system gap in WP 1.8:** the repo's `pyproject.toml` has no explicit `[tool.setuptools]` packages table; with multiple flat top-level dirs, `pip install ".[svc]"` fails setuptools auto-discovery (this is precisely why agent-2's existing `pip install -e .` always falls to its fallback list). WP 1.8 now adds the explicit packages table as step 0.
5. Smaller hardening notes folded into WPs: R.1 ticker-class regex (`BRK.B`-style symbols) and peer/generic degradation rule; R.3 `fit_text` explicit-newline and no-sentence-boundary handling; 2.2 lockout-behind-proxy, refresh-rotation race, and CSRF notes; 2.3 G20 ordering dependency on 2.4; new §4 Edge-Case Register for cross-cutting cases every agent must respect.

---

## 0. How to use this plan

### 0.a Conventions

1. Branch per WP: `refactor/<wp-id>-<slug>`; one PR per WP.
2. **Definition of done (every WP):** `ruff check` clean on touched files; `mypy` clean on touched packages (legacy escape hatch: file-level `# mypy: ignore-errors` + `TODO(wp-id)` noted in PR); `pytest -m "not integration and not external"` green; `.env.example` / `docs/SECURITY.md` / `docs/ARCHITECTURE.md` updated when the WP changes config or security posture; `docs/CHANGELOG.md` entry always.
3. **Behavior freeze** in Phase 0/1 WPs: no HTTP response shape, A2A event sequence, or file output changes unless the WP says so. Characterization tests are the referee.
4. Module moves leave deprecation shims (old path re-exports + `DeprecationWarning`); shims removed only in WP 3.5.
5. **Error envelope** (introduced WP 1.6, reused by auth): `{"error": {"code": "<CODE>", "message": "..."}}` — `UNAUTHENTICATED` 401, `FORBIDDEN` 403, `NOT_FOUND` 404, `VALIDATION_ERROR` 400.
6. **Auth flag matrix:**

| `AUTH_ENABLED` | User endpoints | A2A JSON-RPC | MCP SSE | Identity source |
|---|---|---|---|---|
| `false` (default) | open | open | open | `X-FinSight-User-Id` header (dev convention) |
| `true` | user JWT required | service bearer required | service bearer required | verified JWT `sub` only |

Public allowlist in both modes: `/health`, paths starting `/.well-known/` (A2A discovery requires a readable card; the card *advertises* the scheme).

### 0.b Environment matrix (final state)

```
ENV=development|production                      # WP 1.5
FINSIGHT_DB_PATH=./db/finsight_memory.db        # WP 1.8
AUTH_ENABLED=false                              # WP 2.1
AUTH_JWT_SECRETS=<hex>[,<old-hex>]              # WP 2.1 (first signs; rest verify-only)
AUTH_ACCESS_TTL_SECONDS=900                     # WP 2.1
AUTH_REFRESH_TTL_SECONDS=1209600                # WP 2.2
SERVICE_AUTH_TOKEN=<hex32>                      # WP 2.1
LOGIN_MAX_ATTEMPTS=5                            # WP 2.2 (per user+IP / 15 min)
SANDBOX_MODE=ast|container|disabled             # WP 2.6
REPORT_PLACEHOLDER_POLICY=label|omit            # WP R.3
REPORTS_OFFLINE=false                           # WP R.4 (true disables yfinance in report gen; CI sets true)
```

---

## 1. Gap Review (verified against code and runtime on 2026-06-10)

✅ = confirmed (tests run / decks generated / lines read); ◐ = strong static evidence, runtime-verify in owning WP.

| # | Gap | Evidence | St | Sev | Owner |
|---|---|---|---|---|---|
| G1 | **All five Docker images have dependency drift; at least four cannot run.** MCP image lacks `langfuse` (top-level import in `finsight_server.py`), `pyyaml`, `aiosqlite`. All three sub-agent images lack `mcp` while every `executor.py` imports `shared.mcp_client`; agents 2–4 also lack `aiosqlite` (`SQLiteTaskStore`), `python-dotenv`, `langfuse`. Agent-2 runs `pip install -e .` before source is copied → always falls to a hand-list that also lacks `mcp`. | Dockerfiles vs import grep | ✅ | **Crit** | 1.8 |
| G19 | **Compose healthchecks use `curl`; `python:3.12-slim` doesn't ship it.** All five (`docker-compose.yml:22-84`) report unhealthy forever; any `service_healthy` dependency deadlocks. | compose file | ✅ | High | 1.8 |
| G2 | `finsight_memory:/data` volume dead: `DB_PATH` hardcoded repo-relative (`shared/memory/store.py:18`). | code | ✅ | High | 1.8 |
| G3 | Cross-container shared-SQLite assumption (every service builds `SQLiteTaskStore()` on one path). | code | ✅ | Med | 1.8 |
| G4 | Cross-user leak in today's-brief fast-path: `agui_bridge._get_today_cached_text()` → `get_latest(ticker, user_id=None)`; ticker-memory routes unscoped. | code | ✅ | High | 2.3 |
| G20 | **Semantic cache keyed by query+date only** (`shared/semantic_cache.py`, collection `query_cache`); with `SEMANTIC_CACHE_ENABLED=true`, user A's answer can serve user B. | code | ✅ | High | 2.3 |
| G5 | Off-topic guardrail regex duplicated and drifted (`agui_bridge.py` vs `agent_executor.py`). | code | ✅ | Low | 1.7 |
| G6 | `_TODAY` evaluated at import (`agent_1_adk/agent.py:25`) — stale after midnight. | code | ✅ | Med | 1.7 |
| G7 | Import-time env mutation `OPENAI_API_BASE/KEY` (`agent.py:20-21`). | code | ✅ | Low | 1.5 |
| G8 | Operator page health-checks service URLs from the browser (`operator/page.tsx:25`). | code | ✅ | Med | 2.2 |
| G9 | No `.dockerignore`; real `.env` would bake into images. | repo | ✅ | High | 1.8 |
| G10 | No body-size limits/timeouts; SSE must reject *before* the stream starts. | design | — | Med | 2.1 |
| G11 | **`has_changed()` labels inverted — confirmed at `ticker_memory.py:210-211`:** `old_val = latest`, `new_val = previous` (swapped; `changed` bool coincidentally right). Consumers: `format_context`, `/api/memory/ticker/{s}/changed`. | lines read | ✅ | Med | 1.6 |
| G12 | `POST /release-evals` unauthenticated state-changing endpoint on every agent. | code | ✅ | Med | 2.4 |
| G13 | a2a-sdk `OwnerResolver`/`resolve_user_scope` hook exists in `SQLiteTaskStore` — auth must use it, not invent parallel scoping. | code | ✅ | Med | 2.4 |
| G14 | Test seams poke singletons (`conftest.py` swaps `store_mod._db_conn`); refactors must ship `reset_for_tests()` helpers. | code | ✅ | Med | 1.5/1.6 |
| G15 | No schema-migration path; new tables need a version-bump mechanism. | code | ✅ | Med | 2.2 |
| G16 | Token lifecycle unspecified: refresh rotation, reuse detection, logout, login lockout. | design | — | Med | 2.2 |
| G17 | Next proxy is the auth chokepoint. **Verified:** `next.config.ts` rewrites `/api/orch/:path*` **and `/reports/:path*`** — the auth middleware matcher must include both. | code | ✅ | — | 2.2 |
| G18 | `@logged` decorators will log Authorization headers once auth lands — redaction filter required. | code | ✅ | High | 2.1 |
| P1–P6 | Report-generation defects — reproduced by deck generation; details in Phase R. | runtime | ✅ | High | R |

Consciously out of scope (record in SECURITY.md future work): mTLS between services; SQLite→Postgres migration.

**Stale-doc correction:** `ppt-generation-fix.md` is **already fully applied** in the code (markers: `[:1200]` at extraction lines 295/428/431, `thesis_h = 4.2` + 20pt at 1071-1078, bear card at 1182, relaxed peer guard at 1233-1240). Its line tables no longer match the file. WP 1.2 verifies-and-deletes it; nobody re-applies it.

---

## Phase 0 — Baseline & Safety Net

### WP 0.1 — CI pipeline
**Files:** `.github/workflows/ci.yml`, `Makefile`, `pyproject.toml` (mypy scoping).
**Steps:** jobs = `ruff check .`; `mypy shared agent_1_adk` (scoped now, ratchet per-package later via overrides); `pytest -m "not integration and not external" --cov=shared --cov=agent_1_adk`; frontend `npm ci && npm run lint && npx tsc --noEmit`; `docker build` matrix for all five images (`continue-on-error: true` + tracking issue until WP 1.8, then required). `Makefile` targets `lint/type/test/ci` mirror CI.
**Accept:** CI runs on main; non-docker jobs green.

### WP 0.2 — Characterization tests
**Files:** `tests/characterization/{test_mcp_tool_shapes,test_deck_extraction_golden,test_quant_nodes_io,test_api_contracts}.py` + `fixtures/`.
**Steps:**
1. *MCP shapes:* for each `@app.tool()`-registered function, monkeypatch upstreams (`yf.Ticker`, `httpx`, `feedparser.parse`) with fixtures; assert top-level keys/types only.
2. *Deck goldens:* run `_extract_deck_data()` over **four** fixture briefs — `minimal.json`, `structured.json`, `empty.json`, and `realistic_quant.json`. The realistic fixture mirrors real `llm_summary_node` output: `## Investment Recommendation` heading; prose KPIs; `DCF fair value: $X. Analyst price target: $Y.`; `p10=$A, p50=$B, p90=$C`; same-line `Bull case: $X. Bear case: $Y.`; a peer markdown table (`| Metric | TICK | PEER1 | PEER2 |`); **a non-peer table** (`| Metric | Current | YoY Change |`); a `## Key Risks` bullet list. **It currently reproduces P1 and P2 — golden it as-is** so Phase R's diffs are explicit. Compare via sorted-key JSON dump with an `--update-goldens` env flag.
3. *Quant nodes:* per-node state-in/state-out tests with stubbed `get_shared_mcp()`; extend `tests/unit/test_quant_graph_nodes.py`.
4. *API contracts:* `httpx.AsyncClient(transport=ASGITransport(app=orchestrator_app))` + temp DB seeded via `TickerMemory.save()`; assert status + key/type schema for all routes in `get_api_routes()` and `/health`.
**Accept:** moving any covered function without a re-export fails a test; suite < 60s, zero network.

### WP 0.3 — Dependency hygiene
Remove or quarantine to `[project.optional-dependencies] future` (verify by grep + suite): `llama-index-vector-stores-pinecone`, `llama-index-embeddings-bedrock`, `streamlit`, `psycopg2-binary`, `boto3`, `litellm`, `langsmith`. Upper-bound `crewai`, `langgraph`, `a2a-sdk` at current major.
**Accept:** fresh `uv pip install -e ".[dev]"` + unit suite green.

---

## Phase 1 — Structural Refactor

Order: **1.5 → {1.1, 1.2, 1.3, 1.4, 1.8} parallel → 1.6 → 1.7**.

### WP 1.5 — Configuration overhaul (pydantic-settings + bootstrap)
**Files:** `shared/settings.py` (new), `shared/bootstrap.py` (new), `shared/config.py` (becomes shim), every entrypoint (`agent_1_adk/main.py`, three `server.py`, `mcp_servers/finsight_server.py`, `agents/finsight_agent/agent.py`), `tests/conftest.py`.
**Skeleton:**
```python
# shared/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    env: str = "development"
    llm_model: str = "qwen/qwen3-30b-a3b-2507"
    llm_base_url: str = "http://localhost:1234/v1"
    llm_api_key: str = "lmstudio"
    # ... mirror EVERY constant in current config.py, same env names via Field(alias=...)
    # incl. the A2A_TIMEOUT_SENTIMENT back-compat alias (model_validator)
    finsight_db_path: str = "./db/finsight_memory.db"          # G2
    agent_port_rag: int = 8002; agent_port_quant: int = 8003; agent_port_market: int = 8004
    auth_enabled: bool = False
    auth_jwt_secrets: str = ""        # comma-separated; first signs
    auth_access_ttl_seconds: int = 900
    auth_refresh_ttl_seconds: int = 1_209_600
    service_auth_token: str = ""
    login_max_attempts: int = 5
    sandbox_mode: str = "ast"
    report_placeholder_policy: str = "label"
    reports_offline: bool = False
    langfuse_public_key: str | None = None   # was "pk-lf-..." placeholder — now None
    langfuse_secret_key: str | None = None

    def validate_runtime(self) -> None:
        problems: list[str] = []
        if self.auth_enabled:                      # E4: any env, not just production
            signing = self.auth_jwt_secrets.split(",")[0] if self.auth_jwt_secrets else ""
            if len(signing) < 32:
                problems.append("AUTH_ENABLED=true requires AUTH_JWT_SECRETS (first key >= 32 chars)")
            if len(self.service_auth_token) < 16:
                problems.append("AUTH_ENABLED=true requires SERVICE_AUTH_TOKEN (>= 16 chars)")
        if self.env == "production":
            if not self.auth_enabled:
                problems.append("ENV=production requires AUTH_ENABLED=true")
            if "dev-mode" in self.sec_user_agent:
                problems.append("SEC_USER_AGENT placeholder in production")
        if problems:
            raise EnvironmentError("FinSight config errors:\n" + "\n".join(f"  - {p}" for p in problems))

_settings: Settings | None = None
def get_settings() -> Settings:
    global _settings
    if _settings is None: _settings = Settings()
    return _settings

def reset_settings_for_tests() -> None:    # G14 test seam
    global _settings; _settings = None
```
```python
# shared/bootstrap.py — ALL former import-time side effects, exactly once per process
def bootstrap(service_name: str) -> Settings:
    s = get_settings()
    os.environ.setdefault("HF_HUB_OFFLINE", "1" if s.hf_hub_offline else "0")
    os.environ.setdefault("OPENAI_API_BASE", s.llm_base_url)   # G7 (from agent.py)
    os.environ.setdefault("OPENAI_API_KEY", s.llm_api_key)
    _reconfigure_stdio_utf8()      # Windows cp1252 guard (from config.py)
    _set_win_event_loop_policy()   # from the 5 entrypoints
    s.validate_runtime()
    setup_file_logging(service_name)
    init_langfuse(service_name)    # no-op when keys are None
    return s
```
**Steps:** build `Settings` field-by-field from current `config.py`; `config.py` becomes constants re-exported from `get_settings()` (snapshot-at-first-import = today's behavior; removal in 3.5); insert `bootstrap("<svc>")` as the first statement after stdlib imports in each entrypoint and delete the per-file blocks it replaces; add `reset_settings_for_tests()` to the autouse conftest fixture.
**Tests:** `test_settings.py` — env > .env > default precedence; alias back-compat; `validate_runtime` matrix.
**Accept:** all services boot with no `.env`; `ENV=production AUTH_ENABLED=false` exits non-zero with readable message; no module-level `os.environ` access outside `settings.py`/`bootstrap.py`. **Risk note:** `bootstrap()` must run before framework imports trigger model loads or asyncio use — verify import order in every entrypoint including the `adk web` path.

### WP 1.1 — Split the MCP god module
**Layout:** `mcp_servers/finsight_server.py` (composition root < 150 lines; **keep `get_app()` — Dockerfile CMD references `mcp_servers.finsight_server:get_app`**) + `tools/{prices,financials,filings,news,market,tickers,registry,python_exec}.py` + `infra/{caching,limits}.py`.
**Steps:** move by responsibility along the file's existing section comments; tool modules export plain async functions, decorators applied at registration (`app.tool()(prices.get_prices)`) so leaves test without FastMCP; lazy accessors in `infra/` (`get_sec_limiter()`, `get_embed_model()`); no import-time bucket/model construction.
**Accept:** WP 0.2 shape tests pass (add re-exports if they imported privates); no `tools/` module > 400 lines; `python -c "import mcp_servers.finsight_server"` < 2s with `HF_HUB_OFFLINE=1` (asserted in a test).

### WP 1.2 — Split the report generator (mechanical only)
**Layout:** `shared/reports/{__init__,extraction,deck_model,pptx_renderer,docx_renderer,html_renderer}.py` + `templates/report.html.j2`; `shared/report_generator.py` becomes shim.
- `extraction.py`: `_extract_deck_data`, `_enrich_from_markdown`, `_parse_markdown_tables/sections`, `_extract_bullets`, `_resolve_ticker_info`, regex helpers. `deck_model.py`: move `DeckData` unchanged (Phase R may evolve it).
- **Behavioral fixes belong to Phase R, not here.**
- Verify `ppt-generation-fix.md` is applied (grep the six markers listed in the Gap Review) → `git rm ppt-generation-fix.md` + CHANGELOG.
**Accept:** WP 0.2 goldens byte-identical; all 20 regression tests green; old imports work via shim.

### WP 1.3 — Split the Quant graph nodes
`agent_3_langgraph/nodes/` by stage (`data_fetch,technical,dcf,monte_carlo,portfolio,summary`) + pure `calculations.py` (zero LangGraph/MCP/LLM imports); `nodes/__init__.py` re-exports for `graph.py`/tests.
**Accept:** existing node + parallel-dispatch tests pass; no node module > 350 lines; `calculations.py` ≥ 90% coverage with seeded-RNG deterministic tests (hand-computed DCF fixture).

### WP 1.4 — Sub-agent server factory
**Files:** `shared/agent_server.py` (new), three `server.py`, settings ports.
**Skeleton:**
```python
def build_agent_app(*, agent_card: AgentCard, agent: BaseAgent, service_name: str,
                    on_startup: Sequence[Callable] = (), extra_routes: Sequence[Route] = ()) -> Starlette:
    settings = bootstrap(service_name)
    handler = DefaultRequestHandler(agent_executor=GenericAgentExecutor(agent),
                                    task_store=SQLiteTaskStore(), agent_card=agent_card)
    routes = [Route("/health", _health(service_name)),
              Route("/release-evals", _release_evals, methods=["POST"]), *extra_routes]
    routes += create_agent_card_routes(agent_card)
    routes += create_jsonrpc_routes(handler, "/a2a")
    return Starlette(routes=routes, on_startup=list(on_startup),
                     debug=settings.env != "production",
                     middleware=build_auth_middleware(settings))
```
**Phase-1 stub:** `build_auth_middleware(settings) -> list` lives in `shared/agent_server.py` itself and returns `[]`; WP 2.1 moves it to `shared/auth/middleware.py` and re-exports from the old location.
Each agent `server.py` reduces to: card (URL/port from settings — no `8002/8003/8004` literals) + agent instance + factory call + `__main__`. RAG passes `on_startup=[_prewarm]`.
**Tests:** factory unit test (routes present); integration discovery via `SubAgentClient.discover()` against in-process apps (`httpx.ASGITransport` injected into the resolver's client).
**Accept:** each `server.py` < 120 lines; route tables identical modulo card content.

### WP 1.6 — Data-access layer + REST hardening (incl. G11)
**Files:** `shared/memory/session_repo.py` (new), `agent_1_adk/api_routes.py`, `agent_1_adk/sub_agent_client.py`, `shared/memory/ticker_memory.py`, `shared/models.py`.
**Steps:**
1. `SessionRepo.list_sessions(user_id, limit)` — one JOIN+aggregate query (kills the N+1 per-session event count); `get_events(session_id)`; event part-flattening (`functionCall`/`functionResponse`) as a pure `_flatten_event(dict)` helper with unit tests. ADK-sessions DB gets its own singleton accessor in the repo module. Repos use `get_db()` + `write_lock()` — **no** per-request `aiosqlite.connect`.
2. Public `SubAgentClient.get_agent_base_url(name) -> str | None`; delete the `_client._agents` poke; enable ruff `SLF001` for `agent_1_adk/`.
3. Input validation: clamp+parse `limit` (400 envelope on garbage); ticker path params `^[A-Z0-9.^-]{1,10}$`.
4. **G11:** swap `ticker_memory.py:210-211` → `old_val = previous.get(field)`, `new_val = latest.get(field)`. Audit both consumers for compensating inversions; normalize in one PR. Regression test: history `[HOLD(newer), BUY(older)]` → `{"old":"BUY","new":"HOLD","changed":True}`.
5. Route handlers: parse → repo/service → serialize; error envelope everywhere.
**Accept:** contract tests pass (success paths byte-compatible; error paths move to envelope — update goldens, note in PR); `grep -rn "aiosqlite.connect" agent_1_adk/` empty.

### WP 1.7 — Production-readiness sweep
1. `debug=True` → `settings.env != "production"` (factory covers agents; fix orchestrator `main.py`).
2. **G5:** `shared/guardrails.py` with `is_off_topic(text)` (union of both regexes) + `extract_ticker(text)`; both consumers import it; delete locals.
3. **G6:** module-level `_TODAY` → `def _today(): return datetime.now(IST).date().isoformat()` at use sites.
4. Report filename safety: validated ticker + `re.sub(r"[^A-Za-z0-9._-]","-",analysis_date)`; any filesystem path asserted `resolve().is_relative_to(reports_dir)`.
5. Silent `except Exception: pass` in `shared/` → logged (`logger.debug(..., exc_info=True)` min); enable ruff `S110`,`BLE001` on `shared/`.
6. `agents/` shim: remove `sys.path.insert` hacks (package installed editable; absolute imports); manual `adk web` check documented in PR.
**Accept:** `grep -rn "debug=True"` empty; one guardrail implementation; hostile-filename tests pass.

### WP 1.8 — Docker & data-layer correctness (G1, G2, G3, G9, G19)
1. **`.dockerignore`:** `.env*`, `db/`, `logs/`, `node_modules/`, `web/`, `.git/`, `tests/`, `docs/`, `*.bat` (G9).
2. **Per-service extras** in pyproject (mandated; drift too severe for hand-lists): **step 0 — add an explicit packages table first**, because the repo has multiple flat top-level dirs and no `[tool.setuptools]` config, so any `pip install .` fails setuptools auto-discovery (verified — this is exactly why agent-2's existing `pip install -e .` always falls to its fallback list): `[tool.setuptools.packages.find] include = ["shared*","agent_1_adk*","agent_2_llamaindex*","agent_3_langgraph*","agent_4_crewai*","mcp_servers*","agents*"]`. Then define extras `orchestrator`, `rag`, `quant`, `market`, `mcp_server` — derived by grepping each service's + `shared/`'s imports; every list includes `mcp`, `aiosqlite`, `python-dotenv`, `langfuse` (shared is copied everywhere). Dockerfiles: `COPY pyproject.toml README.md ./` → `pip install --no-cache-dir ".[<svc>]"` → `COPY <svc>/ shared/ ...` (cached deps layer; the wheel built at install time is effectively empty since source isn't copied yet — services run from `WORKDIR /app` source, which is fine). Delete agent-2's pre-source `pip install -e .`.
3. **G19:** every healthcheck → `["CMD","python","-c","import urllib.request; urllib.request.urlopen('http://localhost:<port>/health', timeout=5)"]`; redis healthcheck `["CMD","redis-cli","ping"]`.
4. **G2/G3:** `store.py` reads `settings.finsight_db_path`; compose: orchestrator `FINSIGHT_DB_PATH=/data/finsight_memory.db` + existing volume; agents 2–4 get per-service DBs (`/data/<svc>_state.db`, own volumes). ARCHITECTURE.md: "SQLite is per-process; cross-service state travels only via A2A/MCP."
5. Compose hygiene: `env_file: .env`, `restart: unless-stopped`, `REDIS_URL=redis://redis:6379/0` wired into the MCP service + `depends_on`; optional `web` service (or document why separate).
6. Non-root `USER` (uid 10001) in all Dockerfiles; chown `/data`.
**Tests:** CI docker-build matrix flips to required; `scripts/compose_smoke.sh` — up, wait healthy via `docker compose ps` (exercises G19), hit each `/health`, MCP tool list.
**Accept:** five images build AND start; all healthy; briefs survive orchestrator recreation; MCP logs redis backend.

---

## Phase R — Report / PPT Generation Quality

**Context.** `ppt-generation-fix.md` is already applied. The defects below were **reproduced on 2026-06-10** by generating decks from the `realistic_quant.json` fixture; all 20 regression tests pass while producing them (the suite's fixtures are too sanitized). Post-1.2 the code lives in `shared/reports/`; anchors are code snippets, not line numbers.

| ID | Defect (observed) | Root cause |
|---|---|---|
| P1 | `peer_names == ['AMD','INTC','Current','YoY Change']`; financial-table rows leak into the peer table; with no real peer table the fallback renders "Identified peers: Current, YoY Change". | `_enrich_from_markdown` table loop treats every table as a peer table: `peer_cols = [k for k in row if k not in ("Metric","metric","") and k.upper() != ticker]`; plus `ticker_val = row.get(ticker, row.get("Current", ...))` maps financial columns into peer columns. |
| P2 | Risk–Reward slide shows `Bear case: $96.50` and a megabullet `## Peer Comparison \| ## Financial Performance \| ## Key Risks \| - Customer…` under "Growth Opportunities". | `bull_para` regex stop-tokens require `\n` + bare word, so `## Key Risks` never terminates; same-line `Bear case:` is no boundary → captures to EOF; sentence-split emits markdown debris. Twin `bear_para` has the same flaws. |
| P3 | Empty extractions silently render invented analysis ("Market volatility", "Strong operational execution") indistinguishable from findings. | hardcoded defaults in `_enrich_from_markdown`. |
| P4 | 1,200-char summaries clip off the slide (~950 chars fit at 20pt); `_text` sets `auto_size=None`, no fit logic. | caps vs box-size interaction. |
| P5 | Bull/base/bear/DCF figures appear twice on the valuation slide (table rows + scenario cards). | extraction appends to both `valuation_table` and `scenarios`; renderer draws both. |
| P6 | Live yfinance network call inside report generation (`_resolve_ticker_info`). | direct `yf.Ticker(t).info` with only an in-process cache. |

**Depends:** WP 1.2 (and trivially 1.5 for R.3's setting). R.1 ∥ R.2 ∥ R.3 → R.4 → R.5. Phase R and Phase 2 are independent — run concurrently.

### WP R.1 — Table classification (P1)
1. `_parse_markdown_tables` additionally returns `tables: list[ParsedTable]` (`headers`, `rows`); keep the flat list during this WP for back-compat (removed in R.4). **Land in two commits: (a) per-table parser with flat back-compat, (b) flip consumers** — table-semantics changes shift goldens broadly.
2. `classify_table(headers, ticker) -> "peer"|"financial"|"generic"`: `peer` iff ≥2 non-`Metric` headers match `_TICKER_RE` or one equals `ticker`; `financial` iff headers ⊆ {metric, current, value, yoy change, context} case-insensitively; else `generic` (rendered as an extra-section table, never peers). `_TICKER_RE = ^[A-Z]{1,5}(?:[.-][A-Z])?$` — covers class shares (`BRK.B`, `BF-B`); plain `^[A-Z]{1,5}$` would silently demote those peer tables. **Degradation rule:** if a table classifies `peer` but, after the sanity filter, zero valid peer names remain (e.g. headers `Metric | NVDA | Industry Avg`), treat it as `generic` — never emit a peer slide with names and rows out of sync.
3. Peer extraction consumes only `peer` tables; financial only `financial`. Delete the `row.get("Current",...)` fallback in peer-row building.
4. Single peer-name append site with sanity filter `_TICKER_RE` (also guards narrative-regex extractors).
**Tests:** `test_table_classification.py` — realistic fixture → `peer_names==["AMD","INTC"]`, 3 peer rows, 2 financial rows; financial-table-only doc → no peer slide.
**Accept:** realistic golden updated and correct; the WMT regression test's documented misclassification now yields the right outcome (update its expectations).

### WP R.2 — Bounded bull/bear extraction (P2) — corrected skeleton
Two steps: bound the **block**, then split and sanitize. (The v3 one-step version stopped at the first sentence, contradicting the multi-sentence requirement — verified by execution.)
```python
_BLOCK_STOP = re.compile(r"\n#{1,6}\s|\n\s*\n|(?i:\b(?:bull|bear)\s+case\s*:)")
_BARE_FIGURE = re.compile(r"^\$?\s*[\d,]+(?:\.\d+)?\s*%?$")          # E1

def _case_block(text: str, label: str) -> str | None:
    """Text following '<label> case:' up to a markdown heading, blank line,
    or the opposite/next 'X case:' label (which may be on the SAME line)."""
    m = re.search(rf"(?i)\b{label}\s+case\s*:\s*", text)
    if not m:
        return None
    rest = text[m.end():]
    stop = _BLOCK_STOP.search(rest)
    block = rest[: stop.start()] if stop else rest
    return block.strip() or None

def _case_items(text: str, label: str) -> list[str]:
    block = _case_block(text, label)
    if not block:
        return []
    if re.search(r"^\s*[-•*+]\s+", block, re.MULTILINE):              # E2: bulleted form
        parts = _extract_bullets(block)
    else:
        parts = re.split(r"(?<=\.)\s+", block)
    return [it for p in parts if (it := _clean_item(p)) is not None][:5]
```
`_clean_item(s) -> str | None` is the **single choke-point** for every risk/opportunity/bullet append anywhere in extraction: strips markdown tokens and bullet prefixes; rejects items that still contain `\n`, start with `#`/`|`/table debris, are longer than 200 or shorter than 6 chars, match `_BARE_FIGURE` (E1 — a lone `$201.80` is scenario data, not a risk/opportunity), or match `^(?:bull|bear) case\b.*\$` (price sentences belong to **scenarios**, which are extracted separately and unchanged). *Known accepted limitation:* the sentence split fires on abbreviations (`U.S. demand…` loses its `U.S` fragment to the min-length filter) — acceptable data-quality cost; do not attempt a full sentence tokenizer.
**Tests:** `test_case_extraction.py` — same-line `Bull case: $201.80. Bear case: $96.50.` → scenarios populated **and `_case_items(...,"bull") == []` (E1 — verify the bare-figure reject, not just absence of the label)**; bulleted `Bull case:\n- a\n- b\n- c` → 3 clean items (E2); 3-sentence prose bull case → 3 clean items; property assertion over all fixtures: no extracted item contains `#` or `\n` or matches `_BARE_FIGURE`.
**Accept:** realistic-fixture Risk–Reward slide shows only the two real risks; opportunities column legitimately empty (R.3 governs presentation of empties).

### WP R.3 — Placeholder integrity (P3), text fit (P4), dedup (P5)
1. **P3:** `REPORT_PLACEHOLDER_POLICY` — `label` (default): generic items render visibly marked ("General market risk — not from analysis"; slide caption "No specific risks identified in this analysis" when all items are placeholders); `omit`: drop column/slide when nothing extracted. Provenance via `DeckData.risks_extracted: bool` / `opportunities_extracted: bool` flags (no string-sniffing). Applies to all three renderers.
2. **P4:** pure helper `fit_text(text, w_in, h_in, start_size) -> tuple[str, int]` — chars/line ≈ `w_in*96/(size*0.55)`, lines ≈ `h_in*72/(size*1.25)`; **explicit `\n` in the text starts a new line — count per-segment `ceil(len/chars_per_line)`, min 1 per segment**; step 20→18→16→14; below 14, truncate at the last sentence boundary within budget + `…`; **if the text has no sentence boundary (one giant sentence), hard-truncate at the last word boundary within budget**. `_text(..., shrink=True)` used by the thesis slide.
3. **P5:** renderer-side rule: a scenario rendered as a card is skipped in the valuation table (`DeckData` keeps both; renderer is the single decision point — cards preferred when ≥2 scenarios).
**Tests:** placeholder matrix (extracted × policy × 3 formats); `test_fit_text.py` (exact fit, 1-over, 3000 chars); valuation slide contains each dollar figure exactly once.
**Accept:** minimal-brief deck has no unlabeled invented bullets; 1,200-char summary fully visible; realistic valuation slide deduplicated.

### WP R.4 — Staged extraction pipeline + offline ticker info (P6)
**Depends:** R.1–R.3.
1. Restructure `extraction.py` into ordered pure stages over an `ExtractionCtx` (deck + cleaned_text + sections + tables): `tables → metrics → scenarios → risks_opportunities → peers → sections`; module-level `STAGES` list folded by `_extract_deck_data`. Per-stage unit tests; new heuristics arrive as stages, not inline regex blobs. Remove the R.1 flat-list back-compat.
2. **P6:** `generate_*` gain optional `company_info: dict | None`; API route passes name/sector/exchange from brief/MCP cache when present; yfinance becomes last fallback with 3s timeout, disabled entirely by `REPORTS_OFFLINE=true` (CI sets it). Tests drop `patch("yfinance.Ticker")`.
3. Regex inventory documented in the module docstring (pattern → stage → covering fixture).
**Accept:** end-to-end generation with network disabled; per-stage tests exist; no `extraction.py` function > 80 lines.

### WP R.5 — Realistic-corpus regression harness
**Depends:** R.1–R.4.
1. `tests/regression/corpus/` — 8–12 anonymized samples: quant-heavy (MC+DCF), sentiment-heavy (structured peers), table-heavy, table-free prose, one-liner, unicode/long, hostile (markdown debris, nested tables). `scripts/export_brief_fixtures.py` (strips user/session ids) lets the developer refresh from a local `db/` — script committed, data optional.
2. Parametrized deck-invariant suite over the corpus × 3 formats: no slide text contains `#`/`|`/newline debris; peer names match `_TICKER_RE`; text boxes fit (oracle = `fit_text` estimator); valuation dollar figures unique; slide count ∈ [4, 14]; generation never raises.
3. Optional visual smoke (`external` mark): if LibreOffice present — **pptx → pdf (`soffice --headless --convert-to pdf`) → `pdftoppm -png -f <page> -l <page>`** for title/thesis/valuation/peer pages, attached as CI artifacts for human review; no pixel assertions. (Direct `--convert-to png` yields only the first slide — do not use it.)
**Accept:** corpus suite green; temporarily re-introducing the P1 or P2 bug in a scratch branch fails ≥1 invariant test (verified, noted in PR).

---

## Phase 2 — Authentication & Authorization (fully inlined)

Three trust boundaries: **(A)** user → frontend → orchestrator; **(B)** orchestrator ↔ sub-agents (A2A); **(C)** agents ↔ MCP. `AUTH_ENABLED=false` preserves today's behavior exactly.

### WP 2.1 — Shared auth toolkit
**Depends:** 1.5. **Files:** `shared/auth/{__init__,tokens,service_auth,middleware,audit}.py`, `shared/logging_config.py` (G18), `pyproject.toml` (`pyjwt>=2.9`, `argon2-cffi>=23`).
**Skeletons:**
```python
# shared/auth/tokens.py
@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "service"]
    subject: str           # user_id or service name
    role: str = "user"     # "user" | "admin" | "service"

def issue_user_token(user_id: str, role: str, *, ttl: int | None = None) -> str:
    s = get_settings(); key = s.auth_jwt_secrets.split(",")[0]
    now = int(time.time())
    return jwt.encode({"sub": user_id, "role": role, "iat": now,
                       "exp": now + (ttl or s.auth_access_ttl_seconds),
                       "iss": "finsight", "type": "access"}, key, algorithm="HS256")

def verify_user_token(token: str) -> Principal:
    last: Exception | None = None
    for key in get_settings().auth_jwt_secrets.split(","):        # rotation
        try:
            c = jwt.decode(token, key, algorithms=["HS256"], issuer="finsight")
            if c.get("type") != "access": raise jwt.InvalidTokenError("wrong type")
            return Principal("user", c["sub"], c.get("role", "user"))
        except jwt.PyJWTError as e: last = e
    raise AuthError("invalid token") from last
```
```python
# shared/auth/middleware.py — pure ASGI so SSE rejects BEFORE the stream starts (G10)
PUBLIC_PATHS = {"/health"}   # plus startswith("/.well-known/"); match path.rstrip("/")

class AuthMiddleware:
    """Handles scope['type']=='http' only; 'lifespan' (and any 'websocket') passes through
       untouched. 1) public path -> pass. 2) Authorization header (ASGI headers are
       lowercase bytes; scheme matched case-insensitively, 'Bearer'/'bearer'):
       E4 guard — empty or short credentials NEVER match:
         len(x) >= 16 and compare_digest(x, SERVICE_AUTH_TOKEN) -> Principal(service);
         else verify_user_token(x) -> Principal(user).
       3) otherwise -> 401 JSON envelope (never a stream).
       Sets scope['finsight.principal']."""
    def __init__(self, app, *, accept: frozenset[str]): ...   # accept ⊆ {"user","service"}
```
Helpers: `get_principal(request)`, `require(request, *, kinds=("user",), role=None)` (403/401 envelopes), `build_auth_middleware(settings, accept=...) -> list` (empty when disabled; **replaces the WP 1.4 stub and is re-exported from `shared/agent_server.py`**). Body-size limit middleware (2 MB → 413) ships here (G10).
**G18:** redaction filter in `setup_file_logging`: regex over formatted messages for `(?i)(authorization|token|secret|password|api_key)` key/value shapes → mask values.
**Tests:** token expiry/wrong-sig/rotation/wrong-`type`/issuer; compare_digest asserted via mock; middleware matrix (public path **with and without trailing slash**, missing header, **empty bearer `Authorization: Bearer ` → 401 even when `SERVICE_AUTH_TOKEN` is misconfigured empty (E4)**, bad token, user-token on service-only route → 403, SSE route → 401 JSON not stream, lifespan scope passes through); redaction.
**Accept:** auth-disabled import changes nothing (middleware list empty); ≥95% coverage on `shared/auth/`.

### WP 2.2 — User authentication (boundary A)
**Depends:** 2.1, 1.6. **Files:** `shared/memory/store.py` (schema v4), `shared/memory/user_store.py`, `agent_1_adk/auth_routes.py`, `agent_1_adk/main.py`, `agent_1_adk/{api_routes,agui_bridge,agui_endpoint}.py`, `scripts/create_user.py`; frontend: `next.config.ts`, `middleware.ts` (new), `app/login/page.tsx`, `app/api/auth/*` route handlers, `app/api/copilotkit/route.ts`, `operator/page.tsx`.
**Backend:**
1. **Schema v4 (G15):** real migration step keyed on the existing SCHEMA_VERSION mechanism (or `PRAGMA user_version`): on `<4` create `users(user_id PK, username UNIQUE, password_hash, role DEFAULT 'user', created_at, disabled DEFAULT 0)` and `refresh_tokens(jti PK, user_id, expires_at, revoked DEFAULT 0)`; bump version; idempotent; runs inside `get_db()` init.
2. `user_store.py`: `create_user`, `verify_password` (argon2, rehash-on-verify), `get_user`. `scripts/create_user.py --username --role` prompts for password (never CLI arg) — bootstrap path for the first admin.
3. `auth_routes.py`: `POST /auth/login` → `{access_token, expires_in}` + httpOnly refresh cookie (refresh JWT, `type:"refresh"`, jti persisted). **Lockout (G16):** `LOGIN_MAX_ATTEMPTS` per username+IP per 15 min via `shared/rate_limiter.TokenBucket` → 429. **Edge — IP behind the proxy:** the orchestrator sees the Next server's IP for every browser user, so the IP dimension collapses; read `X-Forwarded-For` *only* when the direct peer is the trusted proxy (config list), else the socket address. Accept the residual tradeoff that username-keyed lockout lets an attacker DoS a known username — document it in SECURITY.md. `POST /auth/refresh`: rotate (revoke old jti, issue new pair); **reuse-detection:** a revoked jti presented again revokes the user's whole refresh family. **Edge — concurrent refresh race:** two tabs refreshing simultaneously means the loser presents a just-revoked jti and nukes the family; allow a single-use 30 s grace window on the most-recently-rotated jti (track `rotated_at`) so the race doesn't log users out, while genuine replay outside the window still trips family revocation. `POST /auth/logout` revokes + clears. `GET /auth/me` → principal. **CSRF:** mutating endpoints are reachable cross-site only via the cookie→bearer proxy; `SameSite=Lax` blocks cross-site POSTs and the remaining cross-site top-level GETs are reads (report downloads) — record this reasoning in SECURITY.md; if `SameSite=None` is ever needed, a CSRF token becomes mandatory.
4. Orchestrator app: `AuthMiddleware(accept={"user","service"})`; `require()` on all `/api/*`, `/a2a-agui`, `/agentic_chat`; `/auth/login|refresh` public.
5. **`/reports`:** replace the `StaticFiles` mount with authenticated `GET /reports/{filename}` — validate `^[A-Za-z0-9._-]+$`, resolve under `db/reports`, assert `is_relative_to`, stream `FileResponse` (static mounts can't do per-user auth).
6. **Identity swap** (single helper, both bridge and routes):
```python
def resolve_user_id(request) -> str | None:
    p = get_principal(request)
    if p and p.kind == "user": return p.subject
    if not get_settings().auth_enabled:                  # dev fallback only
        return request.headers.get("X-FinSight-User-Id")
    return None
```
**Frontend:**
7. **Proxy chokepoint (G17, verified):** Next `middleware.ts` with matcher `["/api/orch/:path*", "/api/copilotkit", "/api/traces", "/reports/:path*"]` — **`/reports` is rewritten in `next.config.ts` and must be matched or downloads bypass auth.** It reads the httpOnly session cookie (set by `app/api/auth/login/route.ts`, which proxies orchestrator login and stores access+refresh in its own httpOnly `SameSite=Lax` `Secure`-in-prod cookies) and injects `Authorization: Bearer …` server-side. Pages keep calling `/api/orch/...` unchanged.
8. **G8:** `operator/page.tsx` → `/api/orch/api/agents/{name}/health` instead of direct `fetch(svc.url)`.
9. `app/login/page.tsx`; unauthenticated page loads redirect to `/login` when `NEXT_PUBLIC_AUTH_ENABLED=true`.
10. `/api/traces` (Langfuse proxy with secret keys) behind the same middleware.
11. CORS: explicit `allow_headers=["Authorization","Content-Type","X-FinSight-User-Id"]`, origins from settings, `allow_credentials=True` when auth on.
**Tests:** login ok/bad/locked-out; refresh rotation + reuse-detection family-revoke; logout; auth matrix on every protected route × both modes; path-traversal on `/reports/..%2F..` → 400/404; FE: middleware header-injection test or documented manual checklist.
**Accept:** with auth on, browser flow login → chat → brief → PPTX download works end-to-end; `curl` without token → 401 envelope everywhere protected; header spoofing inert.

### WP 2.3 — Authorization / resource scoping (G4, G20)
**Depends:** 2.2. **Files:** `agent_1_adk/{api_routes,agui_bridge}.py`, `shared/memory/{ticker_memory,session_repo}.py`, `shared/semantic_cache.py`.
1. `sessions_list`: user filter mandatory from principal; `?user_id=` override admin-only.
2. `session_events`: owner check; mismatch → **404** (not 403 — no ID probing).
3. `TickerMemory.get_latest/get_history/has_changed`: `user_id` required keyword (+ `system: bool = False` escape for system callers; assert one set); update all call sites. **G4:** `_get_today_cached_text(ticker, user_id)` passes the resolved user; `memory_ticker_latest/changed` routes scope to principal. `report_by_id` adds `AND user_id = ?`; `report_latest` via scoped `get_latest`.
4. Role gates: `/api/agents*` and cross-user listing → `require(role="admin")`.
5. **G20:** `shared/semantic_cache.py` — include user scope in entry metadata and lookups (Chroma `where={"user_id": ...}`; `__system__` for no-user contexts); key remains date-scoped. **Ordering edge:** on the A2A path (`agent_executor.py`) the verified user only becomes available via message metadata in WP 2.4 — until 2.4 lands, the A2A-path semantic cache must scope by `session_id` (never `__system__` for user queries), then switch to the propagated user; the AG-UI path can scope by real user immediately.
**Tests:** two-user integration suite — A cannot read B's brief by id (404), B's session events (404), B's cached today-brief, or B's semantically-similar cached answer; admin can list agents.
**Accept:** suite green; `grep -n "user_id=None"` shows only `system=True` call sites.

### WP 2.4 — A2A service authentication (boundary B)
**Depends:** 2.1, 1.4. **Mandatory pre-task:** read the installed `a2a-sdk` source (`ClientFactory`, `A2ACardResolver`, `ServerCallContext`, `OwnerResolver`) and record the actual header-injection and context APIs in the PR before coding.
1. **Cards advertise:** `securitySchemes` (HTTP bearer) + `security` on every `AgentCard` and mirrored in `agent_cards/*.json` (card endpoint itself stays public).
2. **Server:** factory applies `AuthMiddleware(accept={"service"})` to `/a2a` **and `/release-evals` (G12)** when enabled; orchestrator's own `/a2a` likewise; `/a2a-agui` remains user-auth.
3. **Client:** `SubAgentClient` builds one `httpx.AsyncClient(headers={"Authorization": f"Bearer {settings.service_auth_token}"})` used for both discovery and message-send (constructor-level default headers; adapt to the SDK API found in the pre-task).
4. **User context propagation:** orchestrator forwards verified `user_id` inside A2A **message metadata** (never a bare header); sub-agents trust it because the request bore the service token.
5. **G13:** custom `OwnerResolver` mapping `ServerCallContext` → real principal (service name or propagated user) for `SQLiteTaskStore`; default behavior when auth off.
6. **Offline tooling:** eval runners under `tests/evaluation/` and any `scripts/` hitting protected endpoints gain `--token`/env credential support — audit in this WP.
**Tests:** in-process ASGI — JSON-RPC without/with-wrong bearer → 401; with bearer → completes; discovery unauthenticated; card JSON contains schemes; propagated user_id observed in a stub agent's context; `/release-evals` rejects user tokens (403) and none (401).
**Accept:** full orchestrated brief succeeds in both modes.

### WP 2.5 — MCP service authentication (boundary C)
**Depends:** 2.1, 1.1.
1. In `get_app()`, wrap the SSE `Mount` with `AuthMiddleware(accept={"service"})` (the MCP endpoint is plain ASGI — works regardless of FastMCP version; prefer a native FastMCP token-verifier hook if the pinned version has one, note which in PR). `/health` stays public (compose healthchecks).
2. Client: `MCPServerConfig.headers`; `sse_client(url, headers=...)` (verify signature for pinned `mcp`); `get_shared_mcp()` injects the bearer. Reconnects re-send (connection-level).
3. `MCP_HOST` default `127.0.0.1`; compose overrides `0.0.0.0`.
**Tests:** SSE connect without token rejected pre-stream; tool list + one call with token; `test_mcp_server_smoke.py` parametrized over both modes; compose E2E (integration mark) — four agents complete a brief with tokens.
**Accept:** matrix green; only `mcp_client.py` knows the token.

### WP 2.6 — Sandbox hardening (`execute_python`)
**Depends:** 2.5.
1. `SANDBOX_MODE`: `ast` (today, default) | `container` | `disabled` (tool returns `{"error": "execute_python disabled by SANDBOX_MODE"}`).
2. `container`: `docker run --rm --network none --memory 512m --cpus 1 --read-only --tmpfs /tmp:size=64m --user 65534:65534 python:3.12-slim python -I -S -` with code on **stdin**; 30 s wall-clock (`subprocess.run(timeout=...)` + `docker kill` fallback). Docker absence at startup → fall back to `ast` with loud warning.
3. `validate_runtime()`: production + (`ast` on Windows, or `container` requested but docker absent) → refuse to start unless `disabled`.
4. Audit-log every invocation: principal, sha256(code), duration, exit status.
**Tests:** existing 60+ AST tests unchanged; container (integration mark): network egress blocked, FS write blocked outside /tmp, memory bomb killed, CPU spin killed; `disabled` returns the error dict.
**Accept:** mode-matrix green; SECURITY.md documents the ast-denylist caveat honestly.

### WP 2.7 — Secrets, transport, audit wrap-up
**Depends:** 2.2–2.6.
Token-generation docs (`openssl rand -hex 32`); compose secrets via `env_file` only; audit all images for baked tokens; `deploy/Caddyfile.example` (TLS in front of :3000/:8001; inter-service stays on the compose network; mTLS = future work); audit-log calls on login success/failure/lockout, refresh-reuse detection, admin endpoints, `execute_python`; SECURITY.md rewrite (remove no-auth rows; keep: no mTLS, SQLite unencrypted at rest, ast caveat, single-host design).
**Accept:** secret-pattern grep over repo/Dockerfiles/compose finds nothing real; docs read true.

---

## Phase 3 — Quality, Observability, Docs

**WP 3.1** (after 2.2 and R.5): contract tests for every route × {auth on/off} × {none, user, service, admin}; in-process A2A protocol test of `GenericAgentExecutor` (WORKING→artifact→COMPLETED, cancel, failure); `pytest -m auth` marker; CI matrix `AUTH_ENABLED={false,true}`; corpus suite (R.5) in CI. Coverage ≥90% on `shared/auth/`, `api_routes`, `auth_routes`.
**WP 3.2:** REST surface → mounted FastAPI sub-app with pydantic response models (extend `shared/models.py`); `docs/openapi.json` checked in + CI diff step; regenerate `docs/API_REFERENCE.md`; typed FE fetches.
**WP 3.3:** Langfuse traces tagged with verified `user_id`; structured `auth.denied reason=...` log counter; trace-filter heuristics → `lib/traceFilter.ts` with tests.
**WP 3.4:** README split (dev quick-start vs secured deployment with the exact env vars); ARCHITECTURE.md module map + A/B/C trust-boundary diagram; CHANGELOG per phase.
**WP 3.5 (last):** remove all shims (`shared/report_generator.py`, `shared/config.py` constants, R.1 flat-table back-compat if any remains, dev header fallback flagged in prod docs); ruff/mypy ratchet review; suite emits zero `DeprecationWarning`.

---

## 4. Edge-Case Register (cross-cutting — every agent must respect these)

| # | Edge case | Rule | Enforced in |
|---|---|---|---|
| EC1 | Empty/short bearer credentials (E4: `compare_digest("","") is True`) | Middleware rejects credentials < 16 chars before any comparison; settings refuse `auth_enabled` with weak/missing secrets in **any** env. | 2.1, 1.5 |
| EC2 | Trailing slashes on allowlisted paths (`/health/`) | Match on `path.rstrip("/")`; tests cover both forms. | 2.1 |
| EC3 | Non-http ASGI scopes (`lifespan`) hitting auth middleware | Pass through untouched; only `http` is gated. | 2.1 |
| EC4 | Concurrent refresh from two tabs | 30 s single-use grace on the most-recently-rotated jti; replay outside it revokes the family. | 2.2 |
| EC5 | Lockout IP collapsed behind the Next proxy | `X-Forwarded-For` honored only from the configured trusted proxy; username-DoS tradeoff documented. | 2.2 |
| EC6 | A2A-path user identity unavailable until WP 2.4 | Semantic cache scopes by `session_id` interim; never `__system__` for user queries. | 2.3 |
| EC7 | Class-share tickers (`BRK.B`, `BF-B`) in peer tables | `_TICKER_RE = ^[A-Z]{1,5}(?:[.-][A-Z])?$` everywhere peer names are validated. | R.1, R.5 |
| EC8 | Table classifies `peer` but yields zero valid names (`Industry Avg` columns) | Degrade to `generic`; never emit a peer slide with names/rows out of sync. | R.1 |
| EC9 | Bare price figure as risk/opportunity item (E1) | `_BARE_FIGURE` reject in `_clean_item`. | R.2 |
| EC10 | Bulleted bull/bear case blocks (E2) | Bullet-shaped blocks route through `_extract_bullets` before cleaning. | R.2 |
| EC11 | Abbreviation sentence-splits (`U.S.`) | Accepted data-quality loss; no sentence tokenizer. | R.2 |
| EC12 | Thesis text with explicit newlines or no sentence boundary | `fit_text` counts per-segment lines; word-boundary hard truncation fallback. | R.3 |
| EC13 | `pip install .` with flat multi-package repo (no setuptools table) | Explicit `[tool.setuptools.packages.find] include=[...]` is step 0 of Docker work. | 1.8 |
| EC14 | DST/midnight & timezone: all "today" comparisons use `datetime.now(IST)` consistently (brief dedup, fast-path, `_today()`) | Never mix naive `date.today()` with IST-aware dates. | 1.7, 2.3 |
| EC15 | Ticker path params accept indices/classes (`^GSPC`, `BRK.B`) per `^[A-Z0-9.^-]{1,10}$` while peer names are stricter | Intentional asymmetry — API accepts what yfinance accepts; peer extraction stays conservative. | 1.6/R.1 |

---

## Execution order & dependency graph

```
Phase 0:  0.1 → 0.2 ; 0.3 parallel
Phase 1:  1.5 → {1.1, 1.2, 1.3, 1.4, 1.8} parallel → 1.6 → 1.7
Phase R:  (after 1.2) R.1 ∥ R.2 ∥ R.3 → R.4 → R.5        (independent of Phase 2)
Phase 2:  2.1 → 2.2 → 2.3 ;  2.1 → {2.4 (needs 1.4), 2.5 (needs 1.1)} → 2.6 → 2.7
Phase 3:  3.1 (after 2.2 and R.5) → 3.2 → 3.3 → 3.4 → 3.5
```

**Highest-risk WPs:** **1.5** (load-bearing import-order side effects; `bootstrap()` before framework imports in every entrypoint incl. `adk web`); **2.4** (unverified a2a-sdk client API — pre-task mandatory); **1.8 step 2** (pyproject-driven installs surface masked version conflicts; budget a pinning pass); **R.1** (table-semantics changes shift goldens broadly — two-commit landing strategy as specified).
