# Design Decisions

## Caching Strategy

### Why `_TTLCache` with `OrderedDict` instead of a library?

`functools.lru_cache` is synchronous and doesn't support TTL. `cachetools` and `aiocache` would be new dependencies. `OrderedDict` + `time.monotonic()` gives LRU eviction, TTL expiry, and thread-safety with a `threading.Lock` in ~30 lines. Zero new package imports.

**Why per-tool instances instead of a single global cache?** Different tools have different freshness requirements. Prices change every minute, financials change quarterly, SEC filings never change. Separate instances with distinct TTLs make policy explicit and easy to adjust.

**Why `_fetch_submissions` cached instead of the outer tools?** `get_company_filings` and `get_financial_filings` both call `_fetch_submissions(cik)` internally. Caching at the shared method benefits both callers — caching at the outer tool level would duplicate the cache or require coordination.

**Why permanent cache for `get_filing_content`?** SEC EDGAR documents are immutable once filed. A document at `https://www.sec.gov/Archives/...` never changes. LRU-200 keeps the 200 most recently accessed filings in memory indefinitely — safe and maximally efficient.

### Why LangChain SQLiteCache for LLM responses?

The quant agent's `llm_summary_node` is the only LangChain LLM call in the graph. Given the same ticker + computed metrics (prices, Sharpe, VaR, DCF), the LLM should produce an equivalent summary. `SQLiteCache` is built into `langchain-community` with zero configuration beyond a `set_llm_cache()` call at module level. It uses the full prompt as the cache key, so it only fires on exact repeats — no risk of stale summaries on changed market data.

### Why ChromaDB for the semantic cache instead of Redis?

ChromaDB is already running in the same process for RAG. `all-MiniLM-L6-v2` is already loaded. Adding Redis would require a new container and a new driver. The semantic cache uses a separate Chroma collection (`finsight_semantic_cache`) so it doesn't pollute the RAG indexes.

**Threshold 0.95**: Investment queries are precise ("analyze NVDA for long-term hold" ≠ "analyze AAPL for long-term hold"). A threshold below 0.95 risks incorrect cache hits between different tickers. 0.95 is high enough to match paraphrase variants of the same question while rejecting cross-ticker confusion.

**Why opt-in (`SEMANTIC_CACHE_ENABLED=false`)?** The semantic cache is stateful — a cached stale recommendation from yesterday might mislead a user. Off by default so developers consciously enable it in environments where TTL staleness is acceptable.

## Before-Agent Callback for Same-Day Memory Cache

### Why a `before_agent_callback` instead of extending the code-level gate?

The v1.23 same-day cache relied on the LLM honoring `[TODAY]` tags in the injected memory context — but the LLM was inconsistent. Sometimes it re-ran agents anyway (wasting 30-60s). Making `[TODAY]` a hard **"MUST return directly"** helped but did not eliminate the variance.

`before_agent_callback` is an ADK extension point that fires *before* the LLM is invoked. Returning a `types.Content` response from this callback tells the ADK runner to use that as the agent response and skip the LLM entirely. This gives a deterministic same-day cache with zero LLM variance — the LLM is never asked to make a decision about whether to re-run.

The callback checks for a valid `response_text` in the cached brief (populated by the `update_response_text` overwrite, see below). If only a recommendation exists without analysis text, it falls through to let the LLM run.

**Why two cache paths (callback + executor-level)?** The `before_agent_callback` only fires when the orchestrator runs through ADK's built-in runner (`adk web` path). A2A requests hitting `agent_1_adk/main.py` directly bypass ADK callbacks, so the executor-level `_get_today_cached_text()` provides the same short-circuit for that path.

### Why the full synthesis is stored directly in `save_brief` instead of a post-turn overwrite?

The original approach used `_persist_memory_callback` to overwrite the brief's `response_text` after the turn completed. This had two problems: (1) the overwrite was unreliable — it depended on extracting the right response text from session events after the LLM finished, and (2) it was blind to the A2A executor path, which doesn't fire `after_agent_callback`.

The fix moves the synthesis capture into `save_brief` itself. A new helper `_synthesis_text_from_context` reads the longest LLM-generated text from `session.events` at the time `save_brief` is called. Since `save_brief` is invoked *during* the LLM turn (after the tool response but before the turn ends), the session events already contain the model's full synthesized output. If no model text exists (edge case), it falls back to the rationale. The post-turn `update_response_text` overwrite was removed entirely. Both ADK-web and A2A paths now persist the full analysis because both call `save_brief`.

### Why programmatic dedup at `save_brief` and `_store_memory`?

Two duplicate-prevention layers:

- **`save_brief` (agent.py)**: Checks if today's brief already exists for the ticker before inserting. The `analysis_date` column makes this a single SQL lookup — no equality comparison on timestamps needed.
- **`_store_memory` (agent_executor.py)**: Same check at the executor level, guarding against the case where `save_brief` was called but the brief was stored under a different `user_id` (the A2A executor and ADK callback use different user_id values).

Together they prevent identical rows accumulating on repeated same-day queries, which was the root cause of the original duplicate record bug.

## Ticker Extraction: Dotted & Single-Char Tickers

### Why support dotted tickers?

Berkshire Hathaway (`BRK.A`, `BRK.B`) has a dot in its ticker symbol. The previous regex limited matches to `[A-Z]{1,5}` which excluded the dot entirely. When a user typed `BRK.A`, the pattern matched `BRK` (which is not a valid standalone ticker) and validation failed. The fix was to extend the regex to `[A-Z]{1,5}(?:\.[A-Z]{1,2})?` in all patterns, matching the optional dot suffix.

### Why support single-char tickers?

Visa (`V`) and Alleghany (`Y`) are single-character NYSE tickers. Pattern 5 (`\b([A-Z]{2})\b`) excluded them. Changed to `\b([A-Z]{1,2})\b`. The new mixed-case parens pattern (`\b([A-Z]{1,5})\s+\([A-Za-z][a-z]`) specifically handles "V (Visa)" format, which was the only way these single-char tickers appeared in user input.

### Why extract `_build_response` from `stream()`?

All three sub-agent executors had the same pattern: a try/finally block wrapping Langfuse observability inside `stream()`. Extracting `_build_response(query) → dict` isolates the response-building logic from the async generator boilerplate. Benefits:

- **Testability**: `_build_response()` is a plain async function returning a dict — no generator machinery, no yield semantics. Unit tests can call it directly.
- **Clarity**: `stream()` is now two lines: `yield await self._build_response(query)` in try, `_disconnect()` in finally. The actual logic is in one place instead of mixed with yield statements.
- **Consistency**: All three agents use the same pattern, making cross-agent debugging easier.

## IST Timezone Standardization

### Why IST instead of UTC?

The system is operated from India (IST, UTC+5:30). Before the fix, timestamps were a mix of:

- `datetime.now()` — used the server's local time (IST on the development machine, UTC in Docker)
- `datetime.utcnow()` — explicit UTC in some memory modules
- `datetime.now(IST)` — explicitly IST in the agent executor

The mix caused the same-day cache to fail on non-IST machines: a brief created at `2026-05-26T06:30:00Z` (UTC) was analyzed as `analysis_date=2026-05-26` (correct), but `datetime.now()` on an IST machine returned `2026-05-26 12:00:00` (noon IST, still same day) — same day, fine. But on a UTC machine, `datetime.now()` returns `2026-05-26T12:00:00Z` — also fine during the day. The bug manifested at the day boundary: a brief created at `2026-05-26T23:30:00Z` (next day IST: 05:00 AM) would have `analysis_date=2026-05-26`, but `datetime.now()` in UTC would return `2026-05-26T23:30:00Z` still, appearing to be same day. The boundary was inconsistent.

Centralizing on `IST = timezone(timedelta(hours=5, minutes=30))` and using `datetime.now(IST)` everywhere makes the day boundary unambiguous: the analysis date and "today" comparison always use the same timezone.

## Stale Test Removal

### Why delete all tests instead of fixing them?

The 17 test files were written during the initial architecture iterations (v1.0-v1.15) when the codebase was rapidly evolving. By v1.24, most tests referenced:

- Classes and functions that were renamed or removed
- Mock patterns that no longer matched current dependency injection
- Offline evaluation fixtures that diverged from the live runtime behavior
- A2A communication patterns from the v1-v4 orchestrator architecture that were completely replaced

Fixing them would require a full rewrite against the current codebase — essentially writing new tests from scratch while also removing all the old ones. Deleting the stale fixtures was the honest option rather than maintaining dead test code that would confuse future readers.

**Update (v1.26/v1.27)**: ~148 tests now covering models, quant graph nodes, ticker utilities, TTL cache, rate limiter, trace context, memory store, and the security sandbox (60 AST-gate tests). See [TESTS.md](TESTS.md) for details.

## Same-Day Memory Cache & analysis_date Column

### Why check the date before re-running agents?

Market data (prices, news sentiment) changes daily. A recommendation from yesterday may be wrong today. However, re-running all three sub-agents for every query on the same stock within the same day wastes 30–60 seconds of LLM inference and MCP calls when the underlying data has not changed since the last run. The same-day cache gives users instant responses for repeat queries while guaranteeing a fresh agent run the next day.

### Why a separate `analysis_date` column instead of parsing `created_at`?

`created_at` stores a full UTC ISO-8601 timestamp (`2024-01-15T14:32:00`). Comparing it to today requires string slicing and timezone handling that is fragile across platforms and SQLite versions. `analysis_date` is a plain `TEXT` column storing `YYYY-MM-DD` — an equality check against `date.today().isoformat()` is unambiguous and requires no parsing. Added as a nullable column via `ALTER TABLE` migration so old rows fall back to `created_at` via `COALESCE(analysis_date, created_at)`.

### Why tag with [TODAY] / [STALE] in the injected context instead of a hard code gate?

A hard code gate (returning early before calling the LLM) would be faster, but it removes the LLM's ability to reason about context — a user asking a follow-up question about the same ticker on the same day may warrant a different answer even with the same recommendation. Tagging lets the LLM decide: `[TODAY]` says "you may use this directly"; `[STALE]` says "call agents, treat this as background only." The instruction is reinforced in both the injected user message and the system preamble (`_STATIC_PREAMBLE`) for reliability.

### Why skip `_store_memory()` on [TODAY] responses?

Every call to `_store_memory()` inserts a new row into `ticker_briefs`. Without the guard, a stock queried ten times in a day produces ten identical rows, inflating the DB and returning redundant context on the next query. Since same-day responses are served from an already-stored brief, re-storing is pure duplication. The guard checks for `"[TODAY"` in the augmented `user_input` string — one string check, no extra DB query.

## Unified db/ Folder

### Why consolidate all databases under db/ instead of keeping them at the project root?

Three separate DB files (`finsight_memory.db`, `.langchain_cache.db`, `chroma_db/`) were scattered at the project root, requiring three separate `.gitignore` entries and making it easy to miss one. A single `db/` folder with one ignore rule (`db/`) is easier to reason about, simpler to back up or wipe, and keeps the project root clean. The folder is created automatically on first run — no setup step required.

## Guardrails

### Why a regex off-topic filter instead of an LLM classifier?

An LLM classifier takes 2-5 seconds and consumes tokens. The regex fires in microseconds and costs nothing. The set of clearly off-topic domains (weather, recipes, sports, entertainment) is small and stable — regex is the right tool. Off-topic queries that slip through the regex are still handled gracefully by the orchestrator LLM.

### Why ticker pre-validation before spawning sub-agents?

Without pre-validation, an invalid ticker causes all three sub-agents to fail after 30-60 seconds each (~90-180 seconds total wasted time). MCP `validate_ticker` uses the cached SEC ticker map — it completes in < 100 ms and costs nothing. Failing fast with a clean error message is far better UX than three confusing simultaneous failures.

### Why not retry on missing BUY/HOLD/SELL signal?

The plan originally suggested retrying once with a reminder appended. After evaluation, a retry doubles latency (60+ seconds) for a marginal improvement — the LLM that omitted the signal is likely to omit it again without fundamentally different context. Instead, the missing signal is logged as a Langfuse warning so the pattern can be analyzed and the prompt improved. The user gets the response immediately rather than waiting for a retry that may not help.

## Incremental RAG Ingestion

### Problem

The RAG agent's `_ensure_ingested()` used an in-memory `self._last_ingestion[ticker]` guard keyed by date. This prevented re-ingest within a single server run on the same day, but after a restart all filings were re-ingested from scratch. For a company with 20 historical 10-K/10-Q filings, this meant 20 `get_filing_content` MCP calls + 20 ChromaDB insertions on every cold start — taking 30-60 seconds before the first query could be answered.

### Solution

`ingested_filings` table in SQLite with `edgar_url` as primary key (SEC EDGAR URLs are canonical and immutable). Before fetching any filing, `_ensure_ingested()` calls `is_filing_ingested(url)` — a single indexed SQLite lookup. After successful batch ingest, `mark_filing_ingested(url, ticker)` records the URL. On the next startup, all previously indexed filings are skipped immediately.

**Why not store content hash instead of URL?** The URL is the canonical identity for an SEC filing. Content could theoretically be truncated differently on different runs (network errors, timeouts), making a hash unreliable. The URL is deterministic and guaranteed unique by SEC.

## Health Endpoints

### Why add health endpoints now?

Docker-compose `depends_on` with `condition: service_healthy` requires a healthcheck command. Without health endpoints, docker-compose would start the orchestrator before the MCP server is actually serving requests, causing startup failures. The `/health` route is 5 lines of code per service and enables proper container orchestration.

### Why not use the existing A2A `/.well-known/agent-card.json` as the health signal?

Agent card resolution involves async agent discovery and sub-agent connection — it's not safe to call during startup. A dedicated `/health` route returns immediately regardless of initialization state, which is the correct semantics for a liveness probe.

## RAGAS Evaluation Pipeline

### Why RAGAS over a custom evaluation framework?

RAGAS provides well-established metrics (Faithfulness, ResponseRelevancy, ContextPrecision, ContextRecall) that are accepted in the research community and have known baselines. Writing equivalent metrics from scratch would require significant prompt engineering and validation. RAGAS also integrates with LLM judges, making it suitable for evaluating subjective quality of financial analysis.

### Why custom `AspectCritic` metrics in addition to RAGAS core?

RAGAS core metrics evaluate retrieval quality and factual consistency — they don't capture financial domain requirements. A response can be factually grounded but still fail to cite specific filing dates, omit risk disclosures, or give an ambiguous BUY/HOLD/SELL signal. The three custom rubrics (`citation_quality`, `risk_disclosure`, `recommendation_clarity`) evaluate the domain-specific outputs that matter most in an investment research context.

### Why push scores to Langfuse?

Evaluation results are only useful if they're tracked over time. Pushing scores to Langfuse per-trace links quality metrics to specific queries and model versions, enabling regression detection when the prompt or model changes. The `push_scores.py` script is intentionally decoupled from the evaluation runners so scores can be re-pushed without re-running evaluation.

### Runtime vs Offline Evaluation

**Why score at runtime instead of only in batch tests?** Batch evaluation (RAGAS offline pipeline) runs on a fixed dataset and catches regressions before deployment. Runtime evaluation catches real-world drift — production queries produce different patterns than curated test sets. Together they cover both pre-deployment quality gates and live monitoring.

**Why fire-and-forget (`asyncio.create_task`) instead of synchronous?** RAGAS metric computation calls the LLM 1-5 times per agent response. Waiting synchronously would add 10-30 seconds to response time. Fire-and-forget background tasks complete in parallel with the A2A response being returned to the orchestrator, adding zero latency to the user-facing path.

**Why `asyncio.wait(FIRST_COMPLETED)` instead of `asyncio.gather`?** `asyncio.gather` waits for all metrics to complete before returning any results. Fast metrics (AnswerRelevancy, DomainSpecificRubrics ~3-5s) sat idle while Faithfulness ran its multi-call LLM decomposition (~180s timeout). `asyncio.wait(FIRST_COMPLETED)` logs and pushes each metric score to Langfuse the moment its `ascore()` finishes, so slow metrics don't delay fast ones.

**Why module-level client caching?** `_setup_ragas_clients()` creates an `InstructorLLM` (patched OpenAI client) and `_STEmbeddings` (loading `SentenceTransformer(all-MiniLM-L6-v2)` ~80MB, ~1-2s). Without caching, every agent's eval reloads the embedding model — 4 redundant loads per query. Module-level `_ragas_clients` tuple caches after first call; subsequent calls return the cached instance. The import and model-load cost is paid once per server lifetime.

**Why `BaseException` guard in `_run_metrics`?** Python's `CancelledError` inherits from `BaseException`, not `Exception`. `asyncio.gather(return_exceptions=True)` catches `CancelledError` as a result value, but `isinstance(result, Exception)` returns `False` for it. The fallthrough code `round(float(result), 4)` crashes with `TypeError`, killing the entire eval task silently via `create_task` fire-and-forget. Checking `isinstance(result, BaseException)` catches both regular exceptions and `CancelledError`.

**Why increase HTTP timeout to 180s?** Faithfulness makes multiple sequential LLM calls within a single `ascore()` — it decomposes the response into atomic claims, then verifies each against the retrieved contexts. On a 20B local model at ~20-30s per call, three claims require 60-90s total. The default 60s client timeout kills the metric mid-decomposition. 180s provides headroom for up to 6 claims with retries.

**Why `sys.stdout.reconfigure(encoding='utf-8')`?** RAGAS internal log messages (from `ragas/llms/base.py`) contain Unicode characters like curly quotes (`\u2010`, `\u2011`) when formatting LLM responses. On Windows with cp1252 console encoding, these characters trigger `UnicodeEncodeError`, producing noisy "--- Logging error ---" tracebacks. Setting UTF-8 at import time in `shared/config.py` prevents this for both stdout and stderr.

**Why `_push_scores` skips when trace_id is None?** With placeholder Langfuse API keys (`pk-lf-...`), `langfuse.create_score()` with no `trace_id` sends an API request missing the required trace identifier. The Langfuse cloud API rejects these with "Bad request" errors. Since eval tasks may run outside any active trace context, the early return when `trace_id is None` avoids pointless API failures.

**Why custom `_STEmbeddings` instead of RAGAS's `HuggingfaceEmbeddings`?** RAGAS 0.4.x's `HuggingfaceEmbeddings` is a Pydantic dataclass that fails to serialize correctly when passed to RAGAS internal `aembed_text` calls. The custom wrapper uses `BaseRagasEmbedding` directly with `SentenceTransformer.encode()`, bypassing the broken Pydantic path entirely.

**Why `JSON_SCHEMA` mode instead of `JSON` mode for instructor?** RAGAS defaults to `instructor.Mode.JSON` which sends `response_format.type="json_object"` in the API request. LM Studio only supports `"json_schema"` and `"text"` response format types. Patching to `JSON_SCHEMA` enables structured output without a custom LM Studio fork.

### Why gate every eval call behind a single `EVAL_TRACE_ENABLED` flag?

Sidecar RAGAS evaluation adds 5–180 seconds of background LLM work per query (per agent) on the local LM Studio judge. During fast iteration on prompts or sub-agent behaviour, this background load slows the judge model down for the next user query, and burns context on metrics that aren't being inspected. Wrapping each `asyncio.create_task(_eval_*)` site in `if EVAL_ENABLED:` lets the user kill all sidecar scoring from `.env` without removing code. `EVAL_ENABLED` reads the same `EVAL_TRACE_ENABLED` env var that already controls orchestrator trace JSON dumps — one switch, two effects, both about "extra eval load". Default stays `True` so production observability is on by default.

### Why move the orchestrator eval into `after_agent_callback`?

When `adk web` is the entry point, the orchestrator goes through ADK's built-in runner — `FinSightAgentExecutor.execute()` is never called. The eval call originally placed in `agent_executor.py` only fired when an A2A client hit `agent_1_adk/main.py` directly. Once `run_adk_web.bat` stopped starting the standalone orchestrator A2A server (it's redundant with `adk web`), evals stopped firing entirely.

`after_agent_callback` is the only ADK extension point guaranteed to fire on every agent turn regardless of runner. Wiring eval scheduling into the existing `_persist_memory_callback` keeps both side-effects in one place: callback → check turn type → persist memory → fire eval. The trace_id is read from the active Langfuse span at callback time, so traces from `adk web` are still linked.

### Why gate memory persist + eval on whether `save_brief` was called this turn?

`_persist_memory_callback` fires on every ADK turn, including pure recall turns where the user asks "what were my last recommendations?" and the agent only invokes `load_memory`. The old behaviour persisted the entire session — including the conversational recall query and the agent's response — into long-term memory. Subsequent `load_memory` calls would then surface those recall exchanges alongside actual analyses, polluting search results and drifting the agent toward conversational rather than analytical responses.

`_is_analysis_turn(events)` walks back to the most recent user message and checks for a `save_brief` tool call in the agent's response — `save_brief` is the explicit signal that a fresh recommendation was produced. If absent, persist + eval both skip. This is preferable to time-based heuristics (last N turns) or content-based heuristics (response length, keyword matching) because it relies on a deterministic signal that the orchestrator emits as a real act, not on inference about what the turn *meant*.

### Why namespace Langfuse scores by agent (`ragas/{agent}/{metric}`)?

The previous `ragas/{metric}` naming flattened all four agents into one dimension. `ragas/AnswerRelevancy` could be the orchestrator's synthesis score, the RAG agent's filing-answer score, the quant agent's metric-summary score, or the sentiment agent's narrative score — Langfuse had no way to tell them apart in dashboards or aggregations. Adding the agent prefix surfaces the source in every existing Langfuse view (score breakdowns, trends, filters) without requiring custom metadata processing. The redundant `comment="agent=<name>"` tag gives a second filter dimension if anyone wants to query by comment instead of name prefix.

### Why not introduce a separate batch-eval runner (`_invoke_agent`)?

The earlier shape of `runtime_eval.py` included an `if __name__ == "__main__":` block with `_invoke_agent()` that spun up its own `Runner` + `InMemorySessionService` to invoke the orchestrator for a fixed set of test cases. This duplicated exactly what `FinSightAgentExecutor` and `after_agent_callback` already do for live traffic — running the agent, collecting events, extracting the response. The live executor *already has the response in hand* when it fires the sidecar eval; a second runner adds nothing except a second source of truth that can drift from the first.

Removed: `_invoke_agent()`, `_run_batch_eval()`, `_BATCH_EVAL_CASES`, and the `__main__` block. Batch evaluation with ground-truth references (which is a genuinely different concern — it needs reference tool calls and reference answers) still lives in `tests/evaluation/run_orchestrator_eval.py`.

## Google ADK 2.x Upgrade

### Why upgrade now?

ADK 1.x is in maintenance mode. 2.x is the active development line. The 2.0 breaking-change inventory (event schema additions, `BaseAgent` extending `BaseNode`, automatic exception catching for retries) is small and easy to audit against the current codebase. Putting off the upgrade only widens the diff that needs to be reviewed later.

### Code changes required: zero

All 2.0 breaking changes were checked against the codebase before upgrading:

| 2.0 breaking change | Affects FinSight? |
|---|---|
| Event schema adds `node_info` + `output` fields | No — `SQLiteMemoryService._event_to_dict` only reads `author` and `content.parts`, doesn't validate full schema |
| `BaseAgent` extends `BaseNode`; no more `_run_async_impl` overrides | No — we use `LlmAgent` directly; `shared/base_agent.py` is a separate Pydantic class, not ADK's `BaseAgent` |
| No manual `enqueue_event()` on ADK events | No — `enqueue_event()` calls in `shared/generic_executor.py` are on A2A's `EventQueue`, not ADK |
| No broad `try/except BaseException` | No — the three matches are in `shared/mcp_client.py` and `shared/runtime_eval.py`, both outside the ADK execution path |

Smoke-tested at upgrade time: all imports from `google.adk.agents`, `google.adk.tools`, `google.adk.runners`, `google.adk.sessions`, `google.adk.events`, `google.adk.memory`, `google.adk.cli.service_registry` resolve. `SQLiteMemoryService` still satisfies the 2.x `BaseMemoryService` interface (signatures match). `agent_1_adk.agent.root_agent` loads and registers all four tools. `after_agent_callback` still fires.

### Why pin `>=2.0,<3.0` instead of an exact version?

We track 2.x patches and minor updates automatically (bug fixes, new features) while excluding the next major bump (which will have its own breaking-change audit). Same pattern as other major-version-pinned deps in this project.

## Why Four Different Agent Frameworks?

| Agent | Framework | Why |
|---|---|---|
| Orchestrator | **Google ADK** | A2A protocol built-in, agent card generation, session management |
| RAG | **LlamaIndex** | Best document indexing/retrieval — hybrid search, multi-index routing |
| Quant | **LangGraph** | Conditional state machine maps naturally to graph-based architecture |
| Sentiment | **CrewAI** | Multi-agent role-playing (analysis + synthesis) is what CrewAI was designed for |

## A2A Communication

We use the official Google A2A SDK (`a2a-sdk>=1.0.0`) for inter-agent communication.

### A2A API Reference vs Installed SDK

The A2A SDK has evolved significantly. The official A2A samples (Google, GitHub) reference APIs that may not match the installed version:

| Component | GitHub Samples | Installed SDK |
|---|---|---|
| Client | `A2AClient(httpx, card, url)` | `ClientFactory(config).create(card)` |
| Card resolution | `A2ACardResolver(client, url)` | `A2ACardResolver(client, url)` (same) |
| Well-known path | `/.well-known/agent.json` | `/.well-known/agent-card.json` |
| Server app | `A2AStarletteApplication` | Starlette + `create_agent_card_routes` + `create_jsonrpc_routes` |
| AgentCard type | Pydantic model | Protobuf message |

This project targets the **installed SDK's** API (`a2a-sdk` from PyPI), not the GitHub sample code.

### Key lessons

1. **messageId is required** on every A2A Message
2. **agentInterface must match**: `ClientFactory` requires `supported_interfaces` with `protocol_binding="JSONRPC"`
3. **Timeout propagation**: Both `ClientConfig` + `httpx.AsyncClient` AND `ClientCallContext` must be configured
4. **Response format**: Sub-agents return `data` (structured) not `text` — extract via `get_data_parts()` from `a2a.helpers`
5. **Streaming events**: The streaming `BaseClient.send_message()` yields `StreamResponse` events. Intermediate `SUBMITTED`/`WORKING` status updates must be skipped — only process `artifact_update` events and terminal `COMPLETED`/`FAILED` states
6. **Data > text**: Sub-agents using `GenericAgentExecutor` emit `Part(data=Value(struct_value=s))` for structured responses. `get_stream_response_text()` misses these — use `get_data_parts()` on artifact parts directly

### How our A2A pattern compares to reference projects

| Aspect | Google Samples | bhancockio/agent2agent | theailanguage/a2a_samples | FinSight |
|---|---|---|---|---|
| Client creation | `ClientFactory` | `A2AClient(httpx, card, url)` | Custom `A2AClient` (httpx POST) | `ClientFactory` (SDK current) |
| Streaming | Streaming (events) | Non-streaming (single response) | Non-streaming (single task) | Streaming with correct event routing |
| Host tools | `send_message(name, msg)` | `send_message(name, task)` | `delegate_task(name, msg)` | `send_message(name, task)` |
| list_agents tool | Some have it (unused) | No | Yes | Removed — agents in prompt |
| Sub-agent response | Parts + artifacts | Parts from JSON | Task history text | Data parts then text then fallback |
| Discovery background | `loop.create_task()` | `asyncio.run()` at module | Not async | Both paths (loop/run) |

## Orchestrator Evolution

### v1 — REST Gateway + Planner
Raw Starlette REST API (`gateway.py`) with regex-based `planner.py`, custom `A2AClient`, and `report_generator.py`. Three overlapping orchestrator files.

**Problems**: Duplicated logic, no A2A-native protocol handling, manual HTTP endpoints.

### v2 — Dynamic Per-skill ADK Tools
ADK `LlmAgent` with one tool per agent skill, generated dynamically at module import. MCP + seed URL discovery.

**Problem**: Module-level `asyncio.run(create_agent())` fails when ADK Web UI imports the module.

### v3 — Thread-based Async Initialization
Wrapped `asyncio.run()` in a thread to bypass the running event loop restriction.

**Problem**: httpx `RuntimeError: Event loop is closed` — connections created in thread's loop, used from main loop.

### v4 — Sync Discovery + Lazy Async A2A
Sync `httpx.Client` for startup discovery (no event loop needed). A2A clients lazily via `create_client()` on first tool call.

**Problem**: Sync HTTP for discovery was non-standard, didn't use `A2ACardResolver`. Each sub-agent was one ADK tool — LLM couldn't call them in parallel anyway.

### v5 — A2A Sample Pattern (current)
Background async `A2ACardResolver` discovery (standard well-known endpoint). `ClientFactory` for transport. Single `send_message` tool (LLM routes by name). Correct streaming event handling. No pre-fetch.

**Key insight**: Match the pattern of ALL reference projects — one `send_message` tool, LLM routes sequentially, no `list_remote_agents` (agents are in prompt already).

## Problems Encountered

### 1. `asyncio.run()` and Running Event Loops

**Problem**: Module-level `asyncio.run()` fails when ADK Web UI imports the module (already has a running event loop).

**Final solution**: Check for an existing loop first. If one is running, use `loop.create_task()` for background discovery. Otherwise, use `asyncio.run()`.

### 2. httpx Event Loop Conflicts from Threaded Init

See v3 above. `httpx.AsyncClient` is never created in a thread or at module level.

### 3. httpx.Timeout Constructor Ambiguity

`httpx.Timeout(read=300.0, connect=10.0)` fails — must pass all four or a single value. Use `httpx.Timeout(300.0)`.

### 4. Sub-agent Responses in `data` Format Not Extracted

**Problem**: `get_stream_response_text()` only returns text. Sub-agents return structured `data` parts. Our code got empty results.

**Fix**: Check `get_data_parts(artifact.parts)` first, then fall back to `get_artifact_text(artifact)`.

### 5. Streaming Event Handling — Early Return on WORKING

**Problem**: `get_stream_response_text()` extracts text from ANY event including `WORKING` status messages. The LLM got `"Running Financial RAG Agent..."` as the "result" and called `send_message` again in an infinite loop.

**Fix**: Route events by type: skip `SUBMITTED`/`WORKING` status updates, only process `artifact_update` (data or text), terminal `status_update`, and terminal `task` events.

### 6. Non-terminal Task Events Returned as Result

**Problem**: The first streaming event is `task { state: SUBMITTED }`. Without checking terminal state, this was returned as `{"id": "xxx", "state": 1}`, confusing the LLM.

**Fix**: Check `task.status.state not in _TERMINAL_STATES` before processing a task event.

### 7. Local LLMs Don't Support Parallel Function Calling

**Problem**: The LLM instruction says "call ALL agents simultaneously" but no local or low-end model supports parallel tool calling.

**Initial solution**: Use a single `send_message` tool. The LLM called agents sequentially.

**Resolution with qwen**: The `qwen3-30b-a3b-2507` model supports parallel function calling, calling `send_message` for multiple agents simultaneously. The single `send_message` tool pattern is retained — agents marked as requiring sequential execution will be serialized by the LLM.

### 8. MCP Resource URI Type Mismatch

`'AnyUrl' object has no attribute 'startswith'` — convert `AnyUrl` to string with `str(uri)`.

### 9. ClientConfig Has No `timeout` Parameter

Pass pre-configured `httpx.AsyncClient(timeout=httpx.Timeout(300))` via `ClientConfig(httpx_client=http)`.

### 10. LLM Tool Name Hallucination

Small local models generated wrong tool names. Fixed by: single `send_message` tool (no name to get wrong).

### 11. Agent Name Validation Error

`LlmAgent(name="FinSight Orchestrator")` — spaces not allowed. Use `name="orchestrator"`.

### 12. Slow-Starting Sub-agents Not Discovered

`discover()` retries each URL 3 times with 5-second delay.

### 13. MCP Registry Discovery Not Ported

MCP resource-based agent card discovery is pending future work.

### 14. Windows ConnectionResetError Noise

`ConnectionResetError: [WinError 10054]` on Windows after successful A2A calls. Caused by ProactorEventLoop shutting down sockets already closed by the remote side.

**Fix**: `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` on Windows.

**v1.19 complement**: All sub-agent executors now call `await self._disconnect()` → `mcp.disconnect_all()` in a `finally` block after each stream. This prevents lingering MCP sockets from triggering the error regardless of event loop policy.

### 15. AgentCard Protobuf — No `url` Field

**Problem**: The SDK's `AgentCard` is a protobuf message with no `url` field. Construction like `AgentCard(url="http://...")` raises `ValueError`.

**Fix**: Set `supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=...)]` instead.

## Background Async Discovery

The ADK Web UI imports the agent module synchronously. Using `asyncio.run()` at module level fails if a loop is already running. Using threads caused httpx event loop conflicts.

**Solution**: Detect running loop at import time:
- Loop running → `loop.create_task(discover_background())`
- No loop → `asyncio.run(discover_background())`

## Timeout Strategy

The default `create_client()` creates an httpx client with default timeouts (~5s). Sub-agent analyses routinely exceed this.

**Fix**: Pass a pre-configured `httpx.AsyncClient(timeout=httpx.Timeout(300))` via `ClientConfig(httpx_client=http)`.

## Model Selection (Ollama Era)

| Model | Verdict | Reason |
|---|---|---|
| `qwen2.5:7b` | ✅ | Reliable tool calling, good instruction following, ~4.7GB |
| `llama3.2` (3B) | ❌ | Tool calling unreliable via both `ollama/` and `openai/` providers |
| `deepseek-r1` (7B) | ❌ | Does not support tool/function calling |

**Key**: The `openai/` prefix (LiteLLM OpenAI-compatible provider) sends tool definitions in the correct format.

## Migration from Ollama to LM Studio

### Problem: Ollama was too slow

Ollama's inference speed for `qwen2.5:7b` was 20-40 seconds per LLM call. With the orchestrator calling all three sub-agents sequentially, a single query took 2-3 minutes.

### Solution: LM Studio

LM Studio provides faster inference, OpenAI-compatible API, simpler setup.

### Changes made

| Area | Before (Ollama) | After (LM Studio) |
|---|---|---|
| Base URL | `http://localhost:11434/v1` | `http://localhost:1234/v1` |
| Model name | `qwen2.5:7b` | `gpt-oss-20b` |
| Agent 1 (ADK) | `openai/qwen2.5:7b` | `openai/gpt-oss-20b` |
| Agent 2 (LlamaIndex) | `llama-index-llms-ollama` | `llama-index-llms-openai-like` |
| Agent 3 (LangGraph) | `langchain-ollama` | `langchain-openai` |
| Agent 4 (CrewAI) | `CrewLLM(model="ollama/...")` | `CrewLLM(model="gpt-oss-20b")` |

## MCP Server Design

The unified finsight MCP server (`finsight_server.py`) hosts agent registry + data tools on port 8010.

### Docker-compose Alignment Fix

**Problem**: `docker-compose.yml` referenced 4 separate MCP services (`mcp-yfinance`, `mcp-sec-edgar`, `mcp-reddit`, `mcp-python-runner`) that each ran different server files (`yfinance_server.py`, etc.). These files didn't exist — only the unified `finsight_server.py` existed in the repository.

**Solution**: Replaced the 4 broken services with a single `finsight-mcp` service that runs the existing `finsight_server.py`. Updated agent environment variables to point to the unified server (`MCP_SERVER_URL=http://finsight-mcp:8010/sse`). The actual codebase was already using this pattern — docker-compose was simply out of sync.

### Lazy Agent Registry

**Problem**: `sentence-transformers` downloads the embedding model (~80MB) at import time. When ADK Web UI or MCP host imports the module, model download blocks startup and may fail in restricted environments.

**Solution**: Defer model loading to first tool call via `_ensure_registry()` with `asyncio.Lock` double-checked locking. Model is loaded once in a thread executor, never at module level.

### Windows Compatibility

**Problem**: `import resource` (Unix RLIMIT) raises `ModuleNotFoundError` on Windows.

**Solution**: Guard with `if sys.platform != "win32": import resource`. Sandbox `preexec_fn` is `None` on Windows (RLIMIT is Unix-only).

### Thread-Safe SSE App Singleton

**Problem**: FastMCP's `sse_app()` creates a new Starlette app instance each call. Under concurrent reload or multi-worker setups, this duplicates middleware, routes, and lifecycle hooks, causing `RuntimeError: Lifespan context has already been started`.

**Solution**: `get_app()` with a `threading.Lock` double-checked singleton pattern.

### Inline Imports for Localised Scope

**Problem**: Top-level `import re` creates a module-wide reference. In sandbox contexts or when the module is reloaded, shadowed or patched `re` can break internal normalisation logic.

**Solution**: `import re as _re` inside `_resolve_company_keywords` and `_normalise_for_match` — guarantees a fresh, unpatched reference.

### SEC EDGAR Caching

**Problem**: Every `get_company_filings` call re-fetched the company ticker → CIK mapping (~4MB JSON from SEC.gov), adding latency and hitting SEC rate limits.

**Solution**: `_EdgarClient._get_ticker_map()` with `asyncio.Lock` lazy loading. CIK results cached in `_cik_cache`, ticker→title map cached in `_title_map`. Subsequent calls are dict lookups.

### Sandbox Hardening

**Problem**: The Python sandbox allowed potentially dangerous imports (`builtins`, `gc`, `threading`, `multiprocessing`, etc.) that could be used to escape the subprocess.

**Solution**: Expanded `_RESTRICTED_IMPORTS` and `_RESTRICTED_ATTRS` blocklists. Subprocess runs with `-I` (isolated) and `-S` (no site) flags. RLIMIT applied on Unix.

## Model Change: gpt-oss-20b → qwen

The LLM used by all agents was switched from **`gpt-oss-20b`** to a **qwen** model:

| Model | Speed | Notes |
|---|---|---|
| `gpt-oss-20b` (previous) | ~40-60s per call | Large, slower inference |
| `qwen3-30b-a3b-2507` (current) | ~5-10s per call | Much faster, sufficient quality |

**Key**: The qwen model reduced per-call latency by ~5-10x while maintaining adequate output quality for all agent tasks (routing, summarisation, analysis). This was the single biggest performance improvement in the pipeline.

## RAG Agent Auto-ingest

The RAG agent fetches SEC filings via MCP on first query (`_ensure_ingested`). Was fragile with `json.loads()` on potentially empty MCP responses. Fixed with proper empty-check and `try/except json.JSONDecodeError`.

### RAG Content Ingestion Fix

**Problem**: RAG agent only stored SEC filing metadata (form type, description, URL) in ChromaDB, not actual filing content. Queries returned "cannot be performed based on provided information" because the index had no meaningful text.

**Solution**: 
1. Added `get_filing_content(edgar_url, ix_url)` MCP tool to fetch and extract text from raw EDGAR documents
2. Updated `get_company_filings` to return both raw document URL (`edgar_url`) and IXBRL viewer URL (`ix_url`)
3. RAG agent now calls `get_filing_content()` for each filing and stores extracted text (up to 20K chars) into ChromaDB
4. Enhanced `get_filing_content` to handle multiple content types (HTML, XML, JSON), skip XBRL viewer pages, and fallback to IX URL if raw fails

### Quant DCF Null Fix

**Problem**: DCF valuation always returned `null` because `_get_fcf_from_financials()` looked for "Free Cash Flow" in the `income_statement` dict, but FCF belongs in the `cash_flow` statement.

**Solution**: Changed `dcf_valuation_node()` to use `data.get("cash_flow", {})` instead of `data.get("income_statement", {})`.

## Ticker Extraction Decoupled from SEC Validation

### Problem

`extract_ticker()` in `shared/ticker_utils.py` fetched the full SEC company_tickers.json (~4MB) on every call to validate candidates. This meant:
1. Every agent query triggered a network call to SEC.gov just to extract a ticker
2. `RAGAgent.stream()` had a broken `await self._connect()` call (method didn't exist)
3. Validation logic was duplicated inline across three agent executors with subtle differences
4. No graceful fallback when MCP was unavailable

### Solution

**Step 1 — Pure regex extraction**: `extract_ticker()` now returns the first regex match immediately with no network calls. Priority cascade: parentheses > trigger words > $ prefix > 3-5 letter words > 2 letter words.

**Step 2 — MCP-based validation**: Each agent has a `_validate_ticker()` method that:
1. Connects MCP if not already connected
2. Calls the MCP `validate_ticker` tool (which talks to SEC EDGAR)
3. Returns `(is_valid, ticker, company_or_error)` as a uniform `tuple[bool, str, str]`

**Step 3 — First-match heuristic**: Pattern 4 (`\b([A-Z]{3,5})\b`) returns `matches[0]` (first match) rather than `matches[-1]` (last match). Rationale:

- The orchestrator LLM generates task text like *"Analyze WMT (Walmart) SEC filings for..."* — the ticker appears **first**, stop words like "SEC", "EPS", "NYSE", "INC" appear **after** it
- `matches[-1]` picked up trailing stop words: "SEC" instead of "WMT" in the task above
- `matches[0]` prefers the ticker that was mentioned first
- If the first match is wrong, `_validate_ticker` rejects it → `resolved` fallback → company name resolution catches the real ticker

**Step 4 — Validation fallback to resolution**: When `extract_ticker` returns a candidate that `_validate_ticker` rejects (e.g. regex picks up "SEC" from task context), the agent retries with `_resolve_ticker` (company name resolution) before returning an error. This creates a three-layer defense:

1. **Regex first** (instant) — catches explicit tickers: "(AAPL)", "$V", "for MA"
2. **Company name resolution** (SEC reverse index + Yahoo fallback) — catches natural language: "Mastercard" → "MA"
3. **Validation** (SEC EDGAR) — confirms ticker exists, used as gate for all of the above

**Step 5 — Ticker format gate**: `is_valid_ticker_format()` rejects anything that doesn't match `^[A-Z]{1,5}(\.[A-Z]{1,2})?$` — prevents mutual fund identifiers ("0P0000SECP.F") and other non-equity symbols from reaching validation.

**Key properties**:
- ✅ Extraction never fails — pure regex, no network calls
- ✅ Validation is optional — MCP validation has fallback to raw regex guess
- ✅ No SEC API from agent side — only MCP server talks to SEC
- ✅ MCP server caches — SEC map loaded once, cached per server lifetime
- ✅ Backward compatible — all existing patterns still work

### Consolidating `_validate_ticker` / `_resolve_ticker` Across Agents (v1.16)

**Problem**: The MCP call + JSON-parsing logic of `_validate_ticker()` and `_resolve_ticker()` was copy-pasted verbatim (~108 LOC) across all three sub-agent executors. A bug fix or protocol change had to be applied in three places with no guarantee of consistency.

**Solution**: Extracted `validate_ticker_via_mcp(mcp, ticker)` and `resolve_ticker_via_mcp(mcp, query, exclude_ticker)` into `shared/ticker_utils.py`. Each agent's `_validate_ticker` / `_resolve_ticker` methods are now ~7-line wrappers that handle their own connection logic, then delegate the actual MCP call to the shared functions.

**Why keep the wrapper methods instead of calling shared functions directly from `stream()`?** Each agent connects to MCP differently — RAG agent uses lazy connect with `_ensure_mcp_connected()`, Quant uses `_ensure_connected()` (reused by `analyze()`), Sentiment uses `_connect()`. Keeping thin wrapper methods preserves per-agent connection semantics and leaves `stream()` unchanged.

## Financial Filings Tool (get_financial_filings)

### Problem

`get_company_filings()` returned all recent filings in order, but for large financial companies the "recent" batch was dominated by 8-Ks (current reports filed nearly daily). A request for `limit=10` might return 0-1 actual 10-K or 10-Q statements, leaving RAG agents with no financial data to analyze.

### Solution

Added `get_financial_filings()` that fetches 10-K and 10-Q filings separately with independent limits:

```
annual_limit=5    → up to 5 years of 10-Ks
quarterly_limit=8 → up to 2 years of 10-Qs
```

If the initial "recent" batch doesn't contain enough 10-Ks, it paginates to older filings pages. The response separates annual from quarterly so downstream agents can distinguish yearly trends from quarterly updates.

## News System: Concurrent RSS + Yahoo Finance Fallback

### Problem

The original RSS pipeline had three issues:
1. Feeds were fetched **sequentially** — if MarketWatch timed out, CNBC and Yahoo waited
2. No failure diagnostics — a blank response looked identical to "no news for this ticker"
3. No fallback — if all three RSS feeds returned zero matches, the agent got empty news with no explanation

### Solution

**Concurrent fetching**: All three RSS feeds are fetched simultaneously via `asyncio.gather()`. A slow/unreachable feed doesn't block the others.

**Structured return values**: `_fetch_rss()` returns `{"entries": [...], "status": "ok" | "http_xxx" | "parse_error" | "error", "error": "..."}`. Each source gets an entry in `feed_status` so agents can see which feeds worked.

**Yahoo Finance news API fallback**: When all RSS feeds are unreachable **or** return zero keyword-matched articles, `_fetch_yf_news()` queries Yahoo Finance's structured `v1/finance/search` API. Unlike RSS, results are pre-filtered to the ticker — no keyword matching needed.

**Better diagnostics**: `source_used` tells the agent whether results came from RSS or the Yahoo fallback. `feed_status` shows per-source HTTP status codes. The response distinguishes:
- `rss_unreachable` — feeds returned errors
- `rss_no_match` — feeds returned articles but none matched the ticker

### MCP Response Parsing Inconsistency

**Problem**: Each agent parsed MCP tool responses differently (checking for `.content`, `.text`, dict vs list, etc.), leading to fragile error handling.

**Solution**: Added `parse_mcp_result(result)` utility in `shared/mcp_client.py` that handles various MCP response formats consistently — returns parsed dict/list/string or `{"error": "..."}` on failure.

## DCF Skipped from High Volatility Routing

### Problem

`dcf_valuation` returned `null` for tickers with annual volatility above 35% (e.g. Oracle at 41%). The graph's `_route_on_volatility` function routed these to `stress_test` and DCF was never called. The `dcf_error` field was `null` too — making it impossible to distinguish "DCF failed" from "DCF was never executed".

### Solution

Three changes across the graph pipeline:

1. **Set `dcf_error` in `compute_metrics_node`**: When `annual_vol > 0.35` is detected, the metrics node now includes a descriptive `dcf_error` message (e.g. "DCF skipped: annual volatility (41.0%) exceeds 35% threshold – routed to stress test instead"). This is set *before* the routing decision, so it's available in state regardless of which path is taken.

2. **Surface `dcf_error` in `graph.run()` output**: The result dict now includes the `dcf_error` field so callers can see why DCF is null.

3. **Include `dcf_error` in reasoning**: `format_output_node` appends the error to the reasoning string when DCF is null with an error, so the LLM summary has full context.

### Key properties
- ✅ Callers can distinguish "DCF not computed" from "DCF failed to compute"
- ✅ The error reason appears in both structured output (`dcf_error` field) and natural language summary (reasoning text)
- ✅ No false positives — only set when volatility routing actually causes the skip

## DCF Null from Negative Free Cash Flow

### Problem

`_get_fcf_from_financials()` returned the most recent period's FCF regardless of sign. For financial companies like JPM, the latest year's FCF was negative (large capital expenditures / investment purchases), causing the `latest_fcf <= 0` guard to return `{"dcf_valuation": None}` with no explanation.

### Solution

Changed `_get_fcf_from_financials()` to iterate through periods and return the **first positive FCF** instead of the first period's raw value. Added comprehensive failure logging:

1. **No MCP client** — logs "no MCP client" 
2. **Empty response** — logs "MCP returned empty response"
3. **No cash flow data** — logs "no cash flow data available"
4. **No positive FCF** — logs actual FCF values from both `Free Cash Flow` field and `Operating Cash Flow + Capital Expenditure` calculation
5. **Missing shares outstanding** — logs the invalid value
6. **Missing current price** — logs the invalid value

All failures return a `dcf_error` string alongside `dcf_valuation: null`, which is surfaced in the agent response.

## Ticker False Positives from Financial Acronyms

### Problem

Pattern 4 of `extract_ticker()` (`\b([A-Z]{3,5})\b`) matched any uppercase word of 3-5 characters, including common financial acronyms like "SEC", "EPS", "CEO", "NYSE", "NASDAQ". When a user asked "Analyze General Electric SEC filings", the first match was "SEC" instead of "General Electric".

Validation rejected "SEC", but the fallback `_resolve_ticker()` passed the full noisy query "Analyze General Electric SEC filings for recent financial performance" to MCP's `resolve_company_ticker`. The SEC reverse index could not reliably match against all those noise words, and Yahoo Finance occasionally returned irrelevant tickers.

### Solution

**Step 1 — Financial stop-word blocklist**: Added `_FINANCIAL_STOP_WORDS` in `shared/ticker_utils.py` — a curated set of 30+ financial acronyms that are never valid stock tickers. Applied to both pattern 4 (3-5 letter) and pattern 5 (2 letter) regex results.

```python
_FINANCIAL_STOP_WORDS = frozenset({
    "SEC", "EPS", "CEO", "CFO", "NYSE", "NASDAQ",
    "INC", "LLC", "LTD", "CORP", "GAAP", "EBIT", "EBITDA",
    ...
})
```

**Step 2 — Query noise cleanup**: Added `clean_query_for_resolution()` that strips:
- Common financial analysis words ("analyze", "filings", "financial", "performance", "sec", "edgar")
- Generic English stop words ("the", "a", "for", "about", "this", "that")
- Words from `_FINANCIAL_STOP_WORDS` (uppercase variants like "SEC", "INC")

**Step 3 — Exclude ticker from resolution**: `_resolve_ticker(query, exclude_ticker="SEC")` strips the regex-extracted false positive from the query before calling MCP. So "Analyze General Electric SEC filings" → "General Electric" after cleanup.

**Key properties**:
- ✅ No false positives from financial jargon
- ✅ Company name resolution receives clean input
- ✅ Failed ticker is excluded from the resolution query
- ✅ All three agents (RAG, Quant, Sentiment) apply the same cleanup
- ✅ Backward compatible — all existing patterns still work

## Portfolio Holdings Extraction for Correlation Analysis

### Problem

The Quant agent's `correlation_matrix` was always `{}` even when users explicitly mentioned portfolio holdings. The `correlation_node` in `nodes.py` requires `portfolio_holdings` (a list of ticker symbols) to compute correlations, but the chain never populated it:

```
stream() → analyze(ticker) → graph.run(ticker, portfolio_holdings=None)
```

The `QuantAgent.stream()` method extracted the target ticker from the query but had no logic to extract portfolio holdings. Even though `graph.run()` accepted a `portfolio_holdings` parameter, it was always passed as `None`.

### Solution

**Step 1 — `extract_holdings()` in `shared/ticker_utils.py`**: Four regex patterns covering natural language phrasing:

```python
_HOLDINGS_PATTERNS = [
    # "My portfolio holds AAPL, MSFT, GOOGL"
    # "My portfolio: TSLA, AMZN, META"
    re.compile(r"(?:portfolio|holdings?)\s*(?::|holds?|contains?|includes?|consists\s+of)\s*..."),
    # "I own MSFT and GOOGL"
    # "my current portfolio includes AAPL, TSLA"
    re.compile(r"(?:I\s+(?:own|hold|have|am\s+invested\s+in)|my\s+...portfolio...)\s+..."),
    # "My current holdings are JPM, BAC, WFC"
    re.compile(r"(?:my\s+...portfolio...)\s+are\s+..."),
    # "currently own AAPL, MSFT"
    re.compile(r"(?:currently\s+)?(?:own|hold|have)\s*:?\s*..."),
]
```

Each pattern captures a comma-and-separated list of uppercase tickers. The `exclude_ticker` parameter removes the target stock from the holdings list.

**Step 2 — Pass holdings through the chain**: `stream()` calls `extract_holdings(query, exclude_ticker=ticker)`, passes to `analyze(portfolio_holdings=holdings)`, which passes to `graph.run(portfolio_holdings=holdings)`.

**Step 3 — Orchestrator LLM instruction updated**: Added step 4 to the orchestrator system prompt telling the LLM to include portfolio holdings in the task text for the Quant Analysis Agent. Without this, the LLM would drop holdings from the generated task.

**Step 4 — Helpful notes instead of empty `{}`**: When no holdings are provided, `correlation_node` returns `{"note": "No portfolio holdings provided..."}`. When price data is insufficient, returns `{"note": "Insufficient overlapping price data..."}`. On exception, returns `{"error": "..."}`.

### Key properties
- ✅ Holdings extraction is pure regex — no network calls, instant execution
- ✅ Target ticker excluded from holdings to avoid self-correlation
- ✅ Works with comma-separated, "and"-connected, and mixed formats
- ✅ Backward compatible — returns `[]` when no holdings mentioned

### Correlation Matrix Auto-Trigger via Memory Context (v1.16 Fix)

**Problem**: The orchestrator's `_build_memory_context()` appended stored portfolio holdings from `PortfolioStore` to every query as `"User portfolio: GOOGL, AAPL, META, MSFT"`. This line was injected into `[MEMORY CONTEXT]` before the user's actual message. The orchestrator system prompt said "if the user mentions portfolio holdings, include them in the Quant agent task". The LLM treated the memory line as an explicit user mention and forwarded the holdings — triggering a full correlation matrix for every single-ticker query, even when the user never asked for one.

**Root cause**: No distinction between "user said this right now" vs "system recalled this from memory". The memory context and direct user input were semantically indistinguishable to the LLM.

**Fix (two-pronged)**:
1. **Label the memory line explicitly**: Changed `"User portfolio: ..."` to `"Background — user's known holdings (do NOT include for portfolio correlation unless the user explicitly requests it in their current message): ..."`. The label itself instructs the LLM how to treat the data.
2. **Update the orchestrator prompt**: Changed step 4 from "if the user mentions portfolio holdings" to "only if the user EXPLICITLY mentions their portfolio or asks for correlation in their CURRENT message — do NOT include holdings from memory context background lines."

**Why two changes instead of one?** The label alone relies on the LLM parsing a long inline instruction inside the injected text. The prompt change alone could be forgotten or overridden by conflicting signal in the memory line. Together they create redundant clarity: the data labels itself as background-only, and the instruction explicitly excludes memory-sourced holdings from auto-forwarding.

## File Logging Design

### Problem

All five server entry points used `logging.basicConfig(level=logging.INFO)` either at module level or inside `if __name__ == "__main__":` blocks. This meant:
1. Services started via `uvicorn` (the normal path) never called `basicConfig` — the sub-agent `server.py` files only configured logging when run directly
2. All output went to stderr only — no log files were written
3. `memory_callback.log` was written to the project root instead of `logs/`
4. Each service independently configured logging with no shared format or rotation policy

### Solution

`shared/logging_config.py` provides a single `setup_file_logging(service_name)` function that:
- Attaches a `RotatingFileHandler` (10 MB, 5 backups) → `logs/<service>.log`
- Attaches a `StreamHandler` (stderr) if none is present yet
- Creates the `logs/` directory if absent
- Guards against duplicate handler registration (idempotent)

Each server calls `setup_file_logging(...)` at module level, not inside `if __name__ == "__main__":`, so logging is configured regardless of whether the process is started via uvicorn or run directly.

### Why `RotatingFileHandler` instead of `TimedRotatingFileHandler`?

Size-based rotation is simpler to reason about in a development context. The services generate bursts of logs during queries then go idle — time-based rotation would create many small empty files. 10 MB per file with 5 backups gives 50 MB total per service, enough for days of normal usage without manual cleanup.

### Why not configure via `logging.config.dictConfig`?

A dict config would require all services to share a config file or inline the same config dict — moving complexity, not removing it. A single function call with a service name is the simplest interface that solves the problem.

## Langfuse Span Noise Filtering

### Problem

With `should_export_span=lambda span: True`, Langfuse exported every single span including noisy A2A internal spans. Each A2A `send_message` call generated multiple internal spans from the `a2a-python-sdk` instrumentation scope (HTTP transport, JSON-RPC serialization, event handling). As the number of agents grew, this made Langfuse traces extremely noisy and hard to debug.

### Solution

Use Langfuse's built-in `is_default_export_span` helper which exports spans only from:
- `langfuse-sdk` scope (our manual `start_observation` calls — high-level workflow)
- `gen_ai.*` attribute spans (actual LLM calls)
- Known LLM instrumentors (`litellm`, `openinference.*`, `langsmith`, `haystack`, `agent_framework`, etc.)

This filters out `a2a-python-sdk`, `opentelemetry.instrumentation.httpx`, and other infrastructure scopes automatically.

### What's exported vs filtered

| Span Type | Instrumentation Scope | Exported? |
|---|---|---|
| `finsight-query` trace | `langfuse-sdk` | ✅ |
| `orchestrator-execute` | `langfuse-sdk` | ✅ |
| `rag-agent-stream` | `langfuse-sdk` | ✅ |
| `quant-agent-stream` | `langfuse-sdk` | ✅ |
| `sentiment-agent-stream` | `langfuse-sdk` | ✅ |
| LLM calls | `litellm`, `openinference.*` | ✅ |
| LangGraph nodes | `langfuse-sdk` (via CallbackHandler) | ✅ |
| A2A `send_message` internal | `a2a-python-sdk` | ❌ |
| A2A `DefaultRequestHandler` | `a2a-python-sdk` | ❌ |
| HTTPX transport spans | `opentelemetry.instrumentation.httpx` | ❌ |

### Tradeoff

If you need to **temporarily debug** and see all spans (including A2A internals), switch back to `should_export_span=lambda span: True`. The default filter is the recommended production setting per [Langfuse maintainer guidance](https://github.com/orgs/langfuse/discussions/8366).

## HF_HUB_OFFLINE Default

### Problem

HuggingFace `sentence-transformers` and `transformers` make network calls to `huggingface.co` at import time to check for model updates, download missing files, and verify cached model integrity. In an air-gapped or offline development environment (or during Docker builds without internet), these checks fail with `OSError: Can't load model ...` or hang waiting for a timeout. The `all-MiniLM-L6-v2` model is large enough (~80MB) that re-download on every cold start is both slow and wasteful.

### Solution

`shared/config.py` sets `os.environ["HF_HUB_OFFLINE"] = "1"` at import time, before any HuggingFace code is imported or executed. This tells the `huggingface_hub` library to skip all network calls — it assumes models are already cached locally from a prior online run.

**Why at the top of `config.py` instead of in `.env`?** HuggingFace libraries read `HF_HUB_OFFLINE` at import time via `os.environ.get()`. If `.env` is loaded later (e.g. after the `config` import chain), the embedding model import happens before the env var is set. Setting it via `os.environ.setdefault()` at module level guarantees it's in place before any HuggingFace code runs.

**Tradeoff**: If a model is missing from the local cache, the error is a hard crash (`OSError`) instead of an automatic download. Set `HF_HUB_OFFLINE=0` in `.env` to re-enable downloads.

## Langfuse Distributed Tracing Across Processes

### Problem

Each agent runs in a separate OS process (uvicorn on its own port). When a sub-agent calls `langfuse.start_observation()` it creates a brand new root trace — Langfuse has no way to know that the sub-agent trace belongs inside the orchestrator's trace. This resulted in 4 disconnected traces per query:

```
Trace A: orchestrator-execute   [pid: 8001]
Trace B: rag-agent-stream        [pid: 8002]   ← orphan
Trace C: quant-agent-stream      [pid: 8003]   ← orphan
Trace D: sentiment-agent-stream  [pid: 8004]   ← orphan
```

### Solution

**Text-based trace context injection via A2A payload:**

1. **Orchestrator** (`sub_agent_client.py`): Extracts `trace_id` and `parent_span_id` from the current Langfuse context via `lf.get_current_trace_id()` and `lf.get_current_observation_id()`. Serializes them as a JSON prefix: `{"_trace": {"trace_id": "...", "parent_span_id": "..."}}\n<<<TASK>>>\n{task_text}`.

2. **Sub-agents** (`executor.py`): Extract the prefix via `extract_trace_ids(query)`, rebuild the `trace_context` dict, and pass it to `langfuse.start_observation(..., trace_context=trace_ctx)`. Langfuse uses the `trace_id` to join the existing trace and `parent_span_id` to set the parent observation.

3. **LangGraph CallbackHandler**: Quant agent additionally passes `trace_context` to `CallbackHandler(trace_context=trace_ctx)` so all internal graph nodes are linked to the parent trace.

### Why not OpenTelemetry W3C TraceContext headers?

The A2A SDK controls the HTTP transport layer. Injecting custom headers would require modifying the SDK client or using httpx event hooks. The text-prefix approach is simpler, already partially implemented, and works reliably across all A2A transports (JSON-RPC, HTTP+JSON).

### Why `start_observation()` not `start_as_current_observation()`?

`start_as_current_observation()` is a context manager that manages OTel context tokens. In async generators, the context token is created in one async context but the generator yields control to a different context, causing `ValueError: Token was created in a different Context`. `start_observation()` creates the span manually without OTel context management, avoiding the conflict. The span's `.end()` is called in the `finally` block.

### Result

```
Trace A: finsight-query [ticker=NVDA]
├── orchestrator-execute
│   ├── send_message → Financial RAG Agent
│   │   └── rag-agent-stream (child of orchestrator-execute)
│   ├── send_message → Quant Analysis Agent
│   │   └── quant-agent-stream (child of orchestrator-execute)
│   │       ├── fetch_prices (LangGraph node)
│   │       ├── compute_metrics
│   │       ├── run_dcf / run_stress_test
│   │       └── llm_summary
│   └── send_message → Sentiment Intelligence Agent
│       └── sentiment-agent-stream (child of orchestrator-execute)
```

## Date Hallucination

### Problem

All LLM prompts lacked temporal context. Since the model's training data cut off before 2026, it treated 2026 filing dates as "future-dated anomalies" and instructed users to wait for "Q1 2024 earnings" — two-year-old data.

### Solution

Added `Today's date: {date.today().isoformat()}` as the first line of every LLM prompt:
- **Orchestrator system prompt** — so it includes the date when constructing sub-agent tasks
- **RAG query prompts** — so the LlamaIndex LLM knows the reference date for financial data
- **Quant summary prompt** — so the LangGraph summary LLM frames analysis in correct temporal context
- **Sentiment crew tasks** — so CrewAI agents know the current date when analyzing news and filings

## SQLite Singleton Connection with Write Lock

### Why a singleton connection instead of open/close per call?

The original `get_db()` opened a new `aiosqlite` connection on every function call — every ticker lookup, portfolio read, memory search, and write created + tore down a connection. Under load, this meant 10-20 open/close cycles per query across the memory layer.

SQLite connections are not free: each open acquires a file handle, sets pragmas (WAL, foreign_keys, busy_timeout), and runs schema migration. For a file-backed database, opening a connection also involves a filesystem `open()` call, which under concurrent access (multiple agents) causes lock contention on the `.db` file itself — even for reads.

The singleton pattern eliminates this entirely: one connection is opened once, WAL mode and pragmas are set once, schema migration runs once. All subsequent calls — both reads and writes — reuse the same connection.

### Why a separate write lock instead of relying on SQLite's internal locking?

SQLite in WAL mode supports concurrent reads but serializes writes at the OS level (one writer at a time). Without a lock, two concurrent `await conn.execute("INSERT ...")` calls hit `SQLITE_BUSY` and wait on `busy_timeout` (5000ms). With 3-4 agents writing simultaneously (store_brief, portfolio upsert, performance record, memory persist), this caused 5-second stalls.

The `asyncio.Lock` (`write_lock()`) serializes writes at the Python level before they reach SQLite. The wait is near-instant (microseconds) instead of the full `busy_timeout` (milliseconds). Reads skip the lock entirely — they just call `get_db()` and query, no contention.

### Why not connection pooling (multiple connections)?

SQLite is a single-writer database regardless of connection count. Multiple connections don't help writes — they only complicate the code with pool management, checkout/return patterns, and the risk of connection leaks. A single connection with a write lock is the simplest correct solution.

### Why not use `sqlite3` threading modes?

`aiosqlite` is async-first and already handles thread safety by running all operations on a dedicated background thread. The Python-level `asyncio.Lock` is an additional guard that prevents concurrent callers from queueing multiple writes on the aiosqlite thread — it's defense in depth, not a workaround for a missing feature.

### Key properties
- ✅ One connection, one schema migration, one set of pragmas
- ✅ Writes serialized via `asyncio.Lock` — no `SQLITE_BUSY` stalls
- ✅ Reads contention-free (WAL mode allows concurrent reads on a single connection)
- ✅ 284 lines removed vs 304 added across 5 files — net reduction in connection-management boilerplate
- ✅ `close_db()` for clean process shutdown (no dangling connections)

## Token-Bucket Rate Limiter

### Why a token bucket instead of a fixed-delay sleep?

A fixed `await asyncio.sleep(0.125)` between calls (8/s) would guarantee the rate but wastes 125ms even when no other requests are competing. The token bucket allows **bursts**: 10 consecutive tool calls fire instantly, then the rate smooths to 8/s. This matches real traffic patterns — a quant analysis query triggers 3-5 SEC calls in quick succession (ticker map, submissions, filing content), then nothing for 30+ seconds. The burst handles the batch, the rate limit prevents bans.

### Why separate limiters instead of one global rate limiter?

Three different APIs with three different rate limits and traffic patterns:

| Limiter | Rate | Burst | Applied to | Why |
|---|---|---|---|---|
| `_sec_limiter` | 8/s | 10 | 5 EDGAR HTTP sites | SEC published limit is 10 req/s hard cap; 8/s with 10 burst is conservative while allowing filing batches |
| `_yfinance_limiter` | 4/s | 8 | 4 yfinance tools | Yahoo has no published cap; 4/s is conservative enough to avoid 429s which appeared under the old unthrottled pattern |
| `_rss_limiter` | 2/s | 4 | RSS feeds + Yahoo news fallback | News is the least latency-sensitive — cached by TTL, served stale-is-ok |

A single global limiter would throttle SEC filing fetches because a news RSS fetch happened at the same instant, even though the two APIs have independent rate limits.

### Why the loop form instead of recursion?

The initial plan proposed `await asyncio.sleep(1.0 / self.rate); return await self.acquire()` — a recursive call. Under contention (all 5 SEC sites firing simultaneously), this recurses 5+ levels deep, risking `RecursionError`. The while-loop form is equivalent but iterative — no stack growth regardless of contention depth.

### Why not `asyncio.Semaphore` for burst limiting?

`asyncio.Semaphore` limits concurrency (how many tasks run simultaneously) but does **not** limit rate (how many requests per second). A semaphore of 4 allows 4 concurrent requests every microsecond — no rate enforcement. The token bucket enforces both: at most 10 in a burst, and at most 8/s averaged over time.

### Key properties
- ✅ Burst handling — batch filing requests don't trigger rate limiting
- ✅ Independent limiters — SEC congestion doesn't stall news RSS
- ✅ Iterative wait — no RecursionError under contention
- ✅ Zero dependencies — pure stdlib, 30 lines
- ✅ All sites protected — no unthrottled HTTP path to external APIs

## Async TTL Cache with Single-Flight Dedup

### Why replace the existing `_TTLCache`?

The original `_TTLCache` used `threading.Lock` and had no deduplication. Under concurrent access (all three sub-agents calling `get_prices("NVDA")` at the same time during a single query), each call missed the cache simultaneously, called `yfinance` independently, and produced 3 identical network requests. The cache was also synchronous — `cache.get()` and `cache.set()` were blocking calls in an async context, requiring the old pattern of manual inline caching in each tool function.

The new `TTLCache` is fully async (`asyncio.Lock`), supports `get_or_fetch()` with single-flight dedup, and separates cache management from tool logic.

### How single-flight dedup works

When 5 concurrent callers call `get_or_fetch("prices:NVDA:1y:1d", fetch)` simultaneously:

1. **Callers 1-5**: All check `_data` → miss (empty or expired)
2. **Caller 1**: Acquires `_lock`, re-checks `_data` → still miss, creates `asyncio.Future`, stores in `_inflight`, spawns `_do_fetch` task, releases lock
3. **Callers 2-5**: Queue on `_lock`; each acquires, re-checks `_data` → still miss, but find `key in _inflight` → return the existing Future
4. **All 5**: `await fut` — all unblock when `_do_fetch` calls `fut.set_result(value)`

Result: 1 network call instead of 5. The double-checked pattern (check cache → acquire lock → re-check cache) is critical — if the fetch completes between caller 1's miss and caller 2's lock acquisition, caller 2 finds the cached value and returns immediately instead of waiting for the future.

### Why `asyncio.Future` instead of `asyncio.Event` or a callback queue?

`asyncio.Future` is awaitable by multiple coroutines simultaneously — all callers can `await fut` and all wake when `set_result()` is called. An `Event` requires polling or `wait()` which doesn't propagate the return value. A callback queue requires manual fan-out. `Future` gives exactly the right semantics: one-shot, multi-consumer, value-preserving.

### Why not use `@functools.lru_cache`?

`lru_cache` is synchronous, has no TTL, and doesn't collapse concurrent calls (each call evaluates the function independently). It's also per-process — doesn't help if the same ticker is requested across multiple agent processes. The `TTLCache` solves all three: async-native, TTL-aware, single-flight.

### Why keep separate cache instances per tool type?

Different data has different freshness requirements. A single cache with a global TTL would either serve stale prices or re-fetch immutable filings. Per-tool instances with independent TTLs match the data lifecycle:

| Cache | TTL | Why |
|---|---|---|
| `_cache_prices` | 1 min | Intraday prices change every trade; 1 min balances freshness vs cache utility |
| `_cache_financials` | 1 hr | Quarterly data, but a session refresh may re-query; 24h was unnecessarily long |
| `_cache_news` | 5 min | Headlines publish every few minutes; 15 min was stale by the time user refreshed |
| `_cache_filing` | Permanent (LRU-200) | SEC filings are immutable once filed — never expires |
| `_cache_submissions` | 6 hr | Large JSON blobs that change a few times daily |

### Why `get()`/`set()` still exist alongside `get_or_fetch()`?

Some tools need conditional caching. `get_news_sentiment` only stores results when articles are actually found — empty results are not cached (next call may find news). `get_or_fetch()` doesn't support this "cache only on success" pattern. The `get()`/`set()` pair gives tools fine-grained control over what gets cached and when.

### Key properties
- ✅ N concurrent callers → 1 network request (single-flight)
- ✅ Fully async — no blocking calls in async context
- ✅ Per-tool TTLs match data lifecycle
- ✅ Conditional caching via `get()`/`set()` for tools that gate on result content
- ✅ LRU eviction on `max_entries` — memory bounded
- ✅ Replaces old pattern of manual cache-check-then-fetch in every tool function

## Structured JSON Logging

### Why JSON in the file handler but plaintext in the terminal?

Log aggregators (Loki, CloudWatch, Datadog) ingest JSON natively — no custom parsers needed. A JSON line `{"ts": "...", "level": "INFO", "service": "orchestrator", "message": "Agent response received", "latency_ms": 1200}` is immediately queryable by field. The same line as plaintext `2026-05-27 12:00:00 INFO orchestrator: Agent response received` requires a regex to extract `latency_ms`.

But JSON is painful to read in a terminal during development. `{"ts":"...","level":"INFO",...}` takes ~3× the horizontal space of plaintext and wraps awkwardly. The dual-formatter approach gives both: terminals get plaintext, log files get JSON.

### Why implement a custom `JsonFormatter` instead of using an existing library?

The `python-json-logger` library exists but adds a dependency for ~40 lines of custom code. The `JsonFormatter` in 30 lines:
- Produces the exact payload shape needed (ts, level, service, logger, message + optional extras)
- Supports structured extras via `record.trace_id` etc. — set once on the LogRecord, automatically included
- Picks up `exc_info` and serializes tracebacks as `exc` key
- Uses `json.dumps(default=str)` for serialization safety (handles numpy types, datetimes, etc.)

The cost of the library (version pinning, audit, CI caching) outweighs the benefit for 30 lines of stable code.

### Why `utc` timestamps instead of local time?

Log aggregators operate in UTC internally. Local timestamps (IST, EST, etc.) require timezone-aware parsing at query time, which is fragile across DST changes and deployment regions. UTC ISO-8601 is unambiguous, sortable, and natively supported by every log system. The `ts` field in each JSON line is the exact moment the log was emitted, independent of the server's timezone.

The existing `IST` convention in `shared/config.py` is for *business logic* timestamps (analysis_date, created_at) where users think in local time. Log timestamps are for *operations* — UTC is the standard.

### Why make the StreamHandler idempotency check more specific?

The original check `if any(isinstance(h, logging.StreamHandler) ...)` was broad — it matched any `StreamHandler` including ones not targeting stderr. The refined check narrows to `not isinstance(h, RotatingFileHandler)` to avoid confusing file handlers with stream handlers. This is defense in depth; the actual idempotency is guaranteed by the earlier RotatingFileHandler check on `baseFilename`.

### Key properties
- ✅ JSON file logs ingestible without custom parsers
- ✅ Plaintext terminals remain readable
- ✅ Zero new dependencies (~30 lines of stdlib)
- ✅ Structured extras (trace_id, latency_ms) auto-included when present on LogRecord
- ✅ Backward compatible — `setup_file_logging(service_name)` signature unchanged

## Per-Service Log Levels via Env

### Why `LOG_LEVEL_<SERVICE>` instead of a single `LOG_LEVEL`?

A single `LOG_LEVEL=DEBUG` makes every service chatty — the orchestrator, MCP servers, quant engine, and news fetcher all emit debug lines simultaneously, creating signal-to-noise problems. With per-service overrides, you can set `LOG_LEVEL_MCP=DEBUG` to debug an MCP tool call while keeping `LOG_LEVEL=WARNING` for everything else.

### Why env vars instead of a config file?

Log level is an operational concern, not a code concern. Operators set it in the deployment environment (Docker `-e`, Kubernetes `ConfigMap`, `.env` file) without touching code or config files. Env vars are also the standard for twelve-factor apps and are trivially overridable per-process in supervisor setups.

### Why make `level` parameter `None` by default instead of `logging.INFO`?

The old signature `level: int = logging.INFO` forced env lookup to happen at the caller level — every call site needed `os.environ.get(...)` boilerplate before passing level in. Changing the default to `None` moves the env resolution *inside* the function, where it belongs. Callers that pass `level=` explicitly still override env — best of both worlds.

### Why `service_name.upper().replace('-', '_')` for the env key?

Env var naming conventions use `UPPER_CASE` with underscores. Service names from the codebase may be lowercase or hyphenated (e.g. `finsight-agent`). The transform converts `finsight-agent` → `FINSIGHT_AGENT` → `LOG_LEVEL_FINSIGHT_AGENT`, which reads naturally in an env file.

### Key properties
- ✅ Zero code changes at call sites — old `setup_file_logging("orchestrator")` just works
- ✅ Explicit `level=` still overrides env when specified
- ✅ Environment-controlled — deploy-time config, no code edits needed
- ✅ Flexible — single global level with selective per-service overrides

## Log Sanitization Filter

### Why a `logging.Filter` instead of scrubbing at the call site?

Call-site scrubbing would require every `logger.info("api_key=%s", key)` call to remember to sanitize — easy to miss, especially in error paths. A `logging.Filter` is a single injection point guaranteed to run on every log line before it reaches the handler. Once attached in `setup_file_logging()`, every log from that service is automatically scrubbed with zero cooperation from callers.

### Why not use `logging.Filter`'s `record.exc_info` / `record.exc_text` instead of scrubbing `record.msg`?

`exc_info` is set by the logging framework automatically when `logger.exception(...)` is called — it doesn't contain arbitrary user strings. The dangerous paths are:
1. `record.msg` — the format string, which may contain interpolated secrets (`logger.info("Token: %s", token)`)
2. `record.args` — the tuple of arguments injected into `%s` placeholders

Both are scrubbed by `SanitizeFilter.filter()` before the formatter renders the final line.

### Why these specific patterns?

| Pattern | Example | Risk |
|---|---|---|
| `api_key=` | `api_key=sk-abc...` in query params or debug logs | Full API key in plaintext |
| `sk-...` / `pk-...` | `sk-proj-xxxxxxxx...` (OpenAI-style) | Credential theft |
| `Bearer` header | `Authorization: Bearer eyJ...` | Session hijacking |
| `LANGFUSE_*_KEY` | `LANGFUSE_SECRET_KEY=sk-lf-...` | Langfuse account compromise |

These cover every known secret type in the codebase. The regex approach avoids false positives on short tokens — `sk-` patterns require 20+ alphanumeric characters, and `api_key=` requires a non-empty value.

### Why is args scrubbing needed separately from msg scrubbing?

Python logging's `%` formatting happens *after* the filter runs but *before* the formatter renders. If `record.args` contains a secret tuple `("sk-abc...",)`, the filter must scrub it in `record.args` because the formatter will interpolate it into the final message. Scrubbing only `record.msg` would miss secrets passed as `%s` arguments.

### Key properties
- ✅ Zero cooperation from callers — attach once, all logs scrubbed
- ✅ Both `record.msg` and `record.args` are sanitized
- ✅ Regex patterns are specific enough to avoid false positives
- ✅ Attached to both terminal and file handlers — no leak path

## SQLiteTaskStore

### Why wrap `InMemoryTaskStore` instead of going straight to SQLite?

The A2A `TaskStore` protocol has four hot-path operations: `get`, `list`, `save`, `delete`. `get` and `list` fire on every A2A request — querying SQLite for every read would add ~1–5ms per call and create connection contention. Wrapping `InMemoryTaskStore` means reads are O(1) dict lookups, and SQLite is only touched on writes (`save` / `delete`) and on the one-time cold-start load.

### Why double-checked locking on load?

The `_ensure_loaded()` path must handle the case where N concurrent A2A requests arrive simultaneously on a cold server (e.g., after a mass restart of all 4 agents). Without locking, all N requests would attempt to re-populate the in-memory store from SQLite. The `asyncio.Lock` + double-check pattern ensures exactly one coroutine does the load while others wait, then all proceed without re-querying.

### Why migration v2→v3 inline in `init_db` instead of a separate migration system?

A dedicated migration framework (Alembic, etc.) is overkill for a project with one SQLite database and 3 schema versions. The inline approach — bump `SCHEMA_VERSION`, add a `try/except` block with `CREATE TABLE IF NOT EXISTS` — is trivially idempotent and requires zero tooling. The migration is a best-effort operation; if it fails (e.g. on a read-replica or mounted volume), existing tables are unaffected.

### Why MessageToJson / Parse instead of protobuf's native serialization?

`MessageToJson` produces human-readable JSON that can be inspected in the SQLite database with any tool (`sqlite3`, DB Browser, etc.). Protobuf binary serialization would require the `.proto` definition to decode. The overhead of JSON text storage (~200–500 bytes per task) is negligible for a task store that will rarely have more than a few dozen rows.

### Key properties
- ✅ Fast reads — in-memory dict for hot path, SQLite only on writes
- ✅ Cold-start resilience — tasks survive process restart
- ✅ Thread-safe — double-checked async lock on lazy load
- ✅ SQL-write contention serialized via existing `write_lock()` from `store.py`
- ✅ Zero-dependency migration — inline `CREATE TABLE IF NOT EXISTS`
- ✅ Human-readable payload storage (JSON, not protobuf binary)

## Memory Pruning / Retention Policy

### Why prune on startup instead of on a schedule?

Startup is the only guaranteed execution point — if the process hasn't started, no scheduler runs. A startup-triggered prune ensures the DB is cleaned before any work begins. The operation is best-effort and wrapped in `try/except` so it never blocks the server from starting even if the SQLite file is locked or corrupted.

A background schedule (e.g. `asyncio.create_task` with `asyncio.sleep(86400)`) would be more thorough but adds lifecycle complexity — what happens if the task crashes? Does it get re-created? Startup pruning is simpler and sufficient for this scale.

### Why no VACUUM?

`VACUUM` rewrites the entire SQLite file, which can take seconds on a multi-MB database and blocks all concurrent access. During startup, the orchestrator is initializing MCP connections, loading models, and accepting the first health-check requests — a multi-second lock would cause timeouts. Instead, the deleted rows leave free pages in the SQLite file that will be reused by future inserts. If disk space is a concern, `VACUUM` can be run manually during maintenance windows.

### Why prune three tables instead of just `ticker_briefs`?

| Table | Row growth rate | Why prune |
|---|---|---|
| `ticker_briefs` | ~50–100/day | Stale analysis results (past day's briefs) |
| `recommendation_records` | ~50–100/day | Stale historical recommendations |
| `memory_entries` | ~200–500/day | Full conversation history used by `load_memory` |

If only `ticker_briefs` were pruned, `recommendation_records` and `memory_entries` would grow unbounded. Pruning all three keeps the entire DB bounded. The `load_memory` tool's usefulness degrades with very old entries anyway — conversations from 6 months ago are unlikely to be relevant to today's ticker query.

### Why `created_at < cutoff` and not `updated_at`?

`created_at` never changes after insert — it's an immutable timestamp set once. `updated_at` can change (though currently no table uses it for record modification). The invariant is: "if the record was created more than N days ago, delete it." Using `created_at` avoids edge cases where a recent `updated_at` might preserve an objectively old record.

### Why env var instead of a config constant?

`MEMORY_RETENTION_DAYS` is an operational policy decision — different deployments may want different retention windows (dev: 7 days, staging: 30 days, prod: 90 days). An env var lets operators change policy without touching code. The 90-day default matches common compliance standards (quarterly cleanup).

### Key properties
- ✅ Startup-triggered — runs before any work, guaranteed execution
- ✅ Best-effort — exception-safe, never blocks server start
- ✅ No VACUUM — avoids startup lock contention
- ✅ Prunes all three memory tables — bounded DB growth
- ✅ Configurable — env var with sensible 90-day default

## MCP Client Singleton with Auto-Reconnect

### Why a process-wide singleton instead of per-request MCPClient?

Each `MCPClient.connect_all()` performs an SSE HTTP upgrade handshake, which takes ~100–500ms depending on network conditions. Over 4 agents × N requests per analysis, this adds 1–2 seconds of pure connection overhead per user query. A process-wide singleton means the handshake happens once at cold-start, and all subsequent calls use the already-established SSE stream.

Additionally, each MCP connection holds an open HTTP connection and a background asyncio task (reading SSE events). Per-request connect/disconnect creates connection churn that can exhaust file descriptors under load.

### Why double-checked locking instead of a simple module-level `await`?

Python's `asyncio.Queue` and similar primitives aside, the simplest pattern for async lazy-init with concurrent safety is the double-checked lock:

```python
if _global_client is not None and _global_client._connected:
    return _global_client
async with _client_lock:
    if _global_client is None or not _global_client._connected:
        _global_client = MCPClient(...)
        await _global_client.connect_all()
return _global_client
```

The first check (unlocked) is a fast-path O(1) return for the common case (already connected). The locked check handles the race where N callers arrive simultaneously on cold start — only one creates the client, the rest get the single result. Without the second check, N callers would create N clients.

### Why reconnect on `ConnectionError`+`EOFError` instead of all exceptions?

MCP connections can drop for various reasons: server restart, network timeout, idle timeout on the SSE stream. These produce specific transport-level exceptions (`ConnectionError`, `asyncio.IncompleteReadError`, `EOFError`). Reconnecting on every exception type (including `ValueError`, `TypeError`, etc.) would mask programming errors. The narrow exception list ensures only genuinely transient connection issues trigger a reconnect.

### Why a retry limit of 1 instead of unlimited retries?

If the MCP server is down (e.g., the finsight-mcp process crashed), retrying indefinitely would hang every agent tool call for N seconds per attempt. One reconnect attempt handles the common case (transient blip — the server is still there, the TCP connection just dropped). If both attempts fail, the error propagates immediately, and the caller (the agent) can report the failure to the LLM, which can retry at the application level.

### Why an `atexit` synchronous shutdown hook instead of async cleanup?

Python's `atexit` runs synchronous callbacks. An async `disconnect_all()` inside an `atexit` handler requires a temporary event loop because the main loop may already be closed. This is best-effort — if the event loop is gone, `atexit` won't block process exit. The alternative (relying on garbage collection of the MCPClient) is unreliable because Python's GC doesn't guarantee timely finalization of objects with open connections.

### Key properties
- ✅ Eliminates per-request SSE handshake overhead (~100–500ms saved per A2A call)
- ✅ Double-checked lock — safe for N concurrent first callers
- ✅ Auto-reconnect on transient failures (narrow exception set)
- ✅ Single retry — fails fast on persistent outages
- ✅ `atexit` hook — best-effort clean shutdown
- ✅ −131 lines of boilerplate across 4 executor files

## Lazy OpenTelemetry Instrumentation

### Why lazy instrumentation instead of module-level `*Instrumentor().instrument()`?

Module-level `*Instrumentor().instrument()` fires at import time. If a pytest test does `from agent_2_llamaindex.server import app`, it triggers `LlamaIndexInstrumentor().instrument()` which starts OTel span processors, exporter threads, and may try to connect to the OTLP endpoint — even though no tracing is needed. This breaks test isolation and causes non-deterministic failures when import order changes.

Moving instrumentation into `init_instrumentation()` with deferred imports means:
- Test code can import any module without side effects
- The OTLP exporter only connects after `init_instrumentation()` is called
- Different processes (orchestrator vs rag vs quant) get only the instrumentors they need

### Why a `_instrumented` set guard instead of a simple bool?

A single `_instrumented` bool works for one agent type. But a set supports the case where `init_instrumentation()` is called multiple times with different agent types — unlikely in production (one agent type per process) but possible in tests that import multiple server modules. The set ensures each agent type is instrumented exactly once, and the guard `if agent_type in _instrumented: return` is trivially cheap.

### Why not use OpenTelemetry's built-in `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS`?

OTel supports `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` env var to skip specific instrumentors. However, this is a blunt instrument (disables per-instrumentor, not per-import) and doesn't support the "deferred import" pattern — the instrumentor classes are still imported at module level even if `instrument()` is skipped. The `init_instrumentation()` approach keeps imports deferred, which is the actual fix for test isolation.

### Key properties
- ✅ Test imports have zero OTel side-effects
- ✅ Deferred imports — instrumentor packages loaded only when needed
- ✅ Set-based guard — idempotent across multiple calls
- ✅ Per-agent-type instrumentation — each server gets only what it needs
- ✅ Backward compatible — all traces still appear in Langfuse

## Correlation-ID Propagation via ContextVar

### Why ContextVar instead of passing `trace_id` explicitly through every function?

The trace_id needs to be available in dozens of locations: every `executor.py` method, every MCP tool handler, every log formatter. Passing it as an explicit parameter would require threading it through every function signature — hundreds of changes across the codebase. A `contextvars.ContextVar` makes it implicitly available to any code running in the same async context without touching any signatures.

This is the exact use case `contextvars` was designed for: request-scoped values that flow with the `asyncio.Task` without explicit passing.

### Why both `trace_id` and `session_id`?

| ID | Scope | Purpose |
|---|---|---|
| `trace_id` | Single user query → N sub-agent calls → M MCP calls | Correlate every log line across all services for one user query |
| `session_id` | Entire conversation session | Group log lines by the ADK session (multiple user turns) |

`trace_id` is the primary correlation key — `grep <trace_id> logs/*.log` is the intended debug workflow. `session_id` is secondary, useful for grouping multi-turn conversations.

### Why fallback in JsonFormatter (`record.trace_id` → `ContextVar`)?

The two-tier fallback (`getattr(record, "trace_id", None) or current_trace_id.get()`) supports both patterns:
1. **Explicit**: `logger.info("msg", extra={"trace_id": "abc"})` — overrides ContextVar for that single line
2. **Implicit**: Any log line after `extract_trace_ids()` or `generic_executor.execute()` — ContextVar is set automatically, formatter picks it up

This means existing code with manual `extra=` continues to work, while new code gets automatic correlation without any changes.

### Why MCP tool log lines even though MCP runs in a separate process?

Each MCP server (finsight-mcp) runs as a separate process. The ContextVar doesn't cross process boundaries — but `logger.info("Tool called", extra={"tool": "...", "ticker": "..."})` still produces a JSON line. To correlate it with the orchestrator trace_id, you grep for the ticker across all log files. The trace_id is not available in the MCP process because there's no Langfuse context there — but the ticker and timestamp are usually sufficient to match up with the orchestrator's trace.

### Why `generic_executor` sets both ContextVars instead of individual executors?

`generic_executor` is the single entry point for all A2A requests to any sub-agent. Setting ContextVars there guarantees every sub-agent (RAG, Quant, Sentiment) gets automatic correlation IDs without each implementing its own extraction logic. It's a single line in one place instead of four lines in four files.

### Key properties
- ✅ Zero parameter changes — trace_id flows implicitly via ContextVar
- ✅ Two-tier fallback — explicit `extra=` still overrides ContextVar
- ✅ `generic_executor` sets both IDs — single coverage point for all sub-agents
- ✅ `extract_trace_ids()` sets ContextVar — coverage for orchestrator path
- ✅ MCP tool log lines include structured `tool` and `ticker` fields

## Deduplicate Ticker Validation

### Why shared functions instead of inheriting from a base class?

Inheritance would require the executors to share a common base class — which they don't (ADK's `AgentExecutor`, LlamaIndex's `BaseAgent`, LangGraph's `BaseAgent`, CrewAI's `BaseAgent`). A mixin would need to be slotted into four different class hierarchies. Shared functions in `shared/ticker_utils.py` are language-level composition — no inheritance, no mixins, just `await validate_ticker(ticker)` anywhere it's needed.

The functions are pure wrappers around `validate_ticker_via_mcp()` and `resolve_ticker_via_mcp()` (which already existed in `ticker_utils.py`), adding only the MCP lifecycle management via `get_shared_mcp()`.

### Why `validate_ticker()` returns `(True, ticker, "")` on MCP failure instead of raising?

This is the "optimistic degrade" pattern. If the MCP server is temporarily down, the system should still attempt to process the query using a regex-based ticker guess rather than failing entirely. The orchestrator's input guardrail is the only strict validation point — if MCP is down there, it falls back to allowing the query through (the LLM can still produce a useful response without validated ticker data).

The return tuple `(valid, canonical_ticker, company_or_error)` gives callers flexibility:
- If `valid is False`, the ticker was definitively rejected by SEC data
- If MCP is down, `(True, ticker, "")` means "we couldn't check, proceed with the raw ticker"
- If MCP succeeds, the canonical ticker and company name are returned

### Why both `validate_ticker()` and `validate_ticker_via_mcp()`?

`validate_ticker_via_mcp(mcp, ticker)` is the low-level function that takes an already-connected MCP client — useful for callers that manage MCP lifecycle themselves or want to batch multiple calls over one connection. `validate_ticker(ticker)` is the high-level convenience wrapper that handles MCP lifecycle internally. Both exist, callers choose.

### Key properties
- ✅ ~80 lines of copy-paste removed from 4 executors
- ✅ Shared functions — fix once, run everywhere
- ✅ Optimistic degrade on MCP failure — (True, ticker, "") instead of crashing
- ✅ Low-level `_via_mcp` variants still available for custom callers

## Unified `@logged` Timing Decorator

### Why a decorator instead of explicit `time.monotonic()` calls?

Every hot path that needs latency tracking follows the same pattern: save `t0 = time.monotonic()`, run the function, compute `(t1 - t0) * 1000`, log with `extra={"latency_ms": ...}`. A decorator eliminates this boilerplate and ensures consistency — all `Exit` lines have the same format, the same structured field, and the same `fn.__qualname__` identifier. Without the decorator, each function would format its `latency_ms` field differently, or forget it entirely.

### Why not decorate the `stream()` async generators?

Python's `async def stream(...)` with `yield` is an asynchronous generator — wrapping it with a decorator that calls `await fn(*args, **kwargs)` would consume the generator immediately, yielding a single result instead of streaming. The pattern used instead is to decorate `_build_response()` (the inner async function that produces the dict result), which is a plain `async def` — safe to wrap.

### Why `fn.__qualname__` instead of `fn.__name__`?

`__qualname__` includes the class name for methods — `RAGAgent._build_response` instead of just `_build_response`. When multiple classes have methods with the same name (all 3 executors have `_build_response`), `__qualname__` disambiguates them without manual labeling.

### Why `Enter` / `Exit` / `Fail` log levels?

Three states cover the full lifecycle:
| State | When | What it contains |
|---|---|---|
| `Enter` | Before the function runs | Function name |
| `Exit` | After successful return | Function name + `latency_ms` |
| `Fail` | After an exception | Function name + `latency_ms` + exception message |

The `Fail` line is particularly useful — it captures the exception and the latency before the exception propagates, so you can see how long a failing call took before it failed. This is information that's lost in a normal traceback.

### Why not capture all exceptions including `asyncio.CancelledError`?

`CancelledError` should propagate immediately without logging — logging a cancelled task is noise, and the cancellation itself is already recorded by the asyncio event loop. The `except Exception` clause intentionally excludes `BaseException` subclasses like `CancelledError` and `KeyboardInterrupt`.

### Key properties
- ✅ Consistent Enter/Exit/Fail format across all hot paths
- ✅ Structured `latency_ms` field in JSON file logs
- ✅ `grep "Exit" logs/*.log` → full-system latency report
- ✅ `__qualname__` disambiguation for same-named methods
- ✅ `CancelledError` and `KeyboardInterrupt` pass through unlogged

## Cancellation Support + Per-Agent Timeouts

### Why store `asyncio.current_task()` instead of using `asyncio.tasks.all_tasks()`?

`all_tasks()` returns all tasks in the event loop. In a multi-agent system, there may be unrelated background tasks (MCP keepalive, Langfuse flush, etc.). Storing the specific task returned by `asyncio.current_task()` when `execute()` starts guarantees we cancel exactly the right task — no more, no less.

### Why not use `asyncio.shield()` to protect the `TASK_STATE_CANCELED` event emission?

`asyncio.shield()` protects a specific awaitable from cancellation. But the cancel flow is: `except CancelledError` → emit event → `raise`. The emit is a single `event_queue.enqueue_event()` call, which is fast and unlikely to be the point where the event loop decides to deliver the cancellation. If shielding is needed later (e.g., the emit becomes slow), it can be added as a one-line change. For now, the simple try/except/raise pattern is sufficient.

### Why `wait_for()` with a timeout map instead of `asyncio.timeout()`?

`asyncio.timeout()` (Python 3.11+) is an async context manager — clean but only available in 3.11+. The project may run on 3.10 in some deployment environments. `asyncio.wait_for()` is the cross-version compatible approach.

The timeout map uses substring matching (`"rag" in agent_lower`) so that agent names containing "rag", "quant", or "sentiment" automatically get the right timeout without exact string matching. A fallback to `A2A_TIMEOUT` (180s) ensures unrecognized agent names don't get an unbounded wait.

### Why move eval-trace writes to `finally`?

The eval-trace write captures `(task_sent, response, latency_ms)` for offline RAGAS evaluation. Before this change, the write was in the `try` body — if a `TimeoutError` jumped to the `except` block, the trace was never written. Moving it to `finally` ensures the trace is always persisted, even on timeout or cancellation. The `finally` block runs after both `try` and `except`, so the `result_text` variable (set in `except`) is available.

### Why TimeoutError returns JSON instead of raising?

When a sub-agent times out, the orchestrator should still be able to synthesize a response that says "the quant agent timed out" rather than crashing the entire request. A JSON payload `{"error": "agent_timeout", "agent": "quant", "timeout": 90}` is parseable by the LLM, which can incorporate the timeout into its final response. Raising an exception would propagate through A2A and produce a generic error.

### Key properties
- ✅ `cancel()` works on both executors — replaces `NotImplementedError`
- ✅ Per-agent timeouts — one slow agent doesn't stall the pipeline
- ✅ Clean timeout payload — `{"error": "agent_timeout", ...}` instead of crash
- ✅ Eval traces persisted even on timeout/cancellation
- ✅ Backward compatible — existing `A2A_TIMEOUT` still applies as fallback

## Persistent Memory Layer

### Problem

ADK's default `InMemoryMemoryService` loses all conversation history on server restart. The `DatabaseSessionService` persists sessions to SQLite but doesn't expose them for cross-session search. The `load_memory` tool returned empty results because:

1. `adk web` uses `InMemoryMemoryService` (in-memory, lost on restart)
2. `InMemorySessionService.get_session()` returns a `Session` with an empty `events` list — events are stored in the DB but never loaded back into the session object
3. Our initial `SQLiteMemoryService.add_session_to_memory(session)` iterated over `session.events`, which was always empty

### Solution

**Standard ADK pattern**: `run_async()` → `get_session()` → `add_session_to_memory(session)`

The `DatabaseSessionService.get_session()` loads events from the database when called after `run_async()` completes. We collect events during `run_async()` as a safety net, then use the standard pattern with fallback:

```python
session = await self._runner.session_service.get_session(...)
if session and session.events:
    await self._runner.memory_service.add_session_to_memory(session)
else:
    # Fallback: use events collected during run_async
    await self._runner.memory_service.add_events_to_memory(events=collected_events)
```

### Hybrid Search: BM25 + Embeddings

Instead of Mem0 (requires external OpenAI API), we implemented local hybrid search:

1. **BM25 keyword scoring** (`rank_bm25`) — exact term matching with TF-IDF weighting
2. **Semantic similarity** (`sentence-transformers/all-MiniLM-L6-v2`) — already in dependencies, runs locally
3. **RRF fusion** — combines both rankings, handles cases where one method fails

This gives Mem0-like search quality without external API dependencies or per-query latency.

### Schema Design

- `search_text` column added to `memory_entries` — pre-extracted plain text for fast BM25 scoring
- Auto-migration via `ALTER TABLE` — existing databases get the column on startup
- `TickerMemory`, `PortfolioStore`, `PerformanceTracker` store structured data separately from conversation events

### adk web vs main.py

`adk web` creates its own runner with `InMemoryMemoryService` (default). Our `main.py` uses `SQLiteMemoryService` with BM25 + embedding search. Both work with our `_add_events_to_memory` implementation — the standard ADK pattern is compatible with any `MemoryService`.

For production use with `adk web`, configure a custom memory service via `--memory-service-uri finsight://`.

### `load_memory` Signature Mismatch Fix (v1.14)

The `load_memory` tool returned empty results even after sessions were persisted. Root cause: ADK's `CallbackContext.add_events_to_memory()` calls with signature `(events=..., custom_metadata=None)`, but our `SQLiteMemoryService.add_events_to_memory()` required `(app_name=..., user_id=..., events=..., session_id=...)`.

**Fix**: Made `app_name` and `user_id` optional with defaults (`"finsight"` and `"default_user"`). When called via the callback, user_id, session_id, and app_name are extracted from `custom_metadata`.

**Dual persistence**: Events are persisted via two paths:
1. **`after_agent_callback`** — invoked by ADK after each agent turn (works for `adk web` UI)
2. **`_persist_to_memory`** — called directly in `agent_executor.py` after response processing (works for A2A requests)

This ensures memory works regardless of invocation path.
