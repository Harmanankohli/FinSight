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

**Why two cache paths (callback + executor-level)?** The `before_agent_callback` only fires when the orchestrator runs through ADK's built-in runner (`adk web` path). A2A requests hitting `src/orchestrator/main.py` directly bypass ADK callbacks, so the executor-level `_get_today_cached_text()` provides the same short-circuit for that path.

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

## Testing Strategy — Core Primitives & AST-Gate Sandbox

### Why test shared infrastructure before agent logic?

The four agents (orchestrator, RAG, quant, market context) depend on shared infrastructure: settings, MCP client, ticker utilities, TTL cache, rate limiter, memory store, task store, trace context, log sanitizer, timing decorator. Testing agents directly requires spinning up LLM instances, databases, and MCP servers — fragile, slow, and expensive. Testing the shared primitives catches the bugs that affect all agents, with no LLM calls required.

### Solution

**88 unit tests covering 10 shared modules** — models, quant graph nodes (stateless computation), ticker utilities, TTL cache, rate limiter, trace context, memory store, task store, log sanitizer, timing decorator. These tests run without PyTorch, ChromaDB, or any agent framework — just pytest + pydantic + httpx.

**60 AST-gate tests for the security sandbox** — an additional layer beyond the core unit tests. The sandbox (`src/shared/sandbox/`) validates that user-provided AST trees don't contain dangerous constructs (exec, eval, imports, file I/O). The 60 AST-gate tests enumerate:
- Every blocked AST node type (`Import`, `Call` to `exec`/`eval`, `Attribute` access on `__builtins__`, etc.)
- Every allowed construct (variable assignment, arithmetic, function calls to whitelisted APIs)
- Boundary cases (nested dangerous constructs, Unicode obfuscation attempts, attribute access chains)

The AST-gate tests are deterministic — no asyncio, no I/O, no network. They're the fastest tests in the suite (~2ms each) and the most security-critical.

### Why deduplicate tests across the two categories?

The AST-gate tests are separate from the 88 core tests because they test a security boundary, not a business logic path. A regression in the AST gate is a security incident — it deserves its own test category with clear failure messaging. The 88 core tests cover everything else.

### Why no integration tests for sub-agent A2A flows?

Integration tests that spin up all 4 agents + MCP server + orchestrator would take 30-60s per test and require LM Studio running with specific models. These exist as manual smoke tests (`smoke_test_a2a.py`, `test_full_pipeline.py`) but aren't part of CI. The contract tests (see v2.0 section) cover A2A protocol compliance in-process without agent processes.

### Key properties

- ✅ 88 unit tests — shared infrastructure, no LLM required, runs in CI in ~15s
- ✅ 60 AST-gate tests — deterministic, ~2ms each, security boundary tested explicitly
- ✅ Zero agent framework imports in test dependencies — slim CI install possible
- ✅ Integration tests exist as manual scripts, not CI gates
- ✅ Contract tests cover A2A protocol compliance in-process

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

**Why `max_retries=5` instead of the previous `max_retries=1`?** LM Studio periodically unloads idle models from GPU memory. When a RAGAS metric call arrives after an idle period, LM Studio reloads the model — the first request times out, succeeding on retry. With `max_retries=1`, the single retry was sometimes insufficient for model reload (~2-5s). Increasing to 5 gives the SDK enough budget to absorb reload latency at the httpx layer rather than failing instructor's structured-output calls. The `instructor.from_openai(max_retries=1)` override was removed so instructor calls inherit the client's retry count. Separate `asyncio.TimeoutError` handling prevents timeouts from appearing as bare colons in logs.

**Why `sys.stdout.reconfigure(encoding='utf-8')`?** RAGAS internal log messages (from `ragas/llms/base.py`) contain Unicode characters like curly quotes (`\u2010`, `\u2011`) when formatting LLM responses. On Windows with cp1252 console encoding, these characters trigger `UnicodeEncodeError`, producing noisy "--- Logging error ---" tracebacks. Setting UTF-8 at import time in `src/shared/bootstrap.py` (moved from `src/shared/config.py` in v1.41) prevents this for both stdout and stderr.

**Why `_push_scores` skips when trace_id is None?** With placeholder Langfuse API keys (`pk-lf-...`), `langfuse.create_score()` with no `trace_id` sends an API request missing the required trace identifier. The Langfuse cloud API rejects these with "Bad request" errors. Since eval tasks may run outside any active trace context, the early return when `trace_id is None` avoids pointless API failures.

**Why custom `_STEmbeddings` instead of RAGAS's `HuggingfaceEmbeddings`?** RAGAS 0.4.x's `HuggingfaceEmbeddings` is a Pydantic dataclass that fails to serialize correctly when passed to RAGAS internal `aembed_text` calls. The custom wrapper uses `BaseRagasEmbedding` directly with `SentenceTransformer.encode()`, bypassing the broken Pydantic path entirely.

**Why `JSON_SCHEMA` mode instead of `JSON` mode for instructor?** RAGAS defaults to `instructor.Mode.JSON` which sends `response_format.type="json_object"` in the API request. LM Studio only supports `"json_schema"` and `"text"` response format types. Patching to `JSON_SCHEMA` enables structured output without a custom LM Studio fork.

### Why gate every eval call behind a single `EVAL_TRACE_ENABLED` flag?

Sidecar RAGAS evaluation adds 5–180 seconds of background LLM work per query (per agent) on the local LM Studio judge. During fast iteration on prompts or sub-agent behaviour, this background load slows the judge model down for the next user query, and burns context on metrics that aren't being inspected. Wrapping each `asyncio.create_task(_eval_*)` site in `if EVAL_ENABLED:` lets the user kill all sidecar scoring from `.env` without removing code. `EVAL_ENABLED` reads the same `EVAL_TRACE_ENABLED` env var that already controls orchestrator trace JSON dumps — one switch, two effects, both about "extra eval load". Default stays `True` so production observability is on by default.

### Why move the orchestrator eval into `after_agent_callback`?

When `adk web` is the entry point, the orchestrator goes through ADK's built-in runner — `FinSightAgentExecutor.execute()` is never called. The eval call originally placed in `agent_executor.py` only fired when an A2A client hit `src/orchestrator/main.py` directly. Once `run_adk_web.bat` stopped starting the standalone orchestrator A2A server (it's redundant with `adk web`), evals stopped firing entirely.

`after_agent_callback` is the only ADK extension point guaranteed to fire on every agent turn regardless of runner. Wiring eval scheduling into the existing `_persist_memory_callback` keeps both side-effects in one place: callback → check turn type → persist memory → fire eval. The trace_id is read from the active Langfuse span at callback time, so traces from `adk web` are still linked.

### Why gate memory persist + eval on whether `save_brief` was called this turn?

`_persist_memory_callback` fires on every ADK turn, including pure recall turns where the user asks "what were my last recommendations?" and the agent only invokes `load_memory`. The old behaviour persisted the entire session — including the conversational recall query and the agent's response — into long-term memory. Subsequent `load_memory` calls would then surface those recall exchanges alongside actual analyses, polluting search results and drifting the agent toward conversational rather than analytical responses.

`_is_analysis_turn(events)` walks back to the most recent user message and checks for a `save_brief` tool call in the agent's response — `save_brief` is the explicit signal that a fresh recommendation was produced. If absent, persist + eval both skip. This is preferable to time-based heuristics (last N turns) or content-based heuristics (response length, keyword matching) because it relies on a deterministic signal that the orchestrator emits as a real act, not on inference about what the turn *meant*.

### Why namespace Langfuse scores by agent (`ragas/{agent}/{metric}`)?

The previous `ragas/{metric}` naming flattened all four agents into one dimension. `ragas/AnswerRelevancy` could be the orchestrator's synthesis score, the RAG agent's filing-answer score, the quant agent's metric-summary score, or the sentiment agent's narrative score — Langfuse had no way to tell them apart in dashboards or aggregations. Adding the agent prefix surfaces the source in every existing Langfuse view (score breakdowns, trends, filters) without requiring custom metadata processing. The redundant `comment="agent=<name>"` tag gives a second filter dimension if anyone wants to query by comment instead of name prefix.

### Why not introduce a separate batch-eval runner (`_invoke_agent`)?

The earlier shape of `runtime_eval.py` included an `if __name__ == "__main__":` block with `_invoke_agent()` that spun up its own `Runner` + `InMemorySessionService` to invoke the orchestrator for a fixed set of test cases. This duplicated exactly what `FinSightAgentExecutor` and `after_agent_callback` already do for live traffic — running the agent, collecting events, extracting the response. The live executor *already has the response in hand* when it fires the sidecar eval; a second runner adds nothing except a second source of truth that can drift from the first.

Removed: `_invoke_agent()`, `_run_batch_eval()`, `_BATCH_EVAL_CASES`, and the `__main__` block. Batch evaluation with ground-truth references (which is a genuinely different concern — it needs reference tool calls and reference answers) still lives in `src/tests/evaluation/run_orchestrator_eval.py`.

## Google ADK 2.x Upgrade

### Why upgrade now?

ADK 1.x is in maintenance mode. 2.x is the active development line. The 2.0 breaking-change inventory (event schema additions, `BaseAgent` extending `BaseNode`, automatic exception catching for retries) is small and easy to audit against the current codebase. Putting off the upgrade only widens the diff that needs to be reviewed later.

### Code changes required: zero

All 2.0 breaking changes were checked against the codebase before upgrading:

| 2.0 breaking change | Affects FinSight? |
|---|---|
| Event schema adds `node_info` + `output` fields | No — `SQLiteMemoryService._event_to_dict` only reads `author` and `content.parts`, doesn't validate full schema |
| `BaseAgent` extends `BaseNode`; no more `_run_async_impl` overrides | No — we use `LlmAgent` directly; `src/shared/base_agent.py` is a separate Pydantic class, not ADK's `BaseAgent` |
| No manual `enqueue_event()` on ADK events | No — `enqueue_event()` calls in `src/shared/generic_executor.py` are on A2A's `EventQueue`, not ADK |
| No broad `try/except BaseException` | No — the three matches are in `src/shared/mcp_client.py` and `src/shared/runtime_eval.py`, both outside the ADK execution path |

Smoke-tested at upgrade time: all imports from `google.adk.agents`, `google.adk.tools`, `google.adk.runners`, `google.adk.sessions`, `google.adk.events`, `google.adk.memory`, `google.adk.cli.service_registry` resolve. `SQLiteMemoryService` still satisfies the 2.x `BaseMemoryService` interface (signatures match). `orchestrator.agent.root_agent` loads and registers all four tools. `after_agent_callback` still fires.

### Why pin `>=2.0,<3.0` instead of an exact version?

We track 2.x patches and minor updates automatically (bug fixes, new features) while excluding the next major bump (which will have its own breaking-change audit). Same pattern as other major-version-pinned deps in this project.

## Why Four Different Agent Frameworks?

| Agent | Framework | Why |
|---|---|---|
| Orchestrator | **Google ADK** | A2A protocol built-in, agent card generation, session management |
| RAG | **LlamaIndex** | Best document indexing/retrieval — hybrid search, multi-index routing |
| Quant | **LangGraph** | Conditional state machine maps naturally to graph-based architecture |
| Market Context (née Sentiment) | **CrewAI** | Multi-agent role-playing (analysis + synthesis) is what CrewAI was designed for; rebranded from Sentiment in v1.31 when data lanes were split from RAG |

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

### 16. AgentCard Security Scheme — Protobuf Map API

**Problem**: The A2A SDK's protobuf `AgentCard` has a `security` field typed as `map<string, SecurityScheme>` (protobuf map). Direct assignment like `agent_card.security = {"my_scheme": ApiKeyScheme(...)}` fails because protobuf map fields don't support standard Python dict assignment — they use `get_or_create` or direct item assignment via `field[key] = value`.

**Fix**: Use `agent_card.security["my_scheme"].CopyFrom(api_scheme)` instead of dict assignment. The protobuf map type is a `MessageMapContainer` with a `CopyFrom` API, not a `dict`.

**Lesson**: Protobuf map fields look like Python dicts but aren't. Always check the protobuf generated type before assigning — `type(agent_card.security)` reveals `MessageMapContainer`, not `dict`.

### 17. LangGraph Node Names Colliding with State Keys

**Problem**: LangGraph state keys and node names share a single namespace. Naming a node `peer_comparison` collides with the state key `peer_comparison` — the graph raises `ValueError: Duplicate key 'peer_comparison'` at compile time.

**Fix**: Node names must be unique across BOTH state keys and node names. Renamed nodes to use verb-based names: `fetch_peer_comparison` (node) keeps `peer_comparison` (state key).

**Lesson**: LangGraph's namespace is flat — state keys, nodes, and conditional edges all share one namespace. Prefix node names with `fetch_` / `compute_` / `format_` to avoid collisions.

### 18. CrewAI Instrumentor Crash on 0.95

**Problem**: `crewai` version 0.95 restructured its internal module layout. The OpenInference instrumentation auto-detected the old module path and crashed on import with `ModuleNotFoundError: No module named 'crewai.instrumentation'`.

**Fix**: Upper-bound `crewai<0.95` in `pyproject.toml` until the OpenInference instrumentation is updated. The version pin is temporary — when OpenInference releases a new version supporting crewai 0.95+, the bound can be removed.

**Lesson**: Auto-instrumentation libraries (OpenInference, Langfuse) probe module internals that change between minor versions. Version-pin the instrumented library until the instrumentation tooling catches up.

### 19. LANGFUSE_BASE_URL Resolution — `AliasChoices` in Pydantic

**Problem**: `LANGFUSE_BASE_URL` was read via `os.environ.get("LANGFUSE_BASE_URL")` in multiple files. When the env var was set but the Pydantic settings model had a different priority (e.g., `.env` file overrides env var), the raw `os.environ` read got a different value than the settings model.

**Fix**: Remove all raw `os.environ` reads for Langfuse config. Use Pydantic's `AliasChoices(["LANGFUSE_BASE_URL", "langfuse_base_url"])` on a single `LangfuseSettings` model so both env var names resolve through the same priority chain. All consumers read from `get_settings().langfuse.base_url`.

**Lesson**: Raw `os.environ` reads bypass Pydantic's resolution priority (env var > .env > default). Always use the settings model — mixed `os.environ` + Pydantic creates silent divergence.

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

## Why Centralized Pydantic Settings (v1.41)

### Problem

Configuration was scattered across `src/shared/config.py` with module-level `os.environ.get()` calls and `try/except ImportError` fallbacks. No type validation — a typo in `.env` silently defaulted. `os.environ["HF_HUB_OFFLINE"] = "1"` at `src/shared/config.py` import time produced side effects just by importing the config module.

### Solution

`src/shared/settings.py` uses pydantic-settings `BaseSettings`:

```python
class Settings(BaseSettings):
    llm_base_url: str = Field("http://localhost:1234/v1", alias="LLM_BASE_URL")
    llm_api_key: str = Field("lmstudio", alias="LLM_API_KEY")
    ...
```

**Back-compat aliases**: `LLM_BASE_URL` walks through `OPENAI_BASE_URL` → `LM_STUDIO_BASE_URL` → default via `AliasChoices`.

**Singleton lazy-load**: `get_settings()` caches after first call — no import-time side effects.

**Process-level side-effects** moved to `src/shared/bootstrap.py`: sets event loop policy, `HF_HUB_OFFLINE`, stdout encoding. Called once per process entry point.

**Why not just fix `src/shared/config.py`?** The file had grown to ~184 lines of module-level code with hard-to-track import order dependencies. A fresh `pydantic-settings` class was fewer lines, self-documenting, and eliminated all import-time side-effect bugs.

## Why Split the Monolithic Modules (v1.41)

### MCP Server (2095 lines → 7 files + 1 composition root)

Monolithic `finsight_server.py` had all 18 MCP tools + agent registry + rate limiters + news fetchers in one file. Any import or change risked breaking unrelated tools. Splitting by domain (`tools/` for tools, `infra/` for infrastructure) made each module independently testable and editable. The 78-line `_app.py` composition root wires everything together — no tool module knows about the others.

### Report Generator (1638 lines → 5 files)

Same pattern: extraction pipeline, PPTX renderer, DOCX renderer, HTML renderer, and deck model each became their own file. The `src/shared/report_generator.py` shim (re-exporting public API) remained until v2.0 for backward compatibility, then was removed.

### LangGraph Nodes (1286 lines → 7 files)

`nodes.py` was the largest file in the project. Split by node domain (calculations, data_fetch, technical, dcf, monte_carlo, portfolio, summary) — each file is < 400 lines. The `__init__.py` re-exports all node functions through a clean public API.

## Why Phase R Table Classification (v1.42)

### Problem

The extraction pipeline in `src/shared/reports/extraction.py` treated all markdown tables identically — it extracted data from tables but had no understanding of *what kind* of table it was parsing. A valuation table and a peer comparison table were both processed the same way, producing schema-ambiguous output.

### Solution

`_classify_table_types()` inspects table headers and context to classify into: valuation, financial, scorecard, peer comparison, or general. Each classification feeds a type-specific extraction strategy. This means:
- Valuation tables → populate `DeckData.valuation_table` with target price, upside, P/E
- Financial tables → populate `DeckData.financials` with revenue, margins, growth
- Scorecard tables → populate `DeckData.scorecard` dimensions

## Why CI Pipeline Phase 0 Before Feature Work (v1.41)

### Problem

Before Phase 0, the project had no CI pipeline, no characterization tests, and heavy dependencies (PyTorch, CUDA) installed for every run. Developers frequently broke each other's code without knowing until runtime.

### Solution

Phase 0 established:
1. **CI pipeline** (GitHub Actions): lint (ruff), type (mypy), unit tests (pytest), frontend (next lint + tsc), Docker build matrix
2. **Characterization tests** (45 tests): golden regression for extraction, API contract tests, MCP tool shapes, quant node I/O
3. **Dependency hygiene**: removed `psycopg2-binary`, `boto3`, `streamlit`, `litellm`, `langsmith` from main deps; moved to `[future]` extras; added `aiosqlite` (was missing but imported everywhere); upper-bounded `a2a-sdk<2.0`, `langgraph<0.3`, `crewai<1.0`

**Why characterization tests before unit tests?** Characterization tests document current behaviour — they pass on the existing codebase without fixing anything. This creates a safety net for refactoring: if you change the extraction pipeline and 4 golden tests break, you know exactly what you changed.

## CI Dependency Strategy — Slim Test Dependencies & uv Caching

### Why separate test dependencies from production?

The full dependency install (`pip install -e .` without `--no-deps`) pulls 293 packages including PyTorch (2.8 GB), CUDA libraries, all four agent frameworks (ADK, LlamaIndex, LangChain/LangGraph, CrewAI), and ChromaDB + sentence-transformers. A CI job that installs all of these spends 5-10 minutes on pip install alone, most of which is downloading binaries for frameworks the tests don't use.

### Solution

**Slim test install** (`--no-deps -e .`): The CI test job installs only ~15 packages — pytest, pydantic, httpx, and the project itself. No PyTorch, no CUDA, no agent frameworks. Tests are scoped to `tests/unit/` which exercises shared infrastructure (settings, ticker utils, cache, rate limiter, memory store, task store, trace context, log sanitizer, timing decorator).

**Lazy imports** (`src/shared/memory/__init__.py`): The memory module's public API uses `__getattr__` that imports heavy dependencies only when the specific class is accessed. `from shared.memory import TickerMemory` doesn't import ChromaDB or sentence-transformers until `TickerMemory` is instantiated. This prevents import-time side-effects in modules that only reference the type.

**`pytest.importorskip` guards**: Test files that need `langgraph`, `crewai`, or `google.adk` use `pytest.importorskip("module_name")` at the top. If the framework isn't installed, those tests are skipped with a clear message instead of failing.

**uv cache**: All Python CI jobs share the uv cache via `actions/cache` with a hash of `pyproject.toml`. Since the slim test deps are stable (few changes), cache hit rate is >90%, reducing install time to ~3s.

### Why not use pytest markers (`@pytest.mark.slow`) instead?

Markers rely on developers remembering to flag tests correctly and users remembering to pass `-m "not slow"`. The slim install approach enforces the boundary at the package level — if a test file imports `langgraph`, it simply can't run in the slim job. This is a structural guarantee rather than a convention.

### Why argon2-cffi in slim deps?

`argon2-cffi` is needed by `shared.auth` which is tested in the unit tests. It's a lightweight C FFI binding (~5MB) — negligible cost for the slim install. Without it, auth-related tests would need `pytest.importorskip("argon2")`, and the auth module is a core shared primitive, not an agent-specific framework.

### Key properties

- ✅ Slim install: 15 packages vs 293 — CI install time drops from 5-10 min to ~30s
- ✅ Lazy imports prevent import-time side effects in shared modules
- ✅ `pytest.importorskip` — structural guarantee, not developer convention
- ✅ uv cache with >90% hit rate — <3s install on cache hit
- ✅ argon2-cffi included — lightweight enough for slim deps, needed by auth

## Docker Hardening Strategy

### Why non-root USER?

The default Docker image runs as `root`. If a vulnerability in the Python process allows code execution (e.g. via a malicious filing document), the attacker gets root inside the container. Running as a non-root user limits blast radius — the attacker can only write to world-writable directories like `/tmp`. All 5 Dockerfiles (`orchestrator`, `quant`, `rag`, `market-context`, `mcp`) use `USER appuser` with `uid=10001` (non-standard to avoid conflicts with host UIDs).

### Why per-service pip extras?

The full install of all 4 agent frameworks in every container wastes disk space and attack surface. The orchestrator container doesn't need LlamaIndex or LangGraph, and the RAG container doesn't need ADK or CrewAI. Using pip extras per service:

```
pip install "finsight-mcp[orchestrator]"
pip install "finsight-mcp[rag]"
```

Each container only installs the frameworks its agent needs. The `[all]` extra is available for development. On disk: orchestrator container shrinks from 12 GB (all frameworks) to 3.2 GB.

### Why python urllib healthchecks instead of curl?

Including `curl` in each container adds ~15 MB per image (5 containers = 75 MB total) and is a security concern (curl is frequently targeted by CVEs). Python's built-in `urllib.request` can perform health checks with no additional dependencies:

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/health')"
```

This is 5 lines of inline Python — no curl binary, no package install, no attack surface.

### Why `restart: unless-stopped`?

All 5 services in `docker-compose.yml` use `restart: unless-stopped`. When LM Studio or the MCP server crashes (e.g. OOM, Windows update, driver crash), Docker restarts the container automatically without manual intervention. `unless-stopped` instead of `always` means a developer can explicitly `docker stop` a container for debugging without Docker restarting it.

### Key properties

- ✅ Non-root user in all 5 Dockerfiles — limits vulnerability blast radius
- ✅ Per-service pip extras — each container installs only needed frameworks (3.2 GB vs 12 GB)
- ✅ Python healthchecks — no curl binary needed, no CVE surface
- ✅ `restart: unless-stopped` — auto-recover from crashes, explicit stop still respected
- ✅ `.dockerignore` — excludes `node_modules/`, `.venv/`, `__pycache__/`, `tests/` from build context

The rate-limited lockout uses IP per-username to prevent brute-force attacks. But in Docker Compose, the Next.js proxy forwards all requests — `request.client.host` on the orchestrator sees the proxy's IP, not the browser's. Every login attempt appears to come from the proxy's IP, making per-IP lockout useless because all users share the proxy's IP.

**Why not trust X-Forwarded-For unconditionally?** An attacker can spoof `X-Forwarded-For` to hit a different username's lockout counter or bypass their own lockout. The `TRUSTED_PROXIES` setting restricts `X-Forwarded-For` trust to specific proxy IPs — only when the direct peer matches a trusted proxy is the forwarded header used.

**Why default empty?** An empty `TRUSTED_PROXIES` always uses the socket address, which is correct for direct connections (non-Docker, non-proxy setups). The user must explicitly configure proxy IPs when deploying behind a reverse proxy.

## Why Disable proxy.ts Middleware (v2.1)

The `proxy.ts` middleware in `src/web/nextjs-app/proxy.ts` redirected unauthorized requests to the CopilotKit Cloud login page. Even with `AUTH_ENABLED=false`, the middleware intercepted requests and forced a login redirect — breaking the local development flow where users expect to bypass authentication entirely.

**Fix**: Renamed to `proxy.ts.disabled`. The middleware is git-history preserved but not loaded by Next.js. When auth is re-enabled for production, rename back and verify the redirect condition checks `AUTH_ENABLED` before redirecting.

## Why Full Auth Implementation (v1.43)

### Problem

The original `X-FinSight-User-Id` header convention provided no security — any client could impersonate any user. As the system gained Docker deployment and sub-agent communication, the need for authenticated inter-service communication became critical.

### Solution

A 3-boundary auth model (A: User→Frontend→Orchestrator, B: Orchestrator↔Sub-Agent, C: Agent↔MCP) with bearer JWT tokens for users and static bearer tokens for services. Default off (`AUTH_ENABLED=false`), opt-in via `.env`.

**Why JWT instead of session cookies?** The orchestrator serves both a Next.js frontend and A2A clients. JWT access tokens are transport-agnostic — they work over REST, A2A JSON-RPC, and SSE connections. Session cookies only work for browser→server communication.

**Why Argon2id instead of bcrypt?** Argon2id is the current OWASP-recommended password hashing algorithm. It's memory-hard (resistant to GPU attacks) and time-hard (resistant to ASIC attacks). The `argon2-cffi` library is pure Python with a C FFI binding — no system dependency.

**Why rate-limited lockout instead of exponential backoff?** Lockout provides a clear signal to legitimate users ("try again in 60s") while making brute-force impractical. The cooldown is short enough that users don't abandon the application but long enough to prevent meaningful password guessing.

## Contract Test Strategy & OpenAPI Specification (v2.0)

### Why contract tests before feature work?

After implementing auth (v1.43), the system had multiple interacting boundaries: 4 agent types × 2 auth modes (on/off) × REST + A2A + SSE transports. Manual testing couldn't cover this matrix — bugs in the auth middleware, A2A protocol handling, or route ordering would slip through until a specific combination.

### Solution

**Parametrized auth × route matrix** (37 tests): Each of 13 API routes tested with valid auth, expired token, missing token, and invalid token. The parametrized fixture generates all combinations from route definitions and auth states — adding a new route automatically generates its auth tests.

**In-process A2A protocol test**: Rather than spinning up 4 agent processes and testing over the network, the test runs a sub-agent executor in-process, sends A2A requests via the SDK's JSON-RPC client, and verifies the full streaming lifecycle:
- `WORKING → artifact_update (data) → COMPLETED` (normal path)
- `WORKING → FAILED` (error path)
- `SendMessage → Cancel` (cancellation path)
- `input_required` in-band queries

This catches protocol-level regressions without the flakiness of inter-process tests.

### Why check in openapi.json with CI enforcement?

The OpenAPI spec is generated from a FastAPI spec-generator app that reuses the same Pydantic response models the real API uses. The checked-in `openapi.json` is the source of truth — CI runs `openapi diff` against it on every PR. If the spec changes, the PR must include the updated `openapi.json`. This prevents silent API drift: a new route added without updating the spec fails CI immediately. 15 Pydantic models, 13 paths, 16 schemas.

### Why a trace filter for auth observability?

Langfuse traces include `user_id` from the JWT token. But before auth (v1.43), traces used `user_id: "anonymous"`. The `trace_with_user()` helper wraps each traced operation with the authenticated user ID, falling back to the anonymous cookie ID when auth is disabled. The `traceFilter.ts` in the OpenAPI spec documents which fields appear in traces for each auth mode — this is documentation, not enforcement, but it prevents confusing "anonymous" labels in prod traces.

### Key properties

- ✅ 37 parametrized auth tests — full auth matrix coverage
- ✅ In-process A2A tests — protocol-level regression without inter-process flakiness
- ✅ CI-enforced OpenAPI spec — `openapi diff` blocks silent API drift
- ✅ 15 Pydantic models, 13 paths, 16 schemas — spec is always current
- ✅ Trace filter for auth — observability works correctly in both auth modes

## Model Change: gpt-oss-20b → qwen

The LLM used by all agents was switched from **`gpt-oss-20b`** to a **qwen** model:

| Model | Speed | Notes |
|---|---|---|
| `gpt-oss-20b` (previous) | ~40-60s per call | Large, slower inference |
| `qwen3-30b-a3b-2507` (current) | ~5-10s per call | Much faster, sufficient quality |

**Key**: The qwen model reduced per-call latency by ~5-10x while maintaining adequate output quality for all agent tasks (routing, summarisation, analysis). This was the single biggest performance improvement in the pipeline.

## Model Change: qwen3-30b-A3B → Ministral-3-14b (v1.36)

### Why switch from qwen3-30b-A3B to ministral-3-14b-reasoning?

The default in `config.py` remained `qwen/qwen3-30b-a3b-2507`, but the active `.env` was changed to `mistralai/ministral-3-14b-reasoning` for local development:

| Aspect | qwen3-30b-A3B | ministral-3-14b-reasoning |
|---|---|---|
| Params | 30B total (3B active) | 14B total |
| Inference (M1 MBP 16GB) | ~8-12s per call | ~3-5s per call |
| Tool-calling reliability | Reliable (calls `save_brief` consistently) | Unreliable (often skips `save_brief`) |
| RAM usage | ~6-8GB | ~3-4GB |

Ministral is faster and lighter, which matters for the A3B model's runtime on a laptop with 16GB unified memory. The qwen model, despite having only 3B active parameters, still loads the full 30B into memory — consuming ~6-8GB vs ministral's ~3-4GB.

### Why not change the `config.py` default?

The `.env` is per-developer. Changing the default would force the qwen download on every new clone. Keeping `qwen3-30b-a3b-2507` as the code default preserves it as the "reference" model — it has reliable tool-calling and was tested most thoroughly. Developers can override to ministral (or any LM Studio model) via `.env` for faster local iteration.

### What problems did the switch expose?

Ministral's inconsistent `save_brief` calling directly motivated two v1.36 features:

1. **Auto-save brief fallback** — when the LLM delegates to sub-agents (`send_message` called) but doesn't call `save_brief`, the callback programmatically persists the analysis using regex-extracted rec/confidence
2. **Full synthesis after-turn update** — when `save_brief` is called early with a placeholder, the post-turn callback overwrites with the longer analysis text from session events

See the relevant sections above for detailed rationale.

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

`extract_ticker()` in `src/shared/ticker_utils.py` fetched the full SEC company_tickers.json (~4MB) on every call to validate candidates. This meant:
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

**Solution**: Extracted `validate_ticker_via_mcp(mcp, ticker)` and `resolve_ticker_via_mcp(mcp, query, exclude_ticker)` into `src/shared/ticker_utils.py`. Each agent's `_validate_ticker` / `_resolve_ticker` methods are now ~7-line wrappers that handle their own connection logic, then delegate the actual MCP call to the shared functions.

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

**Solution**: Added `parse_mcp_result(result)` utility in `src/shared/mcp_client.py` that handles various MCP response formats consistently — returns parsed dict/list/string or `{"error": "..."}` on failure.

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

**Step 1 — Financial stop-word blocklist**: Added `_FINANCIAL_STOP_WORDS` in `src/shared/ticker_utils.py` — a curated set of 30+ financial acronyms that are never valid stock tickers. Applied to both pattern 4 (3-5 letter) and pattern 5 (2 letter) regex results.

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

**Step 1 — `extract_holdings()` in `src/shared/ticker_utils.py`**: Four regex patterns covering natural language phrasing:

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

`src/shared/logging_config.py` provides a single `setup_file_logging(service_name)` function that:
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

`src/shared/bootstrap.py` sets `os.environ["HF_HUB_OFFLINE"] = "1"` before any HuggingFace code is imported or executed (moved from `src/shared/config.py` in v1.41). This tells the `huggingface_hub` library to skip all network calls — it assumes models are already cached locally from a prior online run.

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

The existing `IST` convention in `src/shared/settings.py` is for *business logic* timestamps (analysis_date, created_at) where users think in local time. Log timestamps are for *operations* — UTC is the standard.

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

### Why remove the fail-fast first-attempt timeout?

**Original design**: The MCP client's `call_tool_by_name` used `self.timeout / 3` on the first retry attempt to fail fast (5s for a 15s timeout), then fell back to the full timeout on subsequent attempts. The rationale was: if the first attempt fails quickly, the total latency across all retries is bounded.

**Problem**: yfinance HTTP requests have natural latency variance — a call that takes 4 seconds one time might take 6 seconds the next (rate limiter queue, Yahoo backend load, network jitter). Under the /3 logic, a first attempt that took 5.5s would timeout (5s threshold), triggering a retry. The retry using the full 15s would then succeed — but the total latency was 5.5s + 6s = 11.5s instead of just 6s. The fail-fast pattern *added* latency by creating false-positive timeouts on naturally variable endpoints.

Additionally, the retry backoff logic (`2^attempt` exponential) was already fast — the first retry waits <1s. If the first attempt genuinely fails (not a timeout), the retry happens almost immediately. The reduced timeout only affected the "first attempt took longer than expected but would have succeeded" case, which is the common case for yfinance variability, not the failure case.

**Fix**: All retry attempts now use the full configured timeout. The exponential backoff still provides fast retries for genuine failures (connection errors, 503s). The timeout only fires when a request truly hangs beyond the configured limit, not when it's merely slower than average.

### Key properties

- ✅ No false-positive timeouts from normal latency variance
- ✅ Total latency per retry chain is `timeout × max_retries` worst-case (was `timeout/3 + timeout × (max_retries-1)`)
- ✅ Exponential backoff still provides fast retries on genuine failures
- ✅ Simplifies mental model — one timeout value, not context-dependent thresholds

## Lazy OpenTelemetry Instrumentation

### Why lazy instrumentation instead of module-level `*Instrumentor().instrument()`?

Module-level `*Instrumentor().instrument()` fires at import time. If a pytest test does `from financial_rag.server import app`, it triggers `LlamaIndexInstrumentor().instrument()` which starts OTel span processors, exporter threads, and may try to connect to the OTLP endpoint — even though no tracing is needed. This breaks test isolation and causes non-deterministic failures when import order changes.

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

Inheritance would require the executors to share a common base class — which they don't (ADK's `AgentExecutor`, LlamaIndex's `BaseAgent`, LangGraph's `BaseAgent`, CrewAI's `BaseAgent`). A mixin would need to be slotted into four different class hierarchies. Shared functions in `src/shared/ticker_utils.py` are language-level composition — no inheritance, no mixins, just `await validate_ticker(ticker)` anywhere it's needed.

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

## Data-Driven DCF Assumptions

### Why replace the hardcoded 8% WACC with data-driven computation?

The original DCF model assumed a fixed 8% WACC for all tickers. For a high-beta tech stock (AAPL, beta=1.2), CAPM gives cost of equity ≈ 4.3% + 1.2 × 5.5% = 10.9% — 36% higher than the fixed assumption, overvaluing the stock. For a low-beta utility (DUK, beta=0.5), CAPM gives 4.3% + 0.5 × 5.5% = 7.05% — 12% lower, undervaluing it. A single WACC for all tickers systematically biases DCF outputs.

### How is WACC now computed?

Three-step process in `dcf_valuation_node()` (`src/quant/nodes.py:737`):

1. **Cost of equity (CAPM)**: `risk_free (4.3%) + beta × equity_premium (5.5%)`. Beta is taken from the computed metrics (yfinance 60-month), fallback to 1.0 if unavailable.

2. **After-tax cost of debt**: `interest_expense / total_debt × (1 - tax_rate)`, with fallback to 4%. Total debt is `longTermDebt + shortTermDebt` from latest financials.

3. **WACC**: Weighted average by market cap and total debt, clamped to [6%, 18%]. `wacc = (E/(E+D)) × coe + (D/(E+D)) × cod_at`. The clamp prevents degenerate values from extreme capital structures (e.g., a company with near-zero debt doesn't get a 2% WACC).

### How is the growth rate determined?

Previously fixed at a single value. Now computed as:
- **Base growth**: Blend of `revenueGrowth` (60%) and `earningsGrowth` (40%) from yfinance fundamentals
- **Bounds**: Clamped to [2%, 25%] — a 2% floor reflects long-term GDP trend, 25% ceiling prevents unrealistic perpetual growth
- **Fallback**: 8% if neither growth metric is available (same as the old hardcoded value)
- **Terminal growth**: `min(3%, wacc - 2%)` — prevents terminal value from exceeding WACC (which would make terminal value infinite)

### Why a tapered projection instead of a single-stage model?

A 2-stage DCF (high growth → terminal) produces abrupt jumps in the projected FCF. The 5-year tapered model linearly fades the initial growth rate toward the terminal rate over years 3–5, producing smoother valuations. The terminal value uses the Gordon Growth Model discounted to present value.

### Key properties

- ✅ Per-ticker WACC — beta and capital structure influence discount rate
- ✅ Data-driven growth — revenue/earnings growth sets projections, not a constant
- ✅ Bounded output — WACC clamped [6%, 18%], growth clamped [2%, 25%]
- ✅ Smooth projection — tapered fade avoids GGM discontinuity
- ✅ De facto backward compatible — fallback chain defaults to 8%/3% when data missing

## Parallel Fan-Out Graph Topology

### Why parallel fan-out instead of a sequential chain?

The original Quant graph was largely sequential: `fetch_prices → compute_metrics → DCF/stress → format_output`. Price fetching and fundamentals fetching were sequential, adding ~3-5s per call. The revised graph fans out from START into 5 parallel branches:

```
START ──→ fetch_prices ──→ compute_base_metrics ──[volatility gate]──→ DCF/stress
                       └──→ technical_analysis
       └──→ fetch_fundamentals ──→ peer_comparison
                               └──→ analyst_positioning
       └──→ options_flow
       └──→ insider_signals
```

All paths converge at `format_output` → `llm_summary`. Since the branches are independent (prices don't depend on fundamentals, technicals don't depend on DCF), there's no reason to serialize them. `LangGraph` supports multiple outgoing edges from a node naturally — no special parallel construct needed.

### Why not merge `fetch_prices` and `fetch_fundamentals` into one node?

They call different MCP tools with different cache profiles and error characteristics. `fetch_prices` uses `get_prices` (TTL-cached at 1 min) and can fail independently of `fetch_fundamentals` (TTL-cached at 1 hr). A single node would couple their lifetimes — if fundamentals data fails, prices are also lost, even though price-only analysis (volatility, Sharpe, technicals) is still useful.

### Why a volatility gate instead of always computing both DCF and stress test?

DCF is meaningless for extremely volatile stocks — the discount rate uncertainty swamps the valuation. The 35% annual-vol threshold gates to stress test only, saving the ~1-2s DCF computation and avoiding a meaningless output. The `dcf_error` field is set in `compute_metrics_node` *before* the routing decision, so callers see "DCF skipped: volatility exceeded 35%" regardless of which path is taken.

### Why add options_flow and insider_signals as separate branches?

These were previously not computed at all (no data source). Adding them as parallel branches from START means they run concurrently with prices/fundamentals with zero added latency (assuming no MCP bottleneck). Their data is incorporated at `format_output` through a weighted signal aggregation alongside the risk/DCF/fundamentals/technicals scores.

### Key properties

- ✅ 5 parallel branches — independent work streams don't block each other
- ✅ Volatility-gated DCF — saves compute when valuation would be meaningless
- ✅ No node coupling — price and fundamentals failures are isolated
- ✅ Zero added latency — new branches (options, insider) run concurrently
- ✅ All paths converge at format_output — single enrichment point

## Quant Graph Fan-In Resolution: Diamond Dependency, Reducers & Passthrough Keys

### Problem

The parallel fan-out graph had a subtle topology bug. `fetch_fundamentals` fed into three downstream nodes: `peer_comparison`, `analyst_positioning`, and a direct edge to `format_output`. This created a **diamond dependency** — `format_output` had two indirect predecessors via `peer_comparison`/`analyst_positioning` AND a direct edge from `fetch_fundamentals`. LangGraph scheduled `format_output` twice in the same checkpoint step when the two intermediate nodes completed concurrently. Each duplicate run wrote every state key, triggering `INVALID_CONCURRENT_GRAPH_UPDATE` at whichever key was updated first — typically `positioning` or `metrics`.

### Solution

Three-part fix:

**1. Remove diamond dependency** (`src/quant/graph.py:39`): Deleted `builder.add_edge("fetch_fundamentals", "format_output")`. Fundamentals data flows through `peer_comparison` and `analyst_positioning` into shared state — `format_output` reads it from state, not from a direct edge. `format_output` now has exactly 5 clean predecessors and runs exactly once per invocation.

**2. Add Annotated reducers for concurrent writes** (`src/quant/state.py:6-22`): Three state keys are written by multiple nodes in the same step, so they needed reducers even without the diamond:
- `metrics` → `_merge_dict` (merges dicts from `compute_metrics` and `format_output`, second wins on key collision)
- `stress_test_result` → `_last_nonnull` (last non-null value wins — `stress_test_node` writes real data, `format_output`'s initial write is `None`)
- `dcf_error` → `_last_nonnull`
- `reasoning` → `_last_str` (last non-empty string wins, then `llm_summary_node` overwrites)
- `recommendation` → `_last_str` (idempotent — both runs produce same BUY/HOLD/SELL)
- `monte_carlo` → `_last_nonnull`

**3. Remove passthrough keys from format_output** (`src/quant/nodes.py`): `format_output_node` was returning copies of state keys (`positioning`, `dcf_valuation`, `correlation_matrix`, `fundamentals`, `technicals`, `peer_comparison`, `options_signals`, `insider_signals`) that other nodes already wrote. Even with reducers, writing every key from `format_output` is wasteful and confusing — the node should only emit what it computes: `recommendation`, `reasoning`, `metrics` (with signals/confidence), and `stress_test_result`. Full state from `ainvoke()` still carries every key via the owning nodes.

### Why not use LangGraph's built-in fan-in handling?

LangGraph handles fan-in naturally — multiple predecessors can converge on a single node. The problem wasn't fan-in by itself but the *diamond*: `format_output` being reachable via two different path lengths from `fetch_fundamentals`. LangGraph triggers the node twice when its predecessors complete in different checkpoint steps (or in the same step via separate schedule entries). The reducers would have caught the symptom (`INVALID_CONCURRENT_GRAPH_UPDATE`) but the diamond removal fixes the root cause.

### Why keep the reducers after fixing the diamond?

The reducers are still needed for the remaining concurrent writes — `metrics` is written by `compute_metrics` AND `format_output` in the same step, `stress_test_result` by `stress_test` AND `format_output`, and `dcf_error` by `compute_metrics` AND `dcf_valuation`. These are not diamond-induced duplicates but legitimate multi-writer keys. The reducers act as a safety net for any future topology changes that might introduce similar conflicts.

### Key properties

- ✅ Root cause fixed — diamond edge removed, `format_output` runs once
- ✅ Reducers handle legitimate multi-writer keys
- ✅ Passthrough keys removed — `format_output` only emits what it computes
- ✅ Full state still accessible — owning nodes write their keys before fan-in
- ✅ Safety net — reducers prevent regression from future topology changes

## Parallel News Ingestion with SEC Filings

### Why fire news and SEC ingestion as concurrent background tasks?

The original RAG agent fetched SEC filings first (`_ensure_ingested`), then queried. News was fetched on-demand via a separate MCP tool call inside the query path. This meant:
1. News ingestion added 1-3s latency to every query that mentioned "news" or "sentiment"
2. News and SEC ingestion were sequential (~3s + ~8s = ~11s total cold-start)
3. No persistent news index — news was re-fetched from MCP each time (no dedup cache)

### Solution

`query()` in `src/financial_rag/executor.py:162` now fires both ingestions as `asyncio.create_task()` without awaiting:
```python
asyncio.create_task(self._ensure_ingested(ticker))
asyncio.create_task(self._ensure_news_ingested(ticker))
return await self.index.query(ticker, query_text)
```

The ChromaDB query runs immediately against whatever is already indexed. If a filing or news article was ingested in a prior query, it's found. If the ticker has never been queried before, the ingestion tasks complete in the background and the current query returns results from the first ingestion that finishes (or falls back gracefully).

### Why a separate dedup key for news?

SEC filings use `edgar_url` as the dedup key — immutable and canonical for SEC documents. News articles don't have an EDGAR URL. Using `news_{ticker}` as the daily dedup key in `_last_ingestion` ensures news is re-fetched at most once per day per ticker, regardless of how many times the ticker is queried. The same daily-dedup pattern as `_ensure_ingested` but with a different key namespace.

### Why separate ChromaDB collections for news vs filings?

Different retrieval characteristics: news queries want recency (temporal decay matters), filing queries want relevance (semantic similarity matters). A single collection would mix the two, making it impossible to apply different reranking strategies. The `_classify_query_intent` function selects which collections to search based on query keywords, and the `_hybrid_score` blends vector (0.6), keyword (0.2), and temporal decay (0.2) differently per collection.

### Key properties

- ✅ Zero added latency — ingestion is fire-and-forget, query uses existing index
- ✅ Parallel cold-start — news and filings ingest concurrently (~8s total vs ~11s)
- ✅ Daily dedup per ticker — `news_{ticker}` key prevents redundant fetches
- ✅ Separate collections — per-type retrieval strategies
- ✅ Graceful degradation — query returns best available data

## Multi-Collection Query Routing

### Why keyword-based intent classification instead of RouterQueryEngine?

LlamaIndex's `RouterQueryEngine` uses an LLM call to decide which tool/index to route to. For every RAG query, this adds ~2-5s of LLM inference time and ~500-2000 tokens, with the risk of the LLM choosing the wrong collection (e.g., routing an earnings question to general SEC filings). A keyword-based `_classify_query_intent()` in `index_manager.py:68` completes in microseconds with deterministic results.

The classifier checks the query text for keyword groups:
- `news`, `sentiment`, `headline`, `breaking`, etc. → include `"news"` collection
- `earnings`, `revenue`, `guidance`, `eps`, `beat`, `miss` → include `"earnings"` collection
- `analyze`, `overview`, `outlook`, `recommend`, `should I` → search ALL collections
- Default (pure financial analysis queries): search `"sec_filings"` only

### Why score-sorted dedup instead of top-k per collection?

If each collection returns top-5 nodes and they overlap, the final set may have duplicates. The merge-dedup pipeline: collect all nodes from all selected collections → hybrid re-score each node → sort by score descending → deduplicate by content hash (first 200 chars) → truncate to top-5. This guarantees:
1. The best-matching nodes win regardless of source collection
2. No duplicate content reaches the LLM (which would waste context window)
3. A node from a low-priority collection can outrank a weak match from the primary one

### Why a 3-factor hybrid score?

A pure vector score favors semantically similar but potentially old content. Adding keyword overlap (exact term matching) ensures query terms like "Q3 2025 revenue" surface documents with those exact phrases. Adding temporal decay (exponential, λ=0.004, year-old ≈ 0.23) favors recent news/earnings without completely excluding old filings. The weighted blend (0.6 vector + 0.2 keyword + 0.2 temporal) is tuned to balance semantic relevance with recency for financial data, where both matter.

### Why not use LlamaIndex's built-in retriever fusion?

`RetrieverQueryEngine` fusion expects all retrievers to use the same index type and similarity metric. Our collections are independent ChromaDB instances with different embedding profiles (different document types require different chunking strategies). The custom pipeline gives full control over per-collection retrieval parameters (top-k per collection, hybrid scoring weights, dedup strategy) without fighting the framework's assumptions.

### Key properties

- ✅ Deterministic routing — no LLM cost, no latency, no hallucinated choices
- ✅ Microsecond classification — ~500x faster than RouterQueryEngine
- ✅ Score-sorted dedup — best content wins, duplicates eliminated
- ✅ 3-factor hybrid score — semantic + keyword + temporal = financial-grade ranking
- ✅ Framework-agnostic — works with any vector store, not just LlamaIndex

## Orchestrator Parallel Dispatch via System Prompt

### Why change the system prompt instead of adding code-level dispatch logic?

Two options existed for parallel agent dispatch:
- **Option A (system prompt)**: Instruct the LLM in `_STATIC_PREAMBLE` to emit all `send_message` calls in a single assistant turn. Qwen3-30B-A3B natively supports multiple tool calls in one response — the instruction simply unlocks existing capability.
- **Option B (deterministic extraction)**: A Python-side shim that extracts tickers from user input and fires all sub-agent A2A calls simultaneously, bypassing the LLM for routing. ~5h implementation, adds ticker-extraction false-negative risk.

Option A was chosen (implemented in `src/orchestrator/agent.py:184-189`). It's zero new code, zero added latency, and has a documented escape hatch (Option B) if a future model swap breaks parallel tool calling. The `_STATIC_PREAMBLE` is KV prefix cached — adding the instruction has no inference cost.

### Why add agent responsibility boundaries separately?

Without boundaries, the LLM sometimes routes news-related queries to the Sentiment agent ("get news sentiment" sounds like the Sentiment agent's job) even though RAG now owns financial news retrieval. The boundaries block in `_build_instruction()` (`agent.py:253-260`) disambiguates: "Financial RAG Agent owns ALL document and news retrieval." This is a narrow fix — it doesn't refactor any agent — just clarifies the prompt to reduce misrouting.

### Why update step 5 to mention receiving results "together"?

The old step 5 said "After all agents respond" without specifying whether results arrive individually or together. The new wording ("you will receive their results together in the next turn") aligns the LLM's expectation with the actual A2A behavior — all sub-agent responses arrive in the subsequent turn because the LLM doesn't await between `send_message` calls. This prevents the model from trying to process partial results prematurely.

### Key properties

- ✅ Zero new code — pure system prompt instruction, no Python dispatch shim
- ✅ KV-cached — static preamble keeps prefix cache warm
- ✅ Documented escape hatch — Option B exists on paper if needed
- ✅ Narrow boundary fix — prevents LLM misrouting news to Sentiment agent
- ✅ Explicit parallel contract — step 5 confirms batch reception

## Parallel Filing Downloads + Server-Side Truncation

### Why switch from sequential to parallel filing fetches?

The original `_ensure_ingested()` in `src/financial_rag/executor.py` fetched filing content in a sequential loop: for each filing, call `get_filing_content`, parse, append. For a ticker with 5 new filings, this summed the per-filing latencies (~3-5s each = ~15-25s total). The revised two-phase pattern (`executor.py:74-107`):
1. **Filter phase**: Iterate filings to find un-ingested candidates (fast, no network)
2. **Fetch phase**: `asyncio.gather(*[_fetch_one(f) for f in candidates])` — all filing downloads run concurrently

Filing downloads for a 5-filing ingest now complete in ~max(per-filing latency) (~3-5s). The `_fetch_one` helper isolates per-filing errors — one failed filing doesn't block the others.

### Why truncate server-side at 25k instead of client-side at 20k?

The old code truncated `content[:20000]` in the RAG agent after receiving the full response from MCP. A 10-K filing response (~80-150k chars) was fully transmitted over the loopback before being truncated. Moving the truncation to `get_filing_content` in `src/mcp_tools/finsight_server.py:842` (`result["content"] = result.get("content", "")[:25000]`) cuts the bandwidth to <25k per filing — the RAG agent never receives the full text. The limit was also raised from 20k to 25k (MCP server overhead is negligible, more content is better for retrieval).

### Why not parallelize filing fetches in Phase 1?

Phase 1 added the RAG news ingestion and multi-collection architecture. Parallelizing filing downloads was deferred to Phase 2 because the sequential loop was already functional — the latency impact was hidden behind the total cold-start (~11s). With Phase 2's focus on latency reduction, the parallel fetch was the highest-leverage change in the RAG pipeline.

### Key properties

- ✅ O(max) instead of O(sum) — 5-filing ingest drops from ~20s to ~4s
- ✅ Isolated errors — per-filing failure doesn't block other filings
- ✅ Server-side truncation — bandwidth cap, not client-side afterthought
- ✅ Raised limit — 25k vs 20k, more content for retrieval
- ✅ Backward compatible — ingestion pipeline downstream unchanged

## Single-Flight News Cache

### Why refactor `get_news_sentiment` into impl + wrapper?

The original `get_news_sentiment` used manual `_cache_news.get(key)` / `_cache_news.set(key, result)` pattern. When two concurrent callers (e.g., RAG agent + dashboard) requested the same uncached ticker simultaneously, both would miss the cache, both would fetch RSS feeds, and both would set the cache — wasting one RSS round-trip (~1-2s). The `TTLCache.get_or_fetch()` method (`src/shared/ttl_cache.py:25`) was already designed for this: it uses an internal `asyncio.Event` per key so the second caller awaits the first's result instead of duplicating work.

The refactor (`src/mcp_tools/finsight_server.py:1406-1551`):
1. Extracted all fetch logic into `_get_news_sentiment_impl(ticker, limit)` — pure data fetching, no caching concern
2. The outer `get_news_sentiment` tool now just calls `_cache_news.get_or_fetch(cache_key, lambda: _get_news_sentiment_impl(...))`

### Why was this deferred from Phase 1?

Phase 1's news ingestion was single-caller (only the RAG agent's background task calls `get_news_sentiment`). The single-flight pattern matters when multiple concurrent callers exist — which Phase 2 doesn't introduce directly, but the fix is zero-risk (pure refactor) and prevents future regressions when additional consumers (dashboard, sentiment agent's parallel data collection) call the same tool.

### Key properties

- ✅ Single-flight — concurrent callers share one RSS fetch
- ✅ Zero behavioral change — cache key, TTL, and response format identical
- ✅ Pure refactor — impl extracted, wrapper delegates to get_or_fetch
- ✅ Already-supported pattern — TTLCache.get_or_fetch existed, just unused by this tool

## Fire-and-Forget RAG Ingestion

### Why convert `_ensure_ingested` from await to fire-and-forget?

Phase 1's `query()` used `await asyncio.gather(self._ensure_ingested(ticker), self._ensure_news_ingested(ticker))` — the query blocked until both ingestion tasks completed. This added ~8s (filings) + ~3s (news) = ~11s of cold-start latency to every new ticker's first query. The query result could only use data indexed from a previous run.

Phase 2 converts both to `asyncio.create_task()` (`executor.py:162-166`):
```python
asyncio.create_task(self._ensure_ingested(ticker))
asyncio.create_task(self._ensure_news_ingested(ticker))
return await self.index.query(ticker, query_text)
```

The ChromaDB query runs immediately against whatever is already indexed. If the ticker was queried before, all data is available. If it's a new ticker, the ingestion finishes in the background and the current query returns a warming signal.

### Why a warming signal instead of silently returning empty results?

Silent empty results are confusing — the user sees "no data found" and doesn't know whether the ticker genuinely has no filings or the index is still warming. The `_warming` flag in `index_manager.py` (`{"_warming": True, "summary": "Index is warming for {ticker}..."}`) makes the state explicit. The orchestrator LLM can use this signal to tell the user "analysis in progress, results will improve."

### Why keep the per-ticker `asyncio.Event` tracking optional?

The plan mentioned optional `self._ingest_events: dict[str, asyncio.Event]` so a same-process second query could briefly await the first's ingestion. This wasn't implemented because:
1. The warming signal already communicates the incomplete state
2. An Event-based wait adds complexity for marginal UX gain (the background task completes in ~8s anyway)
3. The simple approach (fire-and-forget + warming signal) is correct for both first and subsequent queries

### Key properties

- ✅ Zero blocking on first query — ingestion runs in background
- ✅ Warming signal — explicit incomplete-state communication
- ✅ Existing data still usable — previously indexed content returns immediately
- ✅ No complexity — no ingestion-tracking events needed
- ✅ Graceful degradation — query returns best available data, even if partial

## Sentiment Agent → Market Context Agent Rebrand

### Problem

Phase 1 gave the RAG agent news + filing retrieval. The Sentiment agent's news/filing fetch then became redundant — both agents fetched the same data and produced overlapping narratives. The original redesign proposal wanted to gut the Sentiment agent entirely, but the CrewAI narrative engine was already producing useful qualitative analysis. The question was: what unique analytical lane could Sentiment own that RAG and Quant didn't?

### Solution

Rebrand "Sentiment Intelligence Agent" → "Market Context Agent" and repurpose its CrewAI narrative engine from bottom-up news analysis to top-down macro + peer positioning:

1. **New MCP tool `get_macro_indicators()`** (`finsight_server.py:305`): Fetches Treasury yields (10Y/2Y), VIX, DXY, sector ETF performance — all cached 15 min. No ticker argument (macro is global).

2. **New `src/shared/peer_sets.py`**: 33 hand-curated peer sets across 10+ sectors. Shared with Quant's `peer_comparison_node` (§8.5.5 in the plan). The resolver returns up to 5 peers, excluding the ticker itself.

3. **`_collect_data_parallel` rewritten** (`executor.py:39-86`): 3-step pipeline — macro + primary financials → resolve peers → parallel peer financials + prices. Drops `get_news_sentiment` and `get_company_filings` entirely.

4. **MarketContextCrew** (`crew.py`): Single agent, role "Market Context Analyst". Task outputs JSON: `narrative`, `macro_regime`, `relative_peer_positioning`, `overall_signal`, `confidence_score` (0-1), `key_tailwinds`, `key_headwinds`.

5. **Agent card** (`market_context_agent.json`): Two skills — `macro_regime_analysis`, `peer_landscape_analysis`.

### Why not keep Sentiment and RAG both fetching news?

Overlap. Both agents called `get_news_sentiment` and `get_company_filings` — the same MCP tools, the same cached data. The single-flight cache (Phase 2) already deduplicated the RSS fetches, but the orchestrator LLM still received two redundant narratives. Giving each agent a distinct data lane eliminates redundancy and makes the orchestrator's synthesis more efficient (RAG = document retrieval, Market Context = macro/peer positioning, Quant = numbers).

### Key properties

- ✅ No redundant data fetches — every agent owns distinct MCP tools
- ✅ Shared peer sets — Market Context and Quant use the same `peer_sets.py`
- ✅ Backward compatible — old agent name `sentiment_agent.json` removed; orchestrator discovers `market_context_agent.json` via `SubAgentClient.discover()`
- ✅ Langfuse trace continuity — trace name `market-context-agent-stream` replaces `sentiment-agent-stream`
- ✅ RAGAS rubrics updated — `macro_regime_quality` + `peer_positioning_quality` replace old sentiment rubrics

## Dynamic Peer Discovery via yfinance Industry/Sector Classes

### How peer discovery evolved

Peer discovery went through three iterations:

**Phase 3 (v1.31) — Static `peer_sets.py`**: Hand-curated map with ~33 entries. Required maintenance, had gaps, and broke silently when a ticker's industry wasn't in the map.

**Phase 4 (v1.32) — Yahoo Finance HTTP API**: New MCP tool `get_peers` hitting `/v6/finance/recommendationsBySymbol`. Returned engagement-optimized recommendations, not necessarily true industry peers. Required explicit `User-Agent` headers and had reliability issues with mid-cap/international tickers. Fallback chain: MCP → peer_sets.py → empty.

**Phase 5 (v1.34) — yfinance Industry/Sector classes**: Rewrote `get_peers` to use `yfinance.Industry(slug).top_companies` and `Sector(slug).top_companies`. Returns market-cap-weighted DataFrame of companies in the same classification — deterministic, no scraping, no cookies, no rate limits. `_industry_to_slug()` converts yfinance strings to URL slugs. Falls through industry → sector → empty.

### Why the third rewrite?

The Yahoo Finance HTTP recommendations API had three fundamental problems:
1. **Engagement-optimized**, not fundamental-similarity — "People also watch" returns tickers that Yahoo's algorithms think a user might click, not tickers that share industry/valuation characteristics
2. **Rate-limit fragility** — the HTTP endpoint returned 429 errors under moderate load, requiring the user-agent cat-and-mouse game
3. **No ticker coverage** — international and mid-cap tickers often returned empty, while large-caps like AAPL returned oddly specific recommendations

The yfinance `Industry`/`Sector` API solves all three: it's a deterministic data source (same classification every call), doesn't hit external HTTP endpoints (uses yfinance's cached data), and covers any ticker that yfinance knows about.

### Why remove the peer_sets.py fallback?

With the yfinance Industry/Sector approach, the static peer_sets.py became redundant — the MCP tool covers any yfinance ticker without gaps. Keeping a fallback to an incomplete static map risks returning wrong peers silently. A clean "Peer discovery unavailable" message when the MCP server is down is better than silently falling back to potentially stale static data.

### Why keep peer_sets.py at all?

`src/shared/peer_sets.py` now serves as: (1) the sector→ETF mapping for `get_scenario_shocks` (via `_SECTOR_ETF`), (2) documentation of expected peer groupings, (3) a reference for the yfinance Industry/Sector slug mapping. It's no longer called at runtime for peer discovery.

### Key properties

- ✅ Deterministic — same ticker always returns same peers
- ✅ No HTTP scraping — uses yfinance's cached Industry/Sector data
- ✅ No rate limits — no external HTTP calls for peer resolution
- ✅ Comprehensive coverage — any yfinance ticker works
- ✅ Clean failure mode — "unavailable" message instead of wrong peers
- ✅ peer_sets.py retained for ETF mapping and documentation

## Market Context Agent — Peer Key Fixes & Sector/Industry Restoration

### Problem

Two bugs silently degraded Market Context agent output after the peer refactor:

1. **Wrong `get_financials` response key** (`executor.py`): The code read `primary_fin.get("financials", {}).get("info", {})`, but `get_financials` MCP tool returns top-level keys directly — no `"financials"` wrapper. Every ticker got `info = {}`, so `sector` and `industry` were always empty, and `get_peer_tickers` returned `[]` for every ticker. This was a silent data loss — the agent still produced JSON, but `macro_regime` and `peer_landscape` analyses had no sector context.

2. **Undefined `sector`/`industry` in return dict** (`executor.py:73-74`): The `_collect_data_parallel` return dict referenced `sector` and `industry` variables that were never assigned — leftover from before the peer refactor removed the extraction code. CrewAI's `data.get("sector", "")` handled the `NameError` gracefully, but the keys were always empty strings.

### Solution

1. **Fix data extraction** (`executor.py`): Changed `primary_fin.get("financials", {}).get("info", {})` to `primary_fin.get("info", {})`. Also uses `res` (the full MCP response) directly for peer financials instead of the wrapper.

2. **Restore sector/industry extraction** (`executor.py`): Re-added `info = primary_fin.get("info", {}); sector = info.get("sector", ""); industry = info.get("industry", "")` before the `get_peers` call. The return dict now gets populated values.

3. **Expand peer sets** (`src/shared/peer_sets.py`): Added ~30 new entries covering missing industries (Discount Stores, Grocery Stores, Consumer Defensive/Cyclical sectors). Fixed em-dash vs hyphen mismatch — `_PEER_SETS` used "Software—Infrastructure" (U+2014) but yfinance returns "Software - Infrastructure" (hyphen+spaces). Added `_norm()` normalization function that collapses all dash variants to a single hyphen, with `_NORM_MAP` for O(1) fuzzy lookup.

### Why wasn't this caught by testing?

No integration tests for the Market Context agent's data pipeline existed. The agent produced structurally valid JSON regardless — the narrative was just less informed. These bugs highlight the need for integration tests that verify non-empty `sector`/`industry`/`peers` in the CrewAI output, especially after data-source refactors.

### Key properties

- ✅ Non-empty sector/industry now flows to CrewAI narrative
- ✅ Peer sets expanded to cover all major sectors
- ✅ Em-dash/hyphen normalization prevents silent empty sets
- ✅ Dynamic `get_peers` MCP tool works for any ticker
- ✅ `peer_sets.py` remains as fallback for MCP failures

## Peer Concurrency Cap — Why `asyncio.Semaphore(3)` for Peer Financials

### Problem

Both the Quant LangGraph and Market Context agents fetch `get_financials` for each peer ticker concurrently. When a ticker has 8 peers, this fires 8 simultaneous MCP calls. The MCP server processes them via `asyncio.gather` with a shared `_yfinance_limiter` (4 req/s token bucket). While the token bucket limits the *rate*, it doesn't limit the *number of in-flight requests* the server queues.

Under the old pattern, all 8 peer requests arrived at the MCP server simultaneously. The first 8 tokens were issued instantly (burst=8), but each yfinance `get_financials` call takes 1-3 seconds. The MCP client's `asyncio.wait_for(timeout=15)` wrapped each call — with 8 concurrent calls, the queue depth meant the last calls could exceed 15 seconds of wall-clock time, even though each individual yfinance call completed in <3s. The timeout fired before the queue cleared.

### Why not increase the MCP timeout instead?

The MCP timeout is already 15s (reduced from 30s in v1.31 for faster failure detection). Increasing it again would just mask the queue buildup issue — deeper queues would still timeout at any arbitrary threshold. The root cause is concurrency, not timeout duration.

### Why a Semaphore specifically?

`asyncio.Semaphore(3)` limits in-flight requests to 3, which is the smallest number that keeps the pipeline fed given yfinance's typical per-call latency (~1-3s). With 3 concurrent slots and 8 peers, the total wall-clock time is `ceil(8/3) × max_per_call_latency ≈ 3 × 3 = 9s` — well within the 15s timeout. Without the semaphore, 8 concurrent calls hit the rate limiter's burst of 8, all start their yfinance fetch, and the last ones timeout at ~15s.

The token bucket (`_yfinance_limiter`) handles *rate* (requests per second) — it prevents Yahoo from seeing 8 requests at the same microsecond. The semaphore handles *concurrency* (in-flight requests at the same moment) — it prevents the MCP server's request queue from exceeding the timeout window. They solve complementary problems.

### Why cap at 3 specifically?

3 is the "knee" in the latency/throughput curve:
- 1 concurrent → serial: 8 × 3s = 24s total, exceeds timeout
- 2 concurrent → ceil(8/2) × 3s = 12s total, within timeout
- 3 concurrent → ceil(8/3) × 3s = 9s total, comfortable margin
- 4+ concurrent → no significant throughput gain (yfinance rate limiter is the bottleneck, not CPU), increased timeout risk

3 gives comfortable headroom for the slowest peer (3s) while keeping total wall-clock time under 15s with margin.

### Key properties

- ✅ Limits in-flight peer requests — no queue buildup at MCP server
- ✅ Complements the token bucket (rate vs concurrency)
- ✅ Cap at 3 — optimal latency/throughput knee
- ✅ Applied in both Quant (`nodes.py`) and Market Context (`executor.py`) agents

## Quant Behavioral Signals — 8-Group Weighted Voting

### Problem

The Phase 1 Quant agent produced fundamentals + technicals + DCF — strong on logical analysis but blind to market psychology, insider behavior, and options flow. These signals (`put_call_ratio`, `insider_buying`, `short_interest`, `analyst_consensus`) capture what the broader market is *doing*, not just what the numbers say. The original Phase 3 plan assigned them to a redesigned Sentiment agent, but they're deterministic math on structured data — Quant's natural home.

### Solution

Add three new graph nodes to the Quant LangGraph, each fetching data via existing or new MCP tools and computing normalized signals (-1 to +1):

| Node | Data Source | Signal |
|---|---|---|
| `options_flow_node` (`nodes.py:900`) | `get_options_chain` MCP | put/call vol ratio, OI ratio, flow classification |
| `insider_signals_node` (`nodes.py:921`) | `get_company_filings` → Form 4 XML | net direction (90-day), CEO/CFO weighted |
| `analyst_positioning_node` (`nodes.py:979`) | `get_sentiment_indicators` (new) + `get_earnings_history` (new) | consensus score, upside %, short interest, squeeze risk |

All three fan out from `START` alongside the existing nodes. `format_output_node` collects all 8 signal groups (technical, fundamental, narrative, options, insider, positioning, macro, risk) with weights summing to 1.0:

```python
_SIGNAL_WEIGHTS = {
    "technical":    0.15,
    "fundamental":  0.15,
    "narrative":    0.10,
    "options":      0.12,
    "insider":      0.10,
    "positioning":  0.11,
    "macro":        0.12,
    "risk":         0.15,
}
```

Confidence formula (§8.6.2): `|composite| × (1 − std(present_signals))`

### Why raw weighted sum instead of normalized?

The plan's original `normalize_weights = {k: v / sum_present for k, v in _SIGNAL_WEIGHTS.items()}` renormalized weights to sum to 1.0 using only the signals present in a given ticker's response. This made confidence uninformative: a response with only 2 signals (e.g. technical + fundamental, sum=0.30) would normalize them to 0.50 each, producing the same composite as if all 8 were present. The fix (`nodes.py:228`) keeps the raw weighted sum — if only 2 of 8 signals are present, the composite is inherently lower (0.30 × signal values), which correctly reflects the reduced signal density.

### Key properties

- ✅ Deterministic — no LLM calls for signal computation
- ✅ Same peer resolver (`src/shared/peer_sets.py`) as Market Context Agent
- ✅ Normalized signals (-1 to +1) for uniform voting
- ✅ 8-group coverage across fundamental, technical, behavioral, and macro dimensions
- ✅ Confidence formula accounts for signal sparsity

## Quant Behavioral Signal Refinements: Options Flow, Insider Data, Monte Carlo & Schema Validator

### Options Flow — Why return `no_data` instead of misleading ratios?

When a ticker has zero options volume (no open positions, illiquid, or the options chain fetch returned empty), the old code computed `put_call_vol = 0/0 = NaN` and `oi_ratio = 0/0 = NaN`. These NaNs propagated through `_normalize_to_signal` as `0.0`, producing a neutral signal that looked like "no unusual activity" when it should mean "no data available."

**Fix** (`nodes.py:949`): Check total volume before computing ratios. If zero, return `flow_signal="no_data"`, `note="No options volume — ticker may lack active options market"`, and `put_call_vol_ratio=oi_ratio=0.5` (neutral). The `format_output_node` checks `flow_signal == "no_data"` and excludes it from behavioral signal aggregation rather than scoring it as neutral.

### Insider Signals — Why replace Form 4 keyword matching with yfinance structured MCP tool?

The original `insider_signals_node` used `get_company_filings` filtered for Form 4 documents, then matched keywords (`"P"`/`"A"` for buys, `"D"`/`"S"` for sells) on filing descriptions. Many Form 4 filings have empty or inconsistent description fields — the XML content contains the actual transaction data, but the MCP tool only returns the filing metadata, not the parsed XML.

**Phase 5 fix** (`src/mcp_tools/finsight_server.py`, `nodes.py:1022`): New `get_insider_transactions(ticker, days=90)` MCP tool using `yf.Ticker.insider_transactions` — returns structured buy/sell data with share counts and dollar values, not Form 4 keyword heuristics. The tool classifies each transaction row by its `Transaction` column ("Sale", "Buy", "Option Exercise") with buy/sell/other labels, then computes `summary` with `total`, `buys`, `sells`, `direction`, `net_shares`, `net_value`.

The node now reads `summary.buys`, `summary.sells`, `summary.direction` directly from the structured response. This is faster (no SEC EDGAR parsing), more reliable (structured yfinance data feed), and more informative (includes net share/value amounts). The MCP abstraction ensures `_yfinance_limiter` rate-limiting is applied and the result can be cached for any consumer.

### Monte Carlo — Why run on low-volatility tickers?

The original graph only ran `_run_monte_carlo` on the high-volatility path (stress test branch). Low-volatility tickers went through the DCF valuation path, which ran no simulation. This meant DCF-only tickers (utilities, consumer staples) had `monte_carlo = None`, breaking downstream consumers that expected percentiles or `prob_profit`.

**Fix** (`nodes.py:733`): `dcf_valuation_node` now runs `_run_monte_carlo` on the price data it already holds. The simulation is computationally cheap (~50ms for 5,000 paths) and produces the same percentile/prob_profit/VaR output regardless of volatility route. Added `_last_nonnull` reducer for `monte_carlo` in state to handle the concurrent writer scenario.

### Peer Comparison — Why remove the peer_sets.py fallback?

The v1.33 `peer_comparison_node` had a fallback chain: `get_peers` MCP → `src/shared/peer_sets.py` → empty. In v1.34 (Phase 5), the fallback was removed because the MCP tool now uses yfinance `Industry`/`Sector` classes instead of the Yahoo Finance HTTP recommendations API — it's more reliable, covers any ticker that yfinance knows about, and doesn't suffer from rate-limit issues that plagued the HTTP-based approach. The `src/shared/peer_sets.py` static map is inherently incomplete (80+ entries but still misses many sectors) and a silent fallback to a potentially wrong peer set is worse than a clean "Peer discovery unavailable" note.

**Fix** (`nodes.py:881`): Removed the `peer_sets.py` fallback call. When `get_peers` returns `[]`, the node returns a clean message: `"Peer discovery unavailable for {ticker} — get_peers returned no results. Restart MCP server if recently deployed."` This gives operators a clear signal that something is wrong with the MCP server, rather than silently falling back to potentially stale static data.

### Schema Validator — Why fix key paths?

`score_quant_deterministic()` (`src/shared/runtime_eval.py:614`) is a zero-LLM schema validator that runs on every Quant response. It had three bugs:
1. Wrong access path: `result["signal_scores"]` → correct `result["metrics"]["signal_scores"]`
2. Shortened group names: `"dcf"` in old signal score dict keys → `"dcf_value"` (actual key in `_SIGNAL_WEIGHTS`)
3. `conf` path: same deep-nesting fix

These made the validator always return `False` for well-formed responses — it was checking the wrong dict paths. The validator is now accurate: it checks all 8 signal groups present, weight sum ≈ 1.0, MC percentiles consistent, peer fields present, recommendation + confidence invariants.

### Key properties

- ✅ No-data handling for zero-volume options — `flow_signal="no_data"` instead of NaN
- ✅ Structured insider transactions — yfinance DataFrame via MCP tool, not Form 4 keyword heuristics
- ✅ Monte Carlo runs on both volatility paths — DCF tickers get simulation too
- ✅ MCP peer discovery uses yfinance Industry/Sector classes — no static fallback needed
- ✅ Schema validator actually validates — corrected key paths and nesting

## Redis Two-Level Cache (L1 TTLCache + L2 Redis Write-Through)

### Problem

MCP tool results are cached in-process via `TTLCache` (`src/shared/ttl_cache.py:25`). In a multi-process deployment (e.g. Docker with 3 agent containers each calling the same MCP tools), each process maintains its own cache — the first request to each process pays the full yfinance/RSS fetch cost. A shared cache eliminates this redundancy.

### Solution

`src/shared/redis_cache.py` implements a two-level cache:

- **L1**: In-process `TTLCache` (fast, no network). Hit → return immediately.
- **L2**: Redis write-through. L1 miss → read from Redis → populate L1. Every L1 `set()` propagates to Redis.

```python
def make_cache(ttl_seconds: int = 300, name: str = "") -> TTLCache | RedisCache:
    if REDIS_URL:
        return RedisCache(ttl_seconds=ttl_seconds, name=name)
    return TTLCache(ttl_seconds=ttl_seconds)
```

All existing `TTLCache(...)` instantiations in `finsight_server.py` remain valid — `make_cache()` returns a `TTLCache` when `REDIS_URL` is unset, identical behavior to before. When `REDIS_URL` is configured, the returned `RedisCache` adds shared persistence transparently.

### Why not Redis-only?

Latency and complexity. An in-process L1 is ~1µs (dict lookup) vs ~1ms (Redis round-trip). The L1 covers the common case (repeated tool calls within the same request), while L2 handles cross-process sharing. The write-through pattern ensures L2 is always fresh without a separate invalidation mechanism.

### Key properties

- ✅ Transparent drop-in — `make_cache()` returns same interface as `TTLCache`
- ✅ No migration effort — existing `_cache_*` variables accept `TTLCache` or `RedisCache`
- ✅ L1 speed for repeated calls, L2 sharing across processes
- ✅ Write-through keeps L2 fresh automatically
- ✅ Graceful degradation — Redis unreachable falls back to L1-only

## RAGAS Eval Hardening — Circuit Breaker, SHA-256 Dedup, Burst Limiter

### Problem

Each RAG/Quant/Sentiment response fires 2-5 RAGAS LLM metric calls against the same Qwen3-30B model that serves live user queries. Three failure modes were observed (`logs/sentiment.log`, `logs/rag_agent.log`):

1. **Model unload storms**: A single "Model unloaded" error triggers 3 retries per metric × 5 metrics = 15 concurrent load attempts, overwhelming LM Studio and slowing the next user query.
2. **Identical response re-evals**: When the orchestrator issues the same query in back-to-back requests (e.g. cache miss then cache hit), every metric re-scores the identical (input, response) pair.
3. **Metric timeout cascade**: A single stuck metric (e.g. Faithfulness with 3000-token context) blocks all other metrics indefinitely.

### Solution

Four hardening layers in `src/shared/runtime_eval.py`:

1. **Circuit breaker** (`_CIRCUIT_MAX_FAILURES=5`): After 5 consecutive metric failures, all eval is skipped for 5 min. Per-metric `_last_failure` tracking for granular reset.

2. **SHA-256 dedup** (`_dedup_seen` TTL dict, 1h): `sha256(f"{input}|{response}")` key — skips eval when the exact same query+response pair was scored within the last hour.

3. **Burst limiter** (`_burst_ok()`): Deque of timestamps enforces `EVAL_BURST_LIMIT` evaluations per minute per process. Oldest timestamps evicted on overflow.

4. **Per-metric timeout** (`_score_metric_with_timeout()`): Each RAGAS metric wrapped in `asyncio.wait_for(timeout=EVAL_METRIC_TIMEOUT, default=90)`. Timeout raises `CancelledError`, sibling metrics continue.

The unified gate `_gate_ok()` combines all four: `EVAL_RUNTIME_DISABLED → False` | circuit tripped → `False` | burst exceeded → `False` | dedup hit → `False` | else → `True`.

### Why four layers instead of one?

Each addresses a distinct failure mode. The circuit breaker handles sustained failures (model offline). The burst limiter handles transient spikes (batch processing). Dedup handles redundant work (identical responses). Timeouts handle individual metric issues (overly long contexts). Removing any one layer still leaves exposure.

### Key properties

- ✅ Circuit breaker prevents eval storm cascade
- ✅ SHA-256 dedup eliminates redundant LLM calls for repeated responses
- ✅ Burst limiter protects against eval batch-processing spikes
- ✅ Per-metric timeout prevents one stuck metric from blocking others
- ✅ All controlled via env vars (`EVAL_RUNTIME_DISABLED`, `EVAL_BURST_LIMIT`, `EVAL_METRIC_TIMEOUT`)
- ✅ Zero-LSM `score_quant_deterministic()` validates schema without LLM calls

## Date-Aware Semantic Cache

### Problem

The semantic cache (`src/shared/semantic_cache.py`) stores responses keyed by query text. A user asking "Analyze NVDA" on Tuesday would hit the cache from Monday's analysis — missing overnight news, price moves, or macro shifts. The cache was too effective: it served stale content across trading days.

### Solution

Tag each cache entry with the current `YYYY-MM-DD` date on write, and filter by date on read:

```python
# shared/semantic_cache.py — set()
today = date.today().isoformat()  # YYYY-MM-DD
self._collection.add(
    ids=[entry_id],
    embeddings=[embedding],
    metadatas=[{"date": today, "response": ...}],
    documents=[query],
)

# get()
results = self._collection.query(
    query_embeddings=[embedding],
    n_results=1,
    where={"date": today},  # only today's entries
)
```

A ChromaDB `where` filter on the metadata `date` field ensures only entries from the current date match. Same query on different days → no metadata match → cache miss → new analysis generated and stored.

### Why not a TTL?

A fixed TTL (e.g. 24 hours) would still serve stale data: a query at 9 AM Monday and another at 9 AM Tuesday would be 24 hours apart, but in practice the open market only trades 6.5 hours per day. A date filter is simpler: it aligns with calendar boundaries and avoids edge cases around market holidays and weekends.

### Key properties

- ✅ Same-day repeat queries still hit cache (fast path)
- ✅ Cross-day queries always regenerate (fresh analysis)
- ✅ No TTL configuration — date boundary is unambiguous
- ✅ ChromaDB `where` filter is O(1) with an index on metadata

## RAG Startup Warm-Up

### Problem

The first RAG query after server start paid a ~3-5s cold-start tax: loading the HuggingFace `all-MiniLM-L6-v2` embedder, connecting to ChromaDB, and loading the CrossEncoder reranker all happened on first use. This made the first query of a session or after a restart noticeably slower than subsequent ones.

### Solution

`_do_prewarm()` at `src/financial_rag/server.py` runs once on Starlette startup via `asyncio.to_thread`:

```python
async def _do_prewarm():
    t0 = time.monotonic()
    # 1. Embedder
    embedder = HuggingFaceEmbedding(model_name=EMBED_MODEL)
    embedder._model  # triggers actual model load
    _ = embedder.get_text_embedding("warmup")
    logger.info("Embedder loaded in %.2fs", time.monotonic() - t0)

    # 2. ChromaDB collections
    for coll in ("sec_filings", "news", "earnings"):
        index._chroma.get_or_create_collection(coll)
    logger.info("ChromaDB collections ready in %.2fs", ...)

    # 3. CrossEncoder (lazy-loaded in HybridSearchPipeline)
    from .hybrid_search import HybridSearchPipeline
    hp = HybridSearchPipeline()
    hp._get_reranker()  # triggers model load
    logger.info("Reranker loaded in %.2fs", ...)
```

Each stage logs elapsed seconds so the cold-start budget is visible in server logs. The warm-up runs concurrently with the first A2A request (Starlette startup fires before the server accepts requests), so the first user query typically finds all models already loaded.

### Why not lazy-load as before?

Lazy-load is simple but unpredictable: the ~3-5s delay hits the first user, not the deployer. Pre-warming shifts the cost to startup time where it's visible in logs and doesn't affect user-facing latency. The trade-off is ~5s longer container startup, which is acceptable for a long-running server process.

### Key properties

- ✅ First RAG query: ~0s warm-up penalty (was ~3-5s)
- ✅ Per-stage timing logged for cold-start budget analysis
- ✅ Runs in thread executor — doesn't block the asyncio event loop
- ✅ Idempotent — ChromaDB `get_or_create_collection` is safe to call multiple times
- ✅ Graceful if models fail — warm-up errors are logged but don't crash the server

## Live Sector-Aware Scenario Shocks via MCP get_scenario_shocks

### Problem

The stress test node originally used hardcoded S&P 500 crash percentages in `_SECTOR_ETF_MAP` with the `get_macro_indicators` YTD approach. This had three issues: (1) YTD performance is a single-year snapshot, not a crash scenario — a mild bear market could be -10% YTD while the historical crash templates (2008: -37%, 2020: -34%) are far more severe; (2) the sector ETF YTD was a single point estimate with no scenario diversity; (3) the formula `mkt_decline * beta` on a single YTD value couldn't differentiate between orderly drawdowns and crash scenarios.

### Solution

**MCP tool `get_scenario_shocks(sector)`** (`src/mcp_tools/finsight_server.py`): Computes historical crash returns from live price data using sector-specific ETFs resolved via `_SECTOR_ETF` mapping:

| Sector | ETF | Scenario Windows |
|---|---|---|
| Technology | QQQ | 2008 crash, 2020 COVID, dot-com, 2022 bear |
| Consumer Defensive | XLP | same windows, different returns |
| Energy | XLE | same windows, different returns |
| (14 sectors mapped) | | |

Each scenario window is a (`start_date`, `end_date`) tuple. The tool fetches the sector ETF's full price history, slices the window, and computes `return = price_end / price_start - 1`. Falls back to `^GSPC` (S&P 500) when the sector ETF lacks history for a given window (e.g. XLRE doesn't cover 2008). Falls back to hardcoded `_SHOCK_FALLBACKS` when price history is unavailable entirely.

**Four historical scenarios** with actual peak-to-trough returns:

| Scenario | Window | S&P Fallback | Purpose |
|---|---|---|---|
| `market_crash_2008` | 2007-10-09 → 2009-03-09 | -56.5% | Global financial crisis |
| `covid_crash_2020` | 2020-02-19 → 2020-03-23 | -34.0% | Pandemic panic |
| `dot_com_bubble` | 2000-03-24 → 2002-10-09 | -49.1% | Tech bust |
| `mild_recession` | 2022-01-03 → 2022-10-12 | -25.4% | Recent bear market |

**Beta-adjusted**: Stress test applies `max(-0.95, scenario_return * beta)` to each scenario's sector-specific return — so a high-beta tech stock gets a larger stress hit than a low-beta utility, both relative to their sector's actual historical crash experience.

### Why historical crash windows instead of YTD or Monte Carlo scenarios?

Historical crash windows capture real market dynamics (serial correlation, volatility clustering, sector rotation) that synthetic scenarios miss. A 2008-style crash on a defensive sector (XLP: -36%) looks very different from the same crash on tech (QQQ: -43%) — using the actual historical returns preserves these sector-specific characteristics. The 4 windows cover a range of severity and market regimes (credit crisis, pandemic shock, tech bust, recession).

### Why sector-specific ETFs instead of just S&P 500?

A 2008 crash on financials (XLF: -60%) was far more severe than on healthcare (XLV: -25%). Using S&P 500 returns (-37%) for every sector would overstate stress on defensive stocks and understate it on cyclical stocks. The sector ETF approach produces stress scenarios that are calibrated to what actually happened to similar companies during each crisis.

### Why 7-day cache?

Crash windows don't change — they're historical time ranges. Once computed, a sector's crash returns are valid until the next data revision. The 7-day cache avoids re-fetching price history on every Quant request while auto-refreshing weekly (in case of yfinance data corrections).

### Key properties

- ✅ 4 historical crash scenarios — actual peak-to-trough returns per sector
- ✅ 14 sector ETFs mapped — sector-specific stress calibration
- ✅ Fallback chain — sector ETF → S&P 500 → hardcoded fallback
- ✅ Beta-adjusted — ticker's own volatility amplifies or dampens sector shock
- ✅ Hard floor at -95% — prevents impossible loss values
- ✅ 7-day cached — weekly refresh of crash returns
- ✅ Visible `index_used` — logged for traceability

## Sector-Relative Fundamental Scoring

### Problem

The original `_score_fundamental_value()` and `_score_fundamental_quality()` used absolute universal thresholds: PE < 12 → good, ROE > 25% → excellent. These thresholds were arbitrary and sector-blind — a PE of 20 might be cheap for a tech stock but expensive for a utility. The same ROE of 15% would score "moderate" on the absolute scale, but might be the sector median for banks and quite strong for retail.

### Solution

**`_relative_score(value, median, higher_is_better)`** (`src/quant/nodes.py`): Scores a metric relative to its sector median ratio. Returns in [-1, 1]:

```
For higher_is_better (ROE, margin):
  ratio > 2.0× median → +1.0
  ratio > 1.5× median → +0.6
  ratio > 1.1× median → +0.2
  ratio > 0.7× median → -0.1
  ratio > 0.4× median → -0.4
  else → -0.7

For lower_is_better (PE, EV/EBITDA, D/E):
  ratio < 0.5× median → +1.0 (much cheaper)
  ratio < 0.75× median → +0.6
  ratio < 0.95× median → +0.2
  ratio < 1.2× median → -0.1
  ratio < 1.6× median → -0.4
  else → -0.7
```

**Sector medians computed in `peer_comparison_node`** (`nodes.py:919-935`): After ranking all peer tickers on each fundamental metric, the node computes the median value per metric across all peers. These medians are passed to `format_output_node` via `peer_comparison.medians`.

**Fallback**: When no peer medians are available (no peers found, or all peers lack a given metric), `_score_fundamental_value` and `_score_fundamental_quality` fall back to the original absolute thresholds — no regression for tickers without peer coverage.

### Why relative scoring instead of industry-standard thresholds?

Industry-standard thresholds (e.g. "PE < 15 is cheap") are broad heuristics that work across many sectors but miss sector-specific pricing. A biotech stock with PE = 30 might be at the 90th percentile for its sector (very expensive) while a consumer staple with PE = 30 might be at the median (fairly valued). Relative scoring captures the sector-specific context that absolute thresholds miss, without requiring separate threshold sets per sector.

### Why median instead of mean?

Median is robust to outliers — one peer with an extreme PE (e.g. a startup with PE = 300) would skew the mean but barely affect the median. For peer sets of 3-8 tickers, a single outlier can dominate the mean. Median gives a stable reference point that reflects the typical peer's valuation.

### Why D/E added to peer comparison?

Debt-to-equity varies enormously by sector — utilities and banks carry high leverage by design, while tech companies often have near-zero debt. Adding D/E to the peer comparison enables the relative scoring to capture whether a ticker is over- or under-leveraged relative to its sector, which is a more informative signal than an absolute "D/E > 80 is bad" threshold.

### Key properties

- ✅ Scores relative to sector median — PE, EV/EBITDA, ROE, OpMargin, D/E
- ✅ Higher-is-better and lower-is-better logic separately handled
- ✅ Absolute fallback when no peers available — no regression
- ✅ Median computed from peer_comparison_node — no separate data fetch
- ✅ D/E added to peer comparison for leverage context

## Dynamic Peer Discovery via yfinance Industry/Sector Classes

### Why replace Yahoo Finance HTTP API with yfinance Industry/Sector?

The original `get_peers` tool used Yahoo Finance's `/v6/finance/recommendationsBySymbol` HTTP endpoint ("People also watch"). This HTTP API had several problems: (1) required explicit `User-Agent` headers that changed periodically, (2) occasionally returned empty or stale recommendations for mid-cap and international tickers, (3) returned "recommended" tickers that were not always true industry peers (Yahoo's algorithm optimizes for user engagement, not fundamental similarity).

**New approach** (`src/mcp_tools/finsight_server.py`): Uses yfinance's built-in `Industry(slug).top_companies` and `Sector(slug).top_companies` methods. These return market-cap-weighted DataFrames of companies in the same industry/sector classification. The mapping is deterministic — same ticker always returns the same peers (modulo yfinance data updates).

`_industry_to_slug(name)` converts yfinance industry/sector strings (e.g. "Semiconductor Equipment & Materials") to URL slugs (`semiconductor-equipment-materials`) for the yfinance API. Falls through from industry to sector when the industry slug returns no data.

### Why remove the curated peer_sets.py fallback from the quant agent?

The original Phase 3 fallback chain was: MCP `get_peers` → `src/shared/peer_sets.py` → empty list. In Phase 5, the curated fallback was removed from `peer_comparison_node` because both agents now call the same MCP tool, and the `peer_sets.py` static map is inherently incomplete (only covers ~80 industry/sector strings). The MCP tool using yfinance Industry/Sector classes is more comprehensive (covers any ticker that yfinance knows about) and doesn't need a static fallback. MCP server cold-start or rate-limit failures produce a clean "Peer discovery unavailable" note instead of silently falling back to a potentially wrong static peer set.

### Why keep peer_sets.py at all?

`src/shared/peer_sets.py` is still used by the `_SECTOR_ETF` mapping in `get_scenario_shocks` (which maps sector strings to ETFs) and serves as documentation of expected peer groupings. It's no longer referenced by `peer_comparison_node` or `MarketContextAgent` for runtime peer discovery.

### Key properties

- ✅ Deterministic peers — yfinance Industry/Sector classification, not engagement-optimized recommendations
- ✅ No HTTP scraping — uses yfinance's built-in yfin.Industry/Sector API
- ✅ Slug normalization — handles any yfinance industry string
- ✅ Falls through industry → sector → empty
- ✅ peer_sets.py retained for ETF mapping and documentation

## Structured Insider Transactions via MCP get_insider_transactions

### Why replace Form 4 keyword matching with yfinance structured data?

The original `insider_signals_node` called `get_company_filings(ticker, form_types="4", limit=15)` and keyword-matched filing descriptions for buy/sell signals. This was unreliable: (1) Form 4 filing titles are inconsistent — some say "Statement of changes in beneficial ownership of securities" (uninformative), others say "Sale - 10000 shares" (informative); (2) the filing description field in the SEC EDGAR response is truncated or empty for many filings; (3) keyword matching on "purchase" vs "sale" misses nuanced transactions like option exercises, grants, and gifts.

**New `get_insider_transactions` MCP tool** (`src/mcp_tools/finsight_server.py`): Uses yfinance `Ticker.insider_transactions` which returns a structured DataFrame directly from Yahoo Finance's insider data feed. Each row has: `Insider`, `Position` (CEO/CFO/COO), `Transaction` ("Sale", "Buy", "Option Exercise"), `Shares`, `Value`, `Start Date`. The tool:
1. Fetches the DataFrame
2. Filters to the lookback window (default 90 days)
3. Classifies each row as buy, sell, or other based on the `Transaction` column
4. Computes `net_shares` and `net_value` (sum of buys minus sum of sells)
5. Emits a `direction` summary (net_buy/net_sell/neutral)

The node reads the structured summary directly — no parsing, no keyword heuristics.

### Why not use the existing `yf.Ticker.insider_transactions` directly in the node?

The Quant agent doesn't import `yfinance` — all data access goes through the MCP layer. Keepinsider data behind the MCP tool ensures the rate limiter (`_yfinance_limiter`) protects Yahoo from excessive calls, and the result is cached/structured for any consumer (not just the Quant node). The MCP abstraction is consistent with how all other data sources are accessed.

### Key properties

- ✅ Structured buy/sell data — no Form 4 keyword heuristics
- ✅ Net shares and net dollar values — quantitative signal, not just direction
- ✅ 90-day lookback — configurable via `days` parameter
- ✅ MCP rate-limited — protects Yahoo Finance API
- ✅ Cache-friendly — calls are idempotent for same ticker+window

### Why move `yf.Ticker()` calls to `loop.run_in_executor()`?

**Problem**: `yf.Ticker.insider_transactions` and `yf.Ticker.history(period="max")` are synchronous DataFrame operations that block the asyncio event loop. When the MCP server handles multiple concurrent tool calls (e.g. `get_financials` for 8 peer tickers), a blocking yfinance call stalls all in-flight coroutines — the event loop cannot switch to another task until the blocking call returns.

`get_insider_transactions` was a single DataFrame property access (~200ms), tolerable in isolation. But `_get_scenario_shocks_uncached` calls `history(period="max")` which fetches 25+ years of daily OHLCV data — this can take 2-5 seconds for a single ETF. When called during peer analysis (which already has 8 concurrent `get_financials` calls in-flight), the blocking history fetch starves the financials requests, causing them to exceed their `asyncio.wait_for` timeout.

**Solution (first pass)**: The two worst offenders wrapped with `await loop.run_in_executor(None, lambda: yf.Ticker(...))`.

**Followup (second pass)**: After the first fix, users still saw intermittent `httpx.ReadError` mid-call on `get_financials`, `get_prices`, etc. The underlying cause was the same — other yfinance tools (`stock.history()`, `.financials`, `.balance_sheet`, `.cashflow`, `.info`, `.option_chain()`, `.options`, `stock.calendar`, `stock.earnings_dates`) were still making synchronous calls on the event loop. While individually fast (~50-200ms), under concurrent peer analysis the cumulative blocking window was large enough to stall FastMCP's SSE keepalive writes, causing the client to disconnect. Fixed by wrapping **all 9 synchronous yfinance call sites** in `run_in_executor`. A defence-in-depth change was also made to the MCP client: `httpx.ReadError`, `httpx.ConnectError`, and `httpx.NetworkError` were added to `_TRANSIENT_EXC` so transient SSE blips retry instead of failing immediately.

### Key properties

- ✅ Event loop unblocked — no coroutine starvation during yfinance data fetches
- ✅ Thread-safe — `asyncio.get_event_loop()` runs the executor on the same loop
- ✅ Zero API change — tool signatures and return types identical
- ✅ All 9 synchronous yfinance call sites wrapped (up from the original 2)

## Peer Sets — Expanded with Normalised Key Matching

### Why expand from 33 to 80+ entries?

The original 33 entries covered only broad categories ("Technology", "Healthcare", "Financials"). When the Market Context agent looked up "Banks—Regional" (with em-dash), it didn't match "Banks - Regional" (with hyphens) — the match failed silently. The expanded version covers all major industries with their exact yfinance strings, plus alternative dash variants. Each sector also gets a canonical "Sector"-level entry as catch-all.

### Why the `_norm()` normalisation function?

Yahoo Finance's industry/sector strings use inconsistent punctuation: semiconductor—equipment vs semiconductor - equipment vs semiconductor– equipment. The `_norm()` function collapses all dash variants (em dash U+2014, en dash U+2013, hyphen U+002D with optional surrounding spaces) to a single hyphen, then lowercase and strips. `_NORM_MAP` pre-computes all normalized forms at module load for O(1) lookup. Without this, every em-dash/hyphen mismatch silently returned an empty peer set.

### Key properties

- ✅ 80+ entries across all major sectors and industries
- ✅ Em-dash/hyphen normalization — fuzzy key matching
- ✅ O(1) lookup via pre-built `_NORM_MAP`
- ✅ Lookup order: exact industry → normalized industry → exact sector → normalized sector
- ✅ Self-excluding — ticker never appears in its own peer set

## RAG Index Warming via A2A WORKING Events — From Fire-and-Forget to Await-able

### Problem

The RAG agent's `_build_response()` fired `asyncio.create_task(self._ensure_ingested(ticker))` and returned `{"_warming": True, "summary": "Index is warming for [ticker]..."}` immediately. The orchestrator saw a completed task with a warming placeholder — no data to synthesize. The user would then have to re-query to get the actual analysis after ingestion completed in the background. This was a poor UX: every first query for a new ticker returned a "come back later" placeholder regardless of whether filing downloads were fast (5s) or slow (30s+).

The root issue: A2A only reports terminal events (COMPLETED/FAILED). The RAG agent couldn't say "I'm working on it, hold on" — it had to return something immediately or block the A2A protocol indefinitely.

### Solution

**Two-part fix** using A2A streaming's intermediate states:

1. **RAG agent emits `TASK_STATE_WORKING` SSE events** (`src/financial_rag/executor.py:19-34`): Before entering ingestion, the RAG agent yields a `ServerTaskUpdateEvent` with `status: TASK_STATE_WORKING` and a message like `"Ingesting SEC filings and news for [ticker]..."`. This keeps the A2A channel open and tells the orchestrator "not done yet, but making progress."

2. **Orchestrator processes WORKING events** (`SubAgentClient` via `a2a-sdk`): The `BaseClient`'s streaming event handler skips non-terminal events by default. Changed to accumulate WORKING events — when the streaming loop gets a `status_update` with `TASK_STATE_WORKING`, it updates a `_status_msg` on the client and continues listening. Once the terminal event arrives, the accumulated working messages are prepended to the result text, so the orchestrator LLM sees both the progress message and the final analysis.

3. **Index check before ingestion** (`_ensure_ingested`): The function now checks `is_filing_ingested(edgar_url)` before downloading — if all filings are already indexed, it skips ingestion entirely and the agent returns data from the existing index. Combined with the WORKING events, the path is:
   - Already indexed → immediate data return (no WORKING event)
   - Not indexed → emit WORKING → download + ingest → emit data
   - Timeout → orchestrator's per-agent timeout catches it

### Why not just increase the request timeout?

The A2A protocol is designed for near-immediate responses — the orchestrator's `asyncio.wait_for` wraps the entire `send_message()` call. Increasing the timeout would fix the symptom but not the UX: the user would still wait silently. WORKING events provide progress visibility and let the orchestrator send intermediate status back if desired (e.g. "Analyzing NVDA... ingesting 10-K filings").

### Key properties

- ✅ No more "come back later" placeholders — first queries on new tickers work end-to-end
- ✅ WORKING events — progress visibility for long-running ingestion
- ✅ Existing index check — already-indexed tickers skip ingestion entirely
- ✅ Timeout preserved — orchestrator still has safety net
- ✅ Minimal protocol change — WORKING events are part of the A2A spec

## AG-UI Bridge over ADK Web (v1.36)

### Why a custom SSE bridge instead of `adk web`'s built-in WebSocket?

CopilotKit speaks AG-UI — a streaming protocol that goes through `@ag-ui/client`. ADK Web uses a different WebSocket protocol aimed at its own web UI. Rather than reverse-engineer ADK Web's protocol or hack CopilotKit to speak it, we built a bridge layer (`agui_bridge.py`) that:

1. Accepts `RunAgentInput` JSON (AG-UI's input schema)
2. Translates ADK runner events into AG-UI SSE frames (`TextMessageStartEvent`, `ToolCallStartEvent`, `StateDeltaEvent`, etc.)
3. Streams them back as `text/event-stream`

This keeps the frontend agnostic to the backend framework — CopilotKit connects to `/a2a-agui` via an `HttpAgent` and never knows ADK exists.

### Why SSE instead of WebSocket?

SSE is simpler, unidirectional, and sufficient for streaming LLM responses. WebSocket would require managing connection lifecycle, heartbeats, and reconnection logic on both sides. SSE is natively supported by `StreamingResponse` (Starlette) and `EventSource` (browser/CopilotKit). For the use case — streaming text and tool events from server to client — SSE covers everything.

### Why camelCase aliases in the SSE helper?

CopilotKit's Zod schemas expect camelCase keys (`messageId`, `toolCallId`, `runId`). ADK's AG-UI types generate snake_case by default. The `by_alias=True` dump in `src/shared/agui_sse.py:sse()` lets us define camelCase aliases on the Pydantic models once and have all serialization use the CopilotKit-compatible form automatically.

### Why strip null values from SSE frames?

CopilotKit's Zod validation rejects `null` where an optional field isn't explicitly required. Fields like `rawEvent`, `parentRunId`, `name`, `encryptedValue` are optional metadata — when null, they cause Zod parse errors and the entire event is dropped. Selective null-stripping (`_STRIP_KEYS`) removes only from the event envelope, preserving null in data-carrying fields (`snapshot`, `delta`, nested `content`) where null is semantically meaningful.

### Why server-side API proxies (`/api/traces`, `/api/health`) instead of direct browser calls?

Two reasons:
- **Credentials**: Langfuse secret key and health endpoint URLs never leave the backend. The Next.js server-side `fetch` handles auth; the browser gets a clean JSON response.
- **CORS**: Langfuse and sub-agent health endpoints are on different ports (Langfuse on 4000, sub-agents on 8001+) — the browser would need CORS headers. A same-origin proxy avoids this entirely.

### Why CopilotKit over building a custom chat UI?

CopilotKit provides a production-grade chat interface (message list, input, streaming, function call rendering) out of the box. Building the same from scratch against raw AG-UI events would be ~500+ lines of React state management. CopilotKit's `CopilotKit` + `CopilotChat` components handle this with <50 lines of code. The trade-off is dependency on a third-party library, but it's a mature one (1.59, 2M+ weekly downloads) with a stable AG-UI integration.

## Auto-Save Brief Fallback (v1.36)

### Why detect `send_message` as an analysis signal?

Local models (e.g. ministral-3b, qwen-2.5) don't reliably call `save_brief` after synthesizing a response. They may generate the full analysis text but skip the function call — meaning the analysis is never persisted to `TickerMemory`, the same-day cache never populates, and subsequent queries re-run all agents.

`send_message` is the tool the orchestrator uses to delegate to sub-agents. If `send_message` was called, analysis was performed — the turn produced investment research, not a casual chat. Checking for `_has_send_message_call(session.events)` is a reliable heuristic: if the LLM delegated to agents, there's analysis to save.

### Why pattern-based extraction of rec/conf instead of parsing structured output?

Structured output (e.g. JSON in the system prompt) is fragile with small local models — they often produce malformed JSON or omit fields. The heuristic regexes (`_REC_PATTERN`, `_CONF_PATTERN`) search the LLM's natural language text for "BUY", "confidence: 85%", etc. This works with any model regardless of instruction-following quality. The `store_minimal` method on `TickerMemory` accepts the extracted values directly without requiring the `brief_json` schema that `save_brief` normally populates — meaning the auto-saved brief is slightly less structured but guaranteed to persist.

### Why both `TickerMemory` and `PerformanceTracker`?

Two separate storage systems:
- `TickerMemory` → the brief (text + recommendation) for same-day caching and history
- `PerformanceTracker` → the recommendation record for portfolio/performance tracking

The LLM's normal `save_brief` call handles both. When we auto-save, we must write to both to keep the data consistent.

## Full Synthesis Persistence — After-Turn Update Revisited (v1.36)

### Why re-introduce an after-turn update after removing it in v1.35?

The v1.35 approach stored the full analysis directly in `save_brief` by extracting text from `session.events` at the moment `save_brief` was called. This worked when the LLM generated analysis text *before* calling `save_brief`. But ADK often splits the generation into multiple events:

1. Event A: long analysis text (`author=model`)
2. Event B: function call `save_brief` with short ack like `"Brief saved for NVDA"`

When `save_brief` fires (event B), the `_synthesis_text_from_context` helper sees both events — but `save_brief`'s own content is the short ack, and the analysis lives in the preceding event. The v1.35 approach correctly picked the longest text, so in practice it worked for most models.

The v1.36 change is incremental: **after** the turn completes, the `_persist_memory_callback` / `_store_memory` now checks whether the analysis text in session events is *longer* than what was stored by `save_brief`. If so, it calls `tm.update_response_text()` to overwrite the brief with the full synthesis. This handles the edge case where:
- The LLM called `save_brief` early with a placeholder
- The real analysis text came after in a later event batch
- The v1.35 extraction missed it because it ran at `save_brief` time

The same logic applies to the A2A executor path (`_store_memory` in `agent_executor.py`), ensuring both paths converge on the full text.

### Why compare text lengths instead of always overwriting?

Some LLMs call `save_brief` with the *complete* analysis (text length = full synthesis). Overwriting with another event's text would be a no-op at best and destructive at worst if the extracted text is shorter. Length comparison is a safe heuristic: the longest model-generated text in the turn is the best candidate.

### Why not use the A2A WORKING event approach for this (à la RAG warming)?

The RAG warming problem was about keeping the A2A channel open during long ingestion. This is a post-hoc data quality fix — the analysis text exists but was stored incorrectly. No protocol changes needed; just a longer lens on what to persist.

## Case-Insensitive Ticker Lookup (v1.36)

### Why uppercase the entire query at `extract_ticker` entry?

Before v1.36, `extract_ticker("wmt")` returned `None` because the regex `[A-Z]{1,5}` required uppercase. `extract_ticker("wmt financials")` would also fail unless the ticker appeared in parens (`(WMT)`). This meant:
- Same-day cache missed for lowercase input (`wmt` → no match → `"unknown"` → cache skipped)
- Case-sensitive cache key (`ticker` in `_get_today_cached_text`) would miss even if extraction worked

The fix: `query = query.upper()` at the top of `extract_ticker`. The regexes are all uppercase — they match `WMT` whether the user typed `wmt`, `Wmt`, or `WMT`. The `_is_financial_stop_word` check and parens patterns also operate on the uppercased input, so no existing logic breaks.

### Why not use `re.IGNORECASE` instead?

`re.IGNORECASE` would work but `query.upper()` is simpler, more visible, and avoids the `re` flag mental overhead when reading the function. It's also marginally faster — the regex engine doesn't need to check case at every character.

## Weighted Vote Normalization (v1.36)

### Why redistribute missing signal weights instead of ignoring them?

The quant agent's behavioral voting uses 8 signal groups (options flow, insider transactions, short interest, etc.), each with a fixed weight in `_SIGNAL_WEIGHTS`. Before v1.36:

```python
composite = sum(group_scores[k] * _SIGNAL_WEIGHTS.get(k, 0) for k in present)
```

When a signal was absent (score = 0.0), it was filtered out via `present = {k: v for k, v in group_scores.items() if v != 0.0}`. But its weight was *also* excluded — meaning the total divisor was < 1.0. This dampened the composite score proportionally to the number of missing signals.

Example: if only 2 of 8 signals are present, each with weight 0.15, the composite → sum of (score × 0.15) = 0.30 × average_score. A strong BUY signal of +1.0 in both groups gives composite = 0.30, which is still in HOLD territory (threshold 0.15). **The more signals missing, the harder it was to get a confident non-HOLD vote.**

The fix redistributes weights:

```python
scale = 1.0 / total_present_weight
composite = sum(group_scores[k] * _SIGNAL_WEIGHTS.get(k, 0) * scale for k in present)
```

Now a single +1.0 signal with weight 0.15 (scaled to 1.0) produces composite = 1.0 → confident BUY. This is correct: the present signal says BUY with high conviction; missing data shouldn't dilute its vote.

### Why sort FCF periods descending in FCF checks?

`_get_fcf_from_financials` iterates period keys to find positive free cash flow. Before v1.36, iteration order was insertion order (Python 3.7+) — which for yfinance data roughly follows ascending order (oldest first). If the oldest period had positive FCF but the most recent year had negative FCF (e.g. a capex-heavy investment year), the function returned the old positive FCF, incorrectly suggesting the company still generates positive FCF. Sorting descending ensures the most recent year is checked first.

### Why `golden_cross` default `None` → `False`?

`golden_cross` (50-day MA > 200-day MA) was `None` when one or both MAs were unavailable (e.g. IPO < 200 trading days old). `None` propagated into numeric comparisons in the quant graph — `None < 0` is a TypeError in a strict context, and LangGraph's `add` reducer can't sum `None`. Defaulting to `False` makes it a safe boolean: no golden cross if we can't compute it.

## LLM Priority Queue (v1.37)

### Why a priority queue instead of more concurrent LLM slots?

The system runs four agents against a single local LM Studio instance with limited GPU memory. When RAGAS eval scoring fires (up to 6 metrics per agent response, each calling the LLM), eval can consume all available LLM slots and starve production inference — a user's quant summary or CrewAI kickoff sits behind eval metrics in the queue. Simply increasing `LLM_MAX_CONCURRENT` (default 2) makes the LM Studio model server the bottleneck instead, degrading latency for everyone.

A priority semaphore solves this without increasing concurrency: production calls (`CRITICAL`) jump ahead of eval calls (`LOW`), and warmup/ping calls (`NORMAL`) sit between them. When all slots are full, the highest-priority waiter is served next.

### Why a heap-based implementation instead of multiple semaphores?

Three separate semaphores (one per priority) would require deciding ahead of time how many slots each priority gets. A fixed split wastes capacity: if no CRITICAL calls are active, the CRITICAL slots sit idle while LOW callers wait. A `heapq` with `(priority, sequence, future)` lets any priority use any slot — when no high-priority work exists, low-priority work fills the gap. The heap ensures O(log n) priority ordering at every arrival.

### Why FIFO tie-breaking within the same priority?

Within the same priority level, callers should be served in order of arrival — first-come, first-served. The monotonically increasing `_seq` counter breaks ties fairly. Without FIFO, a late-arriving CRITICAL caller could starve an earlier CRITICAL caller under heavy load (though in practice CRITICAL contention is low — typically one production call at a time).

### Why does `_leave()` hand off to the next waiter instead of releasing immediately?

In a naive semaphore, releasing a slot increments `_active -= 1`, and a new waiter acquires it — but the waiter must go through the full `acquire()` path. In `_leave()`, the slot is handed off directly: pop the highest-priority waiter from the heap, set its future's result, and return. The `_active` count never drops — the slot is effectively transferred. This prevents a window where a new CRITICAL caller arriving between `_leave()` decrement and the next waiter's acquire sees `_active < _max` and enters behind the just-woken waiter, even though the woken waiter hasn't started its LLM call yet. The handoff preserves priority ordering under all arrival patterns.

### Why `LLM_MAX_CONCURRENT=2` as the default?

LM Studio on consumer GPUs (8-16GB VRAM) can comfortably run 2 concurrent LLM requests on a 14-30B model before OOM or significant slowdown. 1 concurrent slot would leave the model server underused during data-fetch phases (prices, fundamentals, MCP calls don't hit the LLM). 3+ concurrent slots on a 16GB GPU cause VRAM contention and individual request slowdown — throughput doesn't increase but tail latency degrades. 2 is the empirically tested sweet spot for the target hardware.

### Key properties

- ✓ Production inference never starved — CRITICAL always served before LOW
- ✓ No fixed partitioning — idle CRITICAL slots used by LOW/NORMAL work
- ✓ O(log n) scheduling — heap-based priority queue, not polling
- ✓ Priority-preserving handoff — slot handed directly to next waiter, no race window
- ✓ Process-local — no IPC, no external dependencies, no serialization
- ✓ Configurable — `LLM_MAX_CONCURRENT` env var tunes for different hardware

## Infrastructure Hardening (v1.36)

### Why `_ALLOWED_TABLES` whitelist in `prune_old_records`?

The prune function iterates a hardcoded table list and runs `DELETE FROM {table} WHERE created_at < ?`. Before v1.36, this was a fully dynamic SQL string with no guard against injection — if a table name somehow came from user input or an env var, it could inject SQL. The whitelist `_ALLOWED_TABLES: frozenset[str]` constrains iteration to known-safe table names. This is defence-in-depth: the table names are still hardcoded, but the whitelist makes it impossible for a future refactor to accidentally pass a user-controlled table name into the f-string.

### Why `_init_started` guard in `SemanticCache`?

SemanticCache is lazily initialized — Chroma client + embedding model are created on first `_ensure_ready()` call. If two concurrent requests hit `_ensure_ready()` simultaneously (or a re-entrant call occurs during the `SentenceTransformer` download), the Chroma/SentenceTransformer init runs twice. This can cause:
- Two Chroma clients pointing to the same on-disk DB (locking errors)
- Two SentenceTransformer models in memory (RAM waste)
- A crash if the first init is mid-download when the second one starts

The `_init_started` boolean gate ensures `_ensure_ready()` is idempotent even under concurrent access. The first thread sets `_init_started = True` and runs init; subsequent threads (and re-entrant calls) see the flag and return immediately.

### Why separate `_ADK_SESSION_DB` from `finsight_memory.db` in API routes?

The API routes (`/api/sessions`, `/api/sessions/{id}/events`) query ADK's session database — which stores conversation turns, tool calls, and event history. This is separate from `finsight_memory.db`, which stores custom tables (`ticker_briefs`, `recommendation_records`, `memory_entries`). Using `_ADK_SESSION_DB = DB_PATH.parent / "adk_sessions.db"` makes the path explicit and prevents accidental cross-contamination if the memory DB schema changes.

### Why proper `finally` blocks for connection cleanup (api_routes)?

Before v1.36, some API routes omitted `try/finally` around `aiosqlite.connect()`. If the handler raised an exception mid-request, the database connection was leaked — no `await db.close()`. Over time, this exhausted the SQLite connection limit (default 5 concurrent writes on Windows, ~unlimited reads but with performance degradation). All routes now use:

```python
db = None
try:
    db = await aiosqlite.connect(...)
    ...
finally:
    if db is not None:
        await db.close()
```

This guarantees cleanup even on exception paths. The `db = None` initialization ensures the `finally` block doesn't reference a `NameError` if the connection assignment fails.

## Deferred Eval Gate (v1.38)

### Why defer sub-agent evals instead of running them immediately?

Sub-agent eval LLM calls (RAGAS scoring for RAG, Quant, Market Context) fired via `asyncio.create_task()` the moment each sub-agent produced a response. All three sub-agents share the same LM Studio instance with the orchestrator. When all three sub-agents complete roughly simultaneously (they run in parallel), three eval storms hit LM Studio right when the orchestrator needs GPU for final answer synthesis. The process-local `LLMPriorityQueue` (v1.37) coordinates LLM calls within a single process, but cannot coordinate across three separate processes — each agent runs in its own uvicorn worker.

### Why a deferred queue instead of reducing eval concurrency?

Reducing eval concurrency (e.g. `LLM_MAX_CONCURRENT=1`) would slow eval throughput but not solve the temporal clash: sub-agent evals peak exactly when orchestrator synthesis needs the LLM. The deferred gate shifts all sub-agent evals to *after* the orchestrator finishes its synthesis, so the LM Studio is used for production inference first and eval scoring second. The 120s safety-net ensures evals eventually run even if the orchestrator never signals.

### Why HTTP POST instead of an in-process signal?

The orchestrator and sub-agents are separate processes (separate uvicorn instances on different ports). Shared memory, asyncio queues, or signals don't work across process boundaries. HTTP POST is the simplest cross-process coordination mechanism — no message broker, no shared filesystem, no new dependencies. A 5-second timeout prevents slow endpoint problems: if a sub-agent isn't responding to `/release-evals`, the orchestrator moves on.

### Why a 120s safety-net timeout?

Sub-agent analyses take 20-60s to complete. The orchestrator synthesis adds 10-30s on top. 120s covers the worst-case pipeline (60s analysis + 30s synthesis = 90s) with 30s margin. If the orchestrator crashes after dispatching sub-agents but before calling `/release-evals`, the safety-net releases evals after 120s — late but not lost.

### Key properties

- ✅ Cross-process eval coordination — single HTTP POST replaces uncoordinated asyncio.create_task storm
- ✅ 120s safety-net — evals always run even if orchestrator fails
- ✅ Zero new infrastructure — HTTP is the only coupling
- ✅ Non-blocking — orchestrator fires release as fire-and-forget, doesn't wait for responses
- ✅ Backward compatible — eval still runs, just deferred

## Confidence Regex — "Confidence Score: X" Format (v1.38)

### Why update the regex to match "Confidence Score"?

The LLM (ministral/qwen) sometimes outputs "Confidence Score: 0.75" (with the word "Score" capitalized) instead of "confidence: 0.75" or "75% confidence". The original `_CONF_PATTERN` used `(?:confidence|conf)[:\s]*(\d+\.?\d*)...` which only matched "confidence:" and "conf:" prefixes. Adding `(?:\s+score)?` makes the "Score" suffix optional — both old and new formats match with the same capture group.

The A2A executor path (`_store_memory` in `agent_executor.py`) previously hardcoded `confidence=0.5` for both `store_minimal()` and `record_recommendation()`. This meant briefs saved via the A2A path always recorded 50% confidence regardless of the actual LLM response. The fix extracts confidence from `response_text` using the same regex, producing accurate confidence values in the A2A path.

### Key properties

- ✅ Matches "Confidence Score: X", "confidence: X", and "X% confidence" uniformly
- ✅ A2A executor path extracts real confidence instead of hardcoding 0.5
- ✅ Consistent extraction logic between ADK-web and A2A paths

## AG-UI Bridge — Null-Stripping + Auto-Save (v1.38)

### Why recursive `_clean()` instead of flat `_strip_message_nulls()`?

CopilotKit Cloud injects `encryptedValue: null` at arbitrary nesting depths inside the AG-UI event payload — not just at the `input.messages[*]` level. The old flat approach only handled specific paths (`input`, `input.messages[*].name`, etc.). When CopilotKit added `encryptedValue: null` at a new depth, the event arrived with null values that Zod rejected.

The recursive `_clean()` traverses the entire event dict and strips any key whose value is `None` and whose key is in `_STRIP_KEYS`. This handles any future nesting depth changes without code updates.

### Why add `"input"` to `_STRIP_KEYS` now?

Previously, `input` was excluded from null-stripping because it carried user-entered data where null could be semantically meaningful for JSON Patch state management. But CopilotKit Cloud included `encryptedValue: null` inside the input payload, and fixing it at the `input.messages[*]` level was fragile. Adding `"input"` to `_STRIP_KEYS` is safe because `input` is an optional metadata field in the AG-UI event envelope — it describes the request, it's not the payload's core data. The `snapshot`, `delta`, and `content` keys remain excluded from stripping because they carry the actual state data.

### Why auto-save briefs in the bridge path?

The bridge (`POST /a2a-agui`) was the only orchestrator entry point that didn't auto-save briefs. The `after_agent_callback` path (ADK web UI) and the `FinSightAgentExecutor` path (direct A2A) both saved briefs, but the bridge streamed AG-UI events directly without persisting to `TickerMemory`. This meant repeat queries via CopilotKit always missed the same-day cache — the brief was generated fresh every time.

The bridge's `_auto_save_brief()` function uses the same extraction logic as the other two paths: regex for recommendation and confidence from the synthesis text, `store_minimal()` for persistence, `PerformanceTracker.record_recommendation()` for tracking. It also checks for an existing today-brief — if found and the stored text is shorter, `update_response_text()` overwrites with the longer synthesis text, ensuring the best version is cached.

### Why not share the save logic between all three paths?

The three paths have different contexts available:
- **ADK callback** (`_persist_memory_callback`): Has `session.events` and `callback_context`
- **A2A executor** (`_store_memory`): Has `session` and direct `TickerMemory` access
- **Bridge** (`_auto_save_brief`): Has `user_text`, `response_text`, `session_id`, `user_id`

Each path extracts the brief from different sources, making a shared function awkward without passing large context objects. The duplication (~30 lines per path) is acceptable for clarity — each path's save logic is self-contained and easy to understand in context.

### Key properties

- ✅ Recursive null-stripping handles arbitrary nesting depths
- ✅ Bridge path now saves briefs — same-day cache works for CopilotKit queries too
- ✅ `update_response_text()` overwrites with longer text — best version cached
- ✅ Self-contained per-path save logic — clear, no shared context coupling

## Logging — Silent `except` Blocks (v1.40)

### Why replace bare `except: pass` with `logger.warning(exc_info=True)`?

Eleven functions across sandbox, report_generator, memory/store, ticker_memory, performance_tracker, api_routes, and trace_context had bare `except: pass` or `except Exception: return None` blocks. These hide bugs permanently: a silent empty return looks identical in production to a correct no-data result. The only way to diagnose a silent failure is to add logging retroactively — by which point context is gone. Replacing with `logger.warning(msg, exc_info=True)` costs nothing when everything works and surfaces the full traceback when it doesn't.

### Key properties

- ✅ All 11 previously-silent exceptions now appear in service log files with full stack traces
- ✅ Default level is `WARNING` — noisy in development, actionable in production
- ✅ Return values preserved (still return `None`/default where appropriate)

## Logging — Third-party Logger Suppression (v1.40)

### Why suppress `httpx`, `chromadb`, `langfuse`, etc. to `WARNING`?

Third-party libraries default to `DEBUG` or `INFO`, filling log files with HTTP request traces, ChromaDB collection scans, and Langfuse event acknowledgements on every query. A typical FinSight request generates 50–200 third-party log lines for every 5–10 application lines. The signal-to-noise ratio makes logs unusable for debugging. Setting these to `WARNING` inside `setup_file_logging()` applies suppression once, globally, without requiring every call site to manage it. Per-library overrides (`LOG_LEVEL_HTTPX=DEBUG`) restore full verbosity when needed without code changes.

### Key properties

- ✅ Suppression applied in `setup_file_logging()` — all services inherit it automatically
- ✅ `LOG_LEVEL_<LIB>` env vars restore per-library verbosity without code changes
- ✅ Application logger still uses service-level `LOG_LEVEL` (default `INFO`)

## Agent — Custom `load_memory` Wrapper (v1.40)

### Why replace the ADK built-in `load_memory` with a custom wrapper?

The ADK built-in `load_memory` returns a `LoadMemoryResponse` Pydantic model. When the AG-UI bridge serializes tool results for the CopilotKit event stream, `json.dumps()` throws `TypeError` on any non-primitive type. The custom async wrapper calls `tool_context.search_memory()` and extracts text parts, returning a plain `str`. The LLM sees the same tool interface; the bridge sees a serializable value.

### Key properties

- ✅ Same tool signature as ADK built-in — no LLM prompt changes needed
- ✅ Returns `"\n---\n".join(parts)` or `"No relevant memories found."` — always a plain `str`
- ✅ Exception fallback returns `f"Memory search unavailable: {e}"` rather than crashing the bridge

## Agent — `send_message`-First Instruction (v1.40)

### Why strengthen the instruction to call `send_message` before any other tool?

Smaller local models sometimes call `load_memory` as their first action on a stock analysis request, spending a full LLM turn retrieving memory before dispatching to sub-agents. The system prompt previously said "call send_message for every agent" but didn't prohibit calling other tools first. Adding "your VERY FIRST ACTION must be send_message" and "do NOT call load_memory when the user asks to analyze a stock" eliminates the ambiguity that causes the model to hedge with a memory lookup before analysis begins.

### Key properties

- ✅ All `send_message` calls emitted in a single turn (parallel execution)
- ✅ `load_memory` gated to explicit user requests about history ("what did you recommend before?")
- ✅ `generate_report` removed from LLM tool list — prevents premature invocation before analysis

## Report Generation — Shared DeckData Extraction (v1.39)

### Why a shared `_extract_deck_data()` instead of per-format extraction?

Each output format (PPTX, DOCX, HTML) previously had its own copy of metric extraction, scorecard building, and executive summary logic. Adding the HTML format would have meant duplicating the extraction code a third time. `_extract_deck_data()` returns a `DeckData` dataclass (ticker, metrics, scorecards, recommendation, executive sections) consumed by all three generators. The per-format functions became thin wrappers around a shared pipeline — adding a new format only requires writing the rendering layer.

### Why module-level metric pattern dicts?

Metric definitions (value, label, thresholds, source) were scattered across `_extract_metrics()`, scorecard helpers, and format-specific code. Consolidating into module-level dicts (`_METRIC_DISPLAY_CONFIG`, `_SCORECARD_METRICS`, `_ADVANCED_SCORECARD_METRICS`) centralizes the canonical metric list in one place. Each format iterates the same dicts — consistency is guaranteed by construction.

### Key properties

- ✅ Single `_extract_deck_data()` sourced by all three formats — PPTX/DOCX/HTML
- ✅ `DeckData` dataclass provides typed access (no dict key typos)
- ✅ Metric patterns defined once at module level — cross-format consistency
- ✅ Adding a new output format only requires building the renderer

## Report Generation — Scorecard RSI Classification (v1.39)

### Why map RSI to qualitative labels instead of raw values?

Quantitative RSI values (0-100) are meaningful to traders but abstract in a narrative report. Classifying into Overbought (≥ 70, expensive), Bullish (55-70, bullish), Neutral (45-55, moderate), and Oversold (≤ 30, strong) transforms raw numbers into actionable signals. The `_rsi_status()` helper is used consistently across all three format scorecards.

### Key properties

- ✅ Overbought → expensive, Bullish → bullish, Neutral → moderate, Oversold → strong
- ✅ Same classification used in PPTX/DOCX/HTML scorecards
- ✅ Consistent with common technical analysis interpretation (70/30 overbought/oversold thresholds)

## Report Generation — Jinja2 HTML Engine (v1.39)

### Why Jinja2 instead of f-strings or Mako?

HTML templates grow beyond what string formatting handles safely. Jinja2 provides autoescaping (XSS prevention), template inheritance (base layout + content blocks), and `FileSystemLoader` (templates live in `src/shared/templates/` separate from code). The environment is lazy-loaded — first call to `generate_html()` creates it once via `_get_jinja_env()` — so the import cost is paid exactly once per process lifetime.

### Why a web component for slide rendering?

`deck-stage.js` is a vanilla JS custom element that renders a multi-slide deck with keyboard navigation (`ArrowLeft`/`ArrowRight`) and a single active `<section>` at a time. A pure-HTML approach would require either server-side slide splitting or CSS scroll-based pagination. The web component is framework-agnostic, bundle-free (no npm dependency), and works with any backend that emits the correct `<template>` structure.

### Key properties

- ✅ Jinja2 with `FileSystemLoader` — templates in `src/shared/templates/`, not inline strings
- ✅ Lazy-loaded environment — single creation per process
- ✅ Autoescaping on by default — XSS protection for ticker/company names in reports
- ✅ `deck-stage.js` web component — keyboard-navigable slides, zero dependencies
- ✅ Template supports inheritance — `investment_deck.html` defines slide blocks, `base.html` provides layout

## Report Generation — Modular Slide Functions (v1.39)

### Why break `generate_pptx()` into 9 standalone functions?

The original `generate_pptx()` was a single 800+ line function with slides built inline. Every slide mixed data extraction, positioning logic, and style application — adding a new metric or reformatting a slide required reading the entire function. The refactored version delegates each slide to a dedicated function (`_add_title_slide()`, `_add_executive_summary()`, `_add_scorecard()`, etc.) collected under `_SlidesHelper` (a namespace class holding shared state: `prs`, `deck_data`, `style`). Each slide function is independently testable and readable in isolation.

### Why `_SlidesHelper` as a namespace class instead of passing parameters?

Nine slide functions would each need the same 3-5 parameters (`prs`, `deck_data`, `style`, `charts`). A namespace class avoids parameter explosion, makes it easy to add shared helpers (`_add_table()`, `_add_bullet_frame()`, `_row_colors()`), and keeps the slide functions focused on their specific layout. The class is instantiated once at the top of `generate_pptx()` and passed to each slide builder.

### Key properties

- ✅ 9 standalone slide functions (title, exec summary, scorecard, metrics, advanced, analysis, fundamentals, holdings, disclaimer)
- ✅ `_SlidesHelper` namespace reduces per-function parameter count from 5+ to 0 (shared via `self`)
- ✅ Each function independently testable
- ✅ Original 800+ line function reduced to ~90 lines of orchestration

## Report Generation — API Route Ordering Fix (v1.39)

### Why does `/ticker/{symbol}/latest/{format}` need to precede `/{brief_id}/{format}`?

Starlette (FastAPI) matches routes top-down. `/{brief_id}/{format}` matches any two-segment path, including `/ticker/{symbol}/latest/{format}` — but `brief_id` would capture `"ticker"` and `format` would capture `"{symbol}"`. Defining the specific route before the parameterized route ensures the ticker endpoint is matched first. The bug only manifests when both routes are registered; the fix is simply reordering the route definitions.

### Key properties

- ✅ `/ticker/{symbol}/latest/{format}` defined before `/{brief_id}/{format}` in route list
- ✅ No Starlette route priority or regex needed — just declaration order

## Report Generation — Append-Only Executive Summary (v1.39)

### Why build executive summary sections by appending to a list?

The executive summary has three sections: Price Target, Thesis Statement, Investment Recommendation. Each format renders them differently (PPTX: 3 text boxes on one slide; DOCX: 3 paragraphs; HTML: 3 `<div>` blocks). Building the sections as a plain list of dicts (`{"heading": ..., "body": ...}`) in `_extract_deck_data()` allows each renderer to iterate and format independently. Adding a fourth section (e.g., Risk Summary) requires appending to the list in one place rather than modifying three format-specific rendering blocks.

### Key properties

- ✅ Executive sections as a list — iterated by all three format renderers
- ✅ Adding a new section = one list append in `_extract_deck_data()`
- ✅ PPTX: 3 text boxes on one slide; DOCX: 3 paragraphs; HTML: 3 `<div>` blocks
- ✅ No format-specific section ordering logic

## Agent Output Capture for Structured Brief Storage (v2.2)

### Why capture sub-agent responses at the `send_message` level?

The extraction pipeline (`src/shared/reports/extraction.py`) previously parsed all report data from the LLM's synthesis prose — regex-extracting metrics, scorecards, and peer comparisons from natural language. This was fragile: the LLM might format "Sharpe ratio of 1.45" as "Sharpe: 1.45", "Sharpe ratio = 1.45", or "the Sharpe ratio stands at 1.45". Each variant needed a separate regex.

Capturing the structured data at the `send_message` tool level — where the sub-agent's response arrives as a parsed dict — gives the extraction pipeline direct access to the raw metrics without regex guessing. The `extra_data` parameter on `store_minimal()` persists these structured responses alongside the synthesis text in the brief record.

### Why `extra_data` on `store_minimal()` instead of a separate table?

Adding a column to `ticker_briefs` would require a schema migration and alter the existing data model. An `extra_data` JSON column is a minimal change: it stores any additional context the orchestrator wants to preserve, without constraining the schema. Future features can add new keys to `extra_data` without database migrations.

### Key properties

- ✅ Structured data available for extraction without regex parsing
- ✅ Falls back to prose extraction when `extra_data` is unavailable (backward compatible)
- ✅ `extra_data` is schema-free — future features add keys without migrations
- ✅ `_populate_from_agent_outputs()` routes structured data to the correct DeckData fields

## Playwright-Based Report Export (v2.2)

### Why Playwright for HTML → PPTX/PDF instead of python-pptx alone?

python-pptx builds slides programmatically — each shape, text box, and chart is positioned via code. This produces correct PPTX files but requires maintaining a parallel rendering engine alongside the HTML template. When the HTML template changes (new slide layout, CSS update), the python-pptx renderer must be updated manually to match.

Playwright renders the HTML template in a headless Chromium browser and screenshots each slide. The PPTX output is a pixel-perfect representation of the HTML template — no manual synchronization needed. The trade-off is a Playwright dependency (~300MB Chromium download) and slightly larger PPTX files (raster images vs vector shapes).

### Why try Playwright first with python-pptx fallback?

Playwright may not be installed in all environments (CI, Docker without Chromium, Windows without Playwright deps). The `generate_pptx()` function tries Playwright first — if it raises `ImportError` or returns empty bytes, it falls back to the python-pptx generator. This gives best-quality output when Playwright is available and functional output everywhere else.

### Why `asyncio.new_event_loop()` on Windows?

Playwright's async API uses `asyncio.subprocess` internally, which requires the ProactorEventLoop on Windows. But FinSight's main event loop uses `WindowsSelectorEventLoopPolicy` (set in `src/shared/bootstrap.py`) to avoid `ConnectionResetError` noise from A2A connections. Creating a temporary event loop with the default Proactor policy for Playwright avoids the incompatibility without changing the main loop policy.

### Key properties

- ✅ HTML → PPTX via screenshots — no manual python-pptx slide synchronization
- ✅ HTML → PDF via print-mode — native PDF from browser rendering
- ✅ Graceful fallback — works without Playwright installed
- ✅ Windows-compatible — temporary ProactorEventLoop for Playwright subprocess

## Ticker Extraction — Pronouns in Stop Words (v2.2)

### Why add English pronouns to `_FINANCIAL_STOP_WORDS`?

The pronoun "I" (single uppercase letter) matched pattern 5 (`\b([A-Z]{1,2})\b`) and was extracted as a ticker. Briefs were stored under ticker "I" instead of the actual target (e.g., NVDA). The user query "I want to invest in NVDA" extracted "I" first because it appeared before "NVDA" in the text.

Adding common 1-2 letter English words (I, AM, AN, IF, IT, IS, IN, AT, ON, TO, NO, SO, OR, etc.) to `_FINANCIAL_STOP_WORDS` prevents these false positives. Explicit patterns like `$I` or `(I)` still work — the stop-word filter only applies to bare-word matches.

### Key properties

- ✅ "I" no longer extracted as a ticker — briefs stored under correct symbol
- ✅ Explicit patterns (`$I`, `(I)`) still work for rare cases
- ✅ Covers all common 1-2 letter English words, not just "I"

## Ticker Extraction — Holdings False Positives (v2.2)

### Why tighten `_HOLDINGS_PATTERNS` with word boundaries?

Pattern 4 (`(?:currently\s+)?(?:own|hold|have)\s*:?\s*...`) was too broad. The word "have" followed by any 1-5 letter word matched as a holding: "everything" → "EVERY", "ERSHI" from "have ERSHI". The pattern lacked `\b` word boundaries, so partial-word matches occurred.

Adding `\b` boundaries and requiring "I" or "we" subject on the broadest pattern prevents false-positive phantom tickers. Extracted holdings are also filtered against stop words and noise words as a final defense.

### Key properties

- ✅ Word boundaries prevent partial-word false positives
- ✅ Subject requirement ("I" or "we") on broadest pattern reduces noise
- ✅ Stop-word filtering catches remaining edge cases

## Stable Anonymous User ID (v2.2)

### Why generate a cookie-based anonymous ID?

The frontend previously sent no `X-FinSight-User-Id` header, so the backend generated a new `anon-{uuid}` on every request. Cache lookups (`_get_today_cached_text`, `_build_memory_context`) filter by `user_id` — with a different UUID each time, they always missed, and agents re-ran on every question even for the same stock in the same session.

A stable anonymous ID set as a `finsight_user_id` cookie (1-year TTL) ensures subsequent requests send the same ID. Cache lookups now find existing briefs, and the same-day cache path short-circuits the LLM for repeat queries.

### Key properties

- ✅ Same-day cache works for anonymous users — no more redundant agent runs
- ✅ 1-year cookie TTL — persists across browser sessions
- ✅ No server-side state — the cookie is the single source of user identity

## AG-UI Bridge Per-Event Timeout (v2.2)

### Why add a dynamic timeout to `runner.run_async()`?

When the LLM model is removed from LM Studio (e.g., user switches models or shuts down the server), `runner.run_async()` hangs indefinitely waiting for an LLM response. The frontend shows "processing" while sub-agents correctly timeout and show errors. The user has no indication that the orchestrator is stuck.

A dynamic per-event timeout distinguishes between two phases:
- **LLM response phase** (120s): The initial call or final synthesis. 120s covers the slowest local model inference.
- **Tool/sub-agent phase** (`A2A_TIMEOUT+30s`): Sub-agent calls already have their own timeouts. The extra 30s margin accounts for orchestration overhead.

If the timeout fires, a `RunError` event is sent to the frontend with a clear message about model unavailability.

### Key properties

- ✅ No indefinite hangs — frontend gets an error instead of infinite "processing"
- ✅ Phase-aware timeout — different limits for LLM vs sub-agent execution
- ✅ Clear error message — user knows the model is unavailable

## `src/` Directory Layout Refactor

### Why move everything under `src/`?

Before the refactor, packages lived at repo root: `orchestrator/`, `quant/`, `financial_rag/`, `market_context/`, `mcp_tools/`, `shared/`, `web/`, `tests/`, `scripts/`. This flat layout caused three problems:

1. **Import ambiguity**: `pytest` ran from root, so imports like `from shared.settings import ...` worked. But Docker containers had different `WORKDIR` setups — some ran from repo root, others from a subdirectory. The same import could resolve differently between CI, local dev, and production.

2. **PyPI packaging impossible**: A flat-layout project can't be published to PyPI without moving files to `src/` (or a nested package). While publishing isn't an immediate goal, the flat layout prevented standard tooling (`build`, `pip install -e .` with consistent paths).

3. **Discovery confusion**: New contributors had to read the Makefile to understand which directories were Python packages vs config vs docs vs frontend. The flat root mixed Python modules, Node.js frontend files, Docker configs, documentation, and CI configs.

### Solution

Move all runnable code under `src/`:

```
src/
  orchestrator/       # ADK orchestrator
  financial_rag/      # RAG agent (LlamaIndex)
  quant/              # Quant agent (LangGraph)
  market_context/     # Market Context agent (CrewAI)
  mcp_tools/          # MCP server
  shared/             # Shared infrastructure
  web/                # Next.js frontend
  tests/              # All test files
  scripts/            # Utility scripts
```

Configuration changes:
- `pyproject.toml`: `packages.find = { where = ["src"] }` — setuptools scans `src/` for packages. `pythonpath = ["src"]` so tests import from `src/` directly. `testpaths = ["src/tests"]`.
- `Makefile` + Dockerfiles: All `COPY` and working directory paths prefixed with `src/`.
- CI workflow: `mypy src/shared src/orchestrator`, `pytest src/tests`, etc.
- Batch files: Next.js `cd src/web`, ADK web `cd src/orchestrator`.
- Path traversals in 6 files fixed (`logging.py`, `task_store.py`, `agent_registry.py`, `openapi.py`, etc.).

### Why not a monorepo with namespaced packages?

A monorepo (`@finsight/orchestrator`, `@finsight/quant`, etc.) would be more conventional for a multi-package project but adds build complexity (each package needs its own `pyproject.toml`, CI matrix, version). The `src/` layout is a single-package structure that provides clean import paths without the monorepo overhead. All 5 agent packages continue to share a single `pyproject.toml`, dependency set, and version.

### Key properties

- ✅ All Python imports go through `src/` — consistent resolution across local dev, CI, Docker
- ✅ Standard PyPI-compatible layout — `pip install -e .` works without path hacks
- ✅ Clear root — config files, docs, and `pyproject.toml` are visually distinct from code
- ✅ Only 6 files needed path traversal fixes — minimal migration cost
- ✅ All 218 unit tests pass without changes — test imports unaffected by `pythonpath = ["src"]`

## HTML Documentation Format (v1.17)

### Why convert from Markdown to HTML?

Commit 638a7ef converted all documentation from Markdown to HTML with a custom CSS design system (ivory background, serif headings, clay accent). Three motivations:

1. **Readability**: Markdown tables, nested lists, and code blocks become hard to scan in large documents. HTML with CSS provides visual hierarchy, section spacing, and consistent typography that Markdown renderers (especially terminal-based ones) cannot match.

2. **Cross-referencing**: Markdown files have no built-in cross-document navigation. The HTML version includes a shared navigation bar across all docs (`index.html`, `ARCHITECTURE.html`, `AGENTS.html`, etc.) with consistent styling. Adding a new document means adding one `<a>` tag to the nav bar rather than updating every markdown file's manual "back to main" links.

3. **Diagram embedding**: The project has 10+ Mermaid diagrams (`docs/diagrams/`). HTML renders Mermaid natively; Markdown editors have inconsistent Mermaid support. The HTML layout reserves full-width diagram containers that Markdown's linear flow cannot provide.

### Why a custom CSS design system instead of a framework?

Bootstrap, Tailwind, or Material CSS would add 50-200KB of unused styles for a documentation site with 10 pages and a single layout. The custom CSS (~30 lines in each file) covers exactly what's needed: body typography, table styling, code blocks, nav bar, and mobile breakpoint. Zero dependencies, zero build step, identical rendering across browsers.

### Why maintain both .md and .html?

The .md files serve as the *source of truth* — they're editable in any text editor, diffable in git, and processable by tools. The .html files are the *rendered output* — generated from the .md source via the `html-docs` skill's templates. Keeping both means the docs are readable in raw form (via the .md files in a terminal) and in rendered form (via the .html files in a browser). The `docs: sync` commits (e.g., 5cf9c8e, 6b9f8f1) update both in lockstep.

## Documentation Cleanup Policy

### Why delete outdated docs and scripts instead of keeping them?

Four cleanup commits (e1104ed, 0931410, b78db9f) deleted 14 debug scripts, 5 outdated .md files, 17 stale test files, and unused modules (`shared/types.py`, `shared/workflow.py`). The policy is:

**If it's not used, delete it.** Dead files impose a cognitive tax:
- New contributors grep the codebase and find irrelevant results
- Refactoring must consider whether a dead file has hidden callers
- Build tools (mypy, ruff, pytest) scan every file, wasting CI time on dead code

### Why not archive instead of delete?

Git history preserves deleted files. Anyone who needs a removed script or doc can `git log --diff-filter=D -- <file>` to find it, or `git show <commit>:<file>` to recover it. Archiving (moving to an `archive/` folder) keeps the files in the working tree where they continue to be scanned by tools, show up in grep results, and accumulate bit-rot. Delete is the honest option — the file is either maintained or gone.

### Why delete tests instead of fixing them?

The 17 deleted test files (commit b78db9f) referenced classes and functions from the v1.0-v1.15 architecture that no longer existed. Fixing them would require a full rewrite against the current codebase — essentially writing new tests from scratch while also removing old ones. Deleting the stale fixtures is more honest than maintaining dead test code that would confuse future readers. The 88+60 replacement tests (see "Testing Strategy" section) cover the current architecture properly.

## Services.py Placement for ADK Web (v1.14)

### Why put services.py at the orchestrator root instead of inside web/?

Commit 906104d originally moved `services.py` from `src/orchestrator/agents/finsight_agent/` to `src/orchestrator/agents/`. Commit d84a3cc later moved it to `src/orchestrator/services.py`. Root cause: ADK's `load_services_module()` looks for `services.py` in the `agents_dir` root, not in subdirectories. When ADK detects `web/agent.py` exists, it sets `agents_dir` to the *parent* of `web/` — so `services.py` must be at `orchestrator/`, not at `orchestrator/web/`. If the file is nested, the `finsight://` memory service URI scheme is never registered, and `create_memory_service_from_options()` falls back to `InMemoryMemoryService` — losing all persistent memory.

### Why not patch ADK's loader instead of moving the file?

The loader's path resolution is a framework detail that could change between ADK versions. Moving `services.py` to the expected location is a one-line structural change that works across ADK versions. Patching the loader would require a monkey-patch or fork — brittle and hard to maintain.

### Key properties

- ✅ `finsight://` URI scheme registered before any memory service is created
- ✅ Persistent SQLiteMemoryService replaces InMemoryMemoryService for `adk web` path
- ✅ ADK-version-independent — works with any version that calls `load_services_module()`
- ✅ Now at `src/orchestrator/services.py` — exact directory ADK expects when `web/agent.py` exists

## Callable Instruction Provider for Dynamic Agent Discovery (v2.4)

### Why make `root_agent.instruction` a callable?

Before d84a3cc, `root_agent.instruction` was set to `_build_instruction()` — a static string computed once at module load time. Under `adk web`, the lifespan handler in `main.py` never fires (ADK manages its own server lifecycle), so agent discovery happened *after* the instruction was built. The instruction would forever list an empty agent list.

Making `root_agent.instruction = _instruction_provider` (a callable) means ADK invokes it on every turn. The callable calls `_build_instruction()`, which reads the current state of `_client.list_agents()`. Newly discovered agents dynamically appear in the system prompt without any module reload.

### Why not re-discover agents on every turn?

The `_discovery_done` flag in `web/agent.py` ensures `SubAgentClient.discover()` runs exactly once — on the first `before_agent_callback` invocation. Subsequent turns reuse the cached agent list. Re-discovery per turn would add ~3s latency (3 sub-agents × 1s HTTP timeout) to every user interaction with no benefit, since agent availability rarely changes within a session.

## RAG Filing Source: `get_company_filings` → `get_financial_filings` (v2.4)

### Why change the MCP tool for filing ingestion?

`get_company_filings` returns all form types (10-K, 10-Q, 8-K) in a single flat `filings[]` array. For large filers like Apple or Microsoft, the SEC EDGAR database returns tens of 8-K filings for every 10-K or 10-Q. With `limit=5`, the parameter is consumed entirely by 8-Ks, and annual/quarterly reports are never ingested.

`get_financial_filings` separates annual from quarterly reports (`annual_limit=3, quarterly_limit=4`), guaranteeing 3 10-Ks and 4 10-Qs per ingestion run regardless of how many 8-Ks exist. The response format is `{annual: [...], quarterly: [...]}` — both arrays are concatenated for the ingestion pipeline.

**Effect**: The RAG agent now reliably ingests substantive financial filings (10-K, 10-Q) instead of drowning in 8-K press releases.

## Frontend Auth: Middleware → AuthProvider Redirect (v2.4)

### Why replace Next.js middleware with AuthProvider?

Next.js 16 deprecated `middleware.ts` in favor of proxy-based auth. The old middleware checked for `finsight_session` cookie and redirected to `/login`. Under Next.js 16, middleware still runs but emits deprecation warnings and may break in future versions.

The auth guard moved into `AuthProvider` (React context) using a `useEffect` that:
1. Redirects to `/login?redirect=<path>` when no authenticated user is detected
2. Redirects away from `/login` when a session exists (prevents login-page loop)
3. Reads session state from cookies — no server-side middleware needed

`middleware.ts` was deleted entirely. No behavior change — same redirect logic, just in the client component instead of edge middleware.

## Public API Endpoints for Frontend Discovery (v2.4)

### Why make `/api/agents` and `/api/reports` public?

The Next.js frontend fetches `/api/agents` to populate the operator page's agent list, and `/api/reports` for authenticated download links. Both fetches go through Next.js rewrites that don't carry auth headers. Adding JWT headers to Next.js rewrites would require significant proxy infrastructure (custom rewrite handlers, token injection middleware).

Making these two endpoints public (with appropriate path constraints — no write operations exposed) is simpler and equally secure: both endpoints only expose information the user can already see from the frontend. The orchestrator's `/a2a` and all sub-agent endpoints remain auth-protected.

## Explanatory Comments Across All Source Files (v1.24)

### Why add comments to ~40 files in a single pass?

Commit 2b8b1d3 added explanatory comments to every production source file (~40 files). The motivation was **bus-factor proofing**: the sole developer had deep context on every module's non-obvious design decisions (why a particular LangGraph pattern was used, why a timeout value was chosen, why an MCP client was a singleton). Without comments, this context would be lost if the developer was unavailable.

### Why not rely on the DESIGN_DECISIONS doc alone?

DESIGN_DECISIONS.md documents architectural-level decisions (why IST timezone, why RAGAS, why four frameworks). But many decisions are *local* — why a specific `asyncio.Lock` pattern in one function, why a `run_in_executor` call around a specific yfinance wrapper. A design doc with 3000+ lines would bury these local decisions. Inline comments put the reasoning at the point of use, where a future reader needs it.

### What comment style was used?

Each source file got a module-level docstring describing its responsibility and key design choices. Within functions, only non-obvious logic received inline comments — straightforward code (`for item in items: process(item)`) was left uncommented. The principle: **explain why, not what**. The code already says what it does; comments say why it does it that way.

## Secrets Externalization (v1.25)

### Why move hardcoded secrets to env vars?

Commit 2a9ba36 moved `SEC_USER_AGENT` (hardcoded user-agent string for SEC EDGAR HTTP requests) and `LLM_API_KEY` (hardcoded `"lmstudio"` API key) from source files to env vars read via `shared.settings`. Three motivations:

1. **Audit trail**: Hardcoded strings in source files are invisible to secret scanners. An env var is a single lookup point — `grep LLM_API_KEY .env` is trivially auditable.

2. **Deployment flexibility**: Different environments use different SEC user agents (dev: `"FinSight/1.0"`, staging: `"FinSight/1.0-staging"`, prod: `"FinSight/1.0-prod"`). An env var changes the agent without a code deploy.

3. **Consistency**: `LLM_API_KEY` was already consumed from settings in most files, but some files still hardcoded `api_key="lmstudio"` directly. Centralizing all key reads through `settings.llm_api_key` ensures one source of truth.

### Why default values still in code?

`LLM_API_KEY` defaults to `"lmstudio"` in `settings.py` because LM Studio doesn't validate the API key — it accepts any non-empty string. `SEC_USER_AGENT` defaults to `"FinSight/1.0"` with `contact@finsight.example.com` as the email. Both defaults work out of the box for local development while being overridable for production. The defaults are clearly documented in `.env.example`.

## Model Evolution Timeline

### Why document the full timeline?

The model selection history is scattered across multiple non-chronological sections ("Model Selection (Ollama Era)", "Migration from Ollama to LM Studio", "Model Change: gpt-oss-20b → qwen", "Model Change: qwen3-30b-A3B → Ministral-3-14b"). A consolidated timeline shows the reasoning at each step and prevents repeating failed experiments:

| Date | Model | Provider | Outcome | Reason for Change |
|---|---|---|---|---|
| v0.1 | `qwen3.5:0.8b` | Ollama | Tested, too small | Replaced by ministral-3:3b |
| v0.2 | `ministral-3:3b` | Ollama | Tested, unreliable tool calling | Replaced by llama3.2 |
| v0.3 | `llama3.2` (3B) | Ollama | ❌ Tool calling unreliable | Failed via both ollama/openai providers |
| v0.4 | `qwen2.5:7b` | Ollama (LiteLLM) | ✅ Reliable tool calling, ~4.7GB, 20-40s/call | Worked but Ollama inference too slow |
| v1.0 | `gpt-oss-20b` | LM Studio | ✅ Good quality, 40-60s/call | Better inference speed than Ollama |
| v1.17 | `qwen3-30b-a3b-2507` | LM Studio | ✅ 5-10x faster (5-10s/call), parallel function calling | Major latency improvement, maintained quality |
| v1.36 | `ministral-3-14b-reasoning` | LM Studio | ⚠️ Faster (3-5s/call) but unreliable save_brief | Per-developer .env override, not code default |
| v1.36+ | `qwen3-30b-a3b-2507` (default) | LM Studio | ✅ Most tested, reliable tool calling | Code default; local overrides via .env |

**Key takeaway**: The project tried 7+ model/provider combinations. Each switch was driven by a specific failure mode (speed, tool-calling reliability, parallel function support). The current combination (LM Studio + qwen3-30b-a3b as default, configurable via .env) gives the best balance of speed and reliability.

---

## Quant Fan-In Data-Readiness Guard (v2.5)

### Problem

The 5-way fan-in at `format_output` causes LangGraph to fire `format_output` (and consequently `llm_summary`) multiple times as predecessors complete in different supersteps. With 5 predecessor branches (fetch_prices, fetch_fundamentals, options_flow, insider_signals, analyst_positioning), LangGraph may schedule `format_output` after each branch completes rather than waiting for all 5. This results in 4 sequential LLM calls (~78s) instead of 1.

### Solution

Data-readiness guard in `llm_summary_node` (`src/quant/nodes/summary.py`): Before firing the LLM call, check that all predecessor branches have written their data by verifying that `metrics`, `reasoning`, `recommendation`, and `fundamentals` are all non-empty in state. If any are missing, return `{}` silently — the next invocation (when more predecessors have completed) will have the full data.

### Why not fix this at the LangGraph topology level?

LangGraph's scheduling of fan-in nodes is an implementation detail that depends on checkpoint ordering and runtime concurrency. The diamond dependency fix (v1.33) reduced but did not eliminate the issue — with 5 predecessors, the probability of partial completion triggering duplicate `format_output` invocations is non-trivial. The data-readiness guard is a defensive check that works regardless of LangGraph's scheduling behavior.

### Key properties

- ✅ Zero wasted LLM calls — guard skips invocation when data is incomplete
- ✅ Silent skip — returns `{}`, no error logging for expected partial firings
- ✅ Backward compatible — full-data invocations proceed normally
- ✅ Also removed duplicate LangChainInstrumentor auto-instrumentation from quant agent

---

## Scrollable HTML Report Replacing Slide Deck (v2.5)

### Problem

The HTML report template used `<deck-stage>` custom element with JavaScript-based slide navigation (one slide at a time, keyboard arrows). PDF export rendered this as a static print of a single slide — not a multi-page document. Users wanted a scrollable HTML page for reading and a properly paginated A4 PDF for sharing.

### Solution

Replace the deck-stage slide presentation with a full scrollable HTML page in `investment_deck.html`. The template now renders all sections vertically with CSS `break-inside-avoid` for PDF print. Playwright renders the same HTML as A4 portrait with print-optimized CSS (cover page, section breaks, conclusion back page). PPTX and DOCX download options removed from the frontend — HTML + PDF only.

### Why remove PPTX/DOCX?

PPTX required either Playwright screenshot-based rendering (fragile, slow) or python-pptx (limited formatting). DOCX had similar limitations. The scrollable HTML page with PDF export covers both use cases: interactive reading (HTML) and print-friendly sharing (PDF). The extraction pipeline now focuses on two formats instead of four.

### Key properties

- ✅ Scrollable HTML — responsive, self-contained, zero JavaScript navigation
- ✅ A4 PDF — properly paginated with cover page and section breaks
- ✅ Executive summary limit raised from 1200 to 4000 chars for scrollable format
- ✅ Frontend simplified — two download buttons instead of four

---

## Case-Insensitive Username Matching (v2.5)

### Problem

Usernames were stored and compared case-sensitively. A user who created account "admin" could not log in with "Admin" or "ADMIN". While technically correct, this is poor UX — users rarely remember exact casing of their usernames.

### Solution

Normalize usernames to lowercase at creation (`seed_user.py`) and lookup (`user_store.py`). The `get_user()` and `create_user()` functions now call `username.lower()` before any database operation. Login accepts any casing — "Admin" matches "admin".

### Why lowercase instead of case-insensitive comparison?

Lowercase normalization at the boundary (creation + lookup) is simpler and more robust than case-insensitive comparison at every query site. A single `username.lower()` at the entry points guarantees all downstream code works with canonical lowercase form.

### Key properties

- ✅ Login accepts any casing — better UX, no security impact
- ✅ Single normalization point — creation + lookup, not scattered across queries
- ✅ Test isolation fix — `_schema_v4_ensured` flag reset between tests

---

## AG-UI Bridge Eval Hook (v2.5)

### Problem

The AG-UI bridge (`src/orchestrator/agui_bridge.py`) is the primary endpoint for the CopilotKit frontend. When the bridge's `_stream` method processed a query, it never called `score_response()` or `_release_sub_agent_evals()`. Runtime RAGAS evaluation only ran through the A2A executor and ADK Web paths — the most common user-facing path (CopilotKit) silently skipped all quality scoring.

### Solution

Add eval hook to `_stream`: after synthesis completes, fire `score_response()` and `_release_sub_agent_evals()` as background tasks, matching the A2A executor pattern. Also add `_truncate_at_sentence()` for sentence-aware truncation of executive summary and market narrative fields — prevents mid-word cuts in report extraction.

### Key properties

- ✅ All three execution paths now fire eval — A2A, ADK Web, and AG-UI bridge
- ✅ Sentence-aware truncation — cleaner report fields
- ✅ Debug logging added to Market Context crew output parsing

---

## Pydantic Agent Output Models (v2.5)

### Problem

Agent outputs were extracted using ~220 lines of fragile `.get()` chains and regex patterns. This caused: zeroed KPI chips (fundamentals fallback returned `{}` instead of `None`), raw JSON narrative from CrewAI (`output_pydantic` not enforced), and DCF key mismatch (`intrinsic_value` vs `fair_value` in different agent outputs).

### Solution

Typed Pydantic models at every agent boundary in `src/shared/agent_models.py`: `QuantAgentOutput`, `MarketContextOutput`, `RAGAgentOutput`. Each agent validates its output through its model before returning. The extraction pipeline uses `model_validate(mode="json")` with fallback to legacy dict extraction for old briefs without agent output metadata.

### Key properties

- ✅ Type-safe output extraction — no more `.get("key", {}).get("subkey", [])`
- ✅ Fixes three classes of extraction bugs in one change
- ✅ Backward compatible — validated path falls back to legacy for old briefs
- ✅ CrewAI `output_pydantic` enforced — structured JSON from LLM, not prose

---

## Why Shared SQLite Agent Output Store (v2.7)

### Problem

The orchestrator sent agent outputs to the reviewer as inline JSON payloads inside `send_message`. For full responses (RAG narrative + quant metrics + market context narrative), the payload grew beyond 50 KB — bloating A2A messages and increasing LLM context overhead. Worse, agent outputs were ephemeral — if the reviewer crashed or the session was retried, outputs had to be recomputed.

### Solution

Shared SQLite table `agent_output_store` keyed by `(session_id, agent_name)`. The orchestrator's `send_message` callback calls `store_agent_output()` to persist the full structured output before returning. The reviewer receives only `session_id` and `ticker` — it fetches all outputs via `get_agent_outputs(session_id)` and runs its verification tools.

### Why SQLite instead of Redis / in-memory?

SQLite is already the project's persistence layer (memory store, sessions, filings index). Adding Redis would introduce a new dependency with connection management overhead. In-memory dicts don't survive restarts and can't be shared across processes. SQLite provides cross-process persistence with zero infrastructure cost.

### Key properties

- ✅ Cross-process persistence — orchestrator and reviewer share data without per-process coupling
- ✅ Race-safe — reviewer reads from SQLite, orchestrator writes to SQLite, no mutex needed (SQLite WAL handles concurrency)
- ✅ Eliminates inline payload bloat — LLM context stays lean
- ✅ TTL-pruned at startup — stale outputs older than 600s are auto-expired by `prune_stale_outputs()`
- ✅ Schema v6 — single migration adds the table alongside existing memory tables

---

## Why Reviewer Synthesis-Only Architecture (v2.7)

### Problem

The original reviewer (v2.6) used OpenAI Agents SDK `Runner.run()` to call 4 tools as native SDK tool calls. This meant the LLM received the full agent output payloads in the system prompt, paid the overhead of parsing/tokenizing them, and made 4 separate runner steps — each requiring an LLM round-trip. The SDK runner also introduced a 20-second overhead just for handshake and context setup.

### Solution

The reviewer calls 4 deterministic Python functions directly: `check_contradictions()`, `verify_sources()`, `score_confidence()`, `validate_recommendation()`. These run in milliseconds — no LLM round-trips. The outputs (contradiction report, source verification, confidence score, validation result) are compiled into a structured verdict dict. Only the final synthesis step uses the LLM, receiving compressed agent summaries (not full payloads) to produce a `cross_validation_verdict` string.

### Why not keep the SDK Runner?

The Runner is designed for interactive multi-step agents — it maintains conversation state, supports handoffs, and streams partial tokens. None of those features are needed here. The reviewer is a batch verification pipeline: collect data, run deterministic checks, summarize with one LLM call. Direct Python calls are simpler, faster (4ms vs 20s+), and have no SDK versioning risks.

### Key properties

- ✅ Tools run in Python — 4 deterministic checks in ~4 ms total, zero LLM cost
- ✅ LLM sees only agent summaries — reduced context, faster synthesis
- ✅ No SDK dependency — avoids OpenAI Agents SDK version lock-in
- ✅ Session ID access — fetches full outputs from shared store only when needed

---

## Why Chart.js from CDN (v2.7)

### Problem

The HTML investment deck needed interactive charts (forecast distribution, Monte Carlo percentiles, trend overlays, stress scenario bars, DCF breakdown). Generating chart images server-side would require a headless browser (Playwright/selenium) — adding ~300 MB to the Docker image and significant complexity. Static SVG generation would produce non-interactive charts that could not show tooltips, zoom, or dynamic toggles.

### Solution

Embed Chart.js 4.4.4 from CDN in the Jinja2 HTML template. The extraction pipeline pre-computes chart-ready data series (labels, arrays, colors) and serializes them as JSON directly into `<script>` blocks. Each chart is a ~10-line JavaScript instantiation. No build step, no bundler, no server-side rendering.

### Why not bundle Chart.js locally?

The HTML report is a self-contained file served by the orchestrator. Bundling Chart.js into the Next.js frontend would require a separate npm dependency, a build step for the report route, and would not work for PDF export or API-based report retrieval. CDN delivery keeps the report self-contained: open the HTML file offline and charts still render.

### Key properties

- ✅ Zero-build integration — no bundler, no npm, no server-side chart rendering
- ✅ Interactive charts — tooltips, zoom, legend toggle all work natively
- ✅ Template grew from 768→1609 lines — all chart init is inline JavaScript
- ✅ 10+ new report sections — technicals table, signals, forecast chart, Monte Carlo summary, stress scenarios, DCF breakdown, stats table, anomaly alerts, trend data, cross-validation
- ✅ 14 new `DeckData` fields — fully typed in Pydantic model

---

## Why NEXT_PUBLIC_AUTH_ENABLED (v2.7)

### Problem

The existing `AUTH_ENABLED` env var controlled backend authentication (orchestrator middleware, A2A service tokens, MCP auth). But the frontend (Next.js) had its own auth checks in `AuthContext.tsx` and `middleware.ts` — these ran independently of the backend flag. Setting `AUTH_ENABLED=false` did not disable frontend auth redirects, so developers hit login screens even in auth-disabled mode.

### Solution

Add `NEXT_PUBLIC_AUTH_ENABLED=false` as a separate frontend-only env var. When false, `AuthContext.tsx` short-circuits all auth checks (no token validation, no login redirect, no HTTP calls to auth endpoints). The frontend behaves as if always authenticated — perfect for local development. The backend `AUTH_ENABLED` remains independently togglable for production deployments that may want frontend auth but backend-only service auth.

### Why two separate env vars instead of one?

Single env var usage: backend reads it at startup, Next.js reads it at build time. With one var, changing auth behavior requires rebuilding the Next.js frontend. Two vars allow independent control: disable frontend auth during local dev (`NEXT_PUBLIC_AUTH_ENABLED=false`) while keeping backend auth on for integration testing (`AUTH_ENABLED=true`), without a rebuild.

### Key properties

- ✅ Frontend auth bypass — zero HTTP calls, zero redirects when disabled
- ✅ Independent from backend `AUTH_ENABLED` — any combination works
- ✅ No rebuild needed — Next.js public env vars are baked at build time but can differ per deployment
- ✅ Default commented out in `.env.example` — opt-in for dev convenience
