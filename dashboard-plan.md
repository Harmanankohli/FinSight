# Dashboard Implementation Plan

Replace the `/trace` page with an observability dashboard showing error rates,
latency, token usage, TFFT, and RAGAS quality metrics.

**Tech stack**: Next.js 16 App Router, React 19, TypeScript, Recharts 3.8,
Zustand, existing design system (globals.css CSS variables).

**Data source**: Langfuse public API (already proxied via `/api/traces/route.ts`).

---

## ⚠ Cross-Cutting Concerns (read before any phase)

### Do NOT touch the research page AG-UI event rail

`app/research/page.tsx` lines 332–400 contain a "Trace rail" (`<div className="trail">`)
that displays AG-UI streaming events during a research run. Despite the name,
it is **completely independent** of the `/trace` page — it renders CopilotKit
messages, not Langfuse traces. It imports nothing from the trace page, uses
none of the trace API, and must remain untouched.

The CSS classes `.trail`, `.trail-head`, `.trail-body`, `.ev`, `.ev-h`, `.ev-tag`,
`.ev-name`, `.ev-ms`, `.ev-args` in `globals.css` are used exclusively by this
research rail. **Do not remove them.**

### TFFT will likely be null

`completionStartTime` is not emitted by any of the current backend instrumentors
(confirmed: zero hits in codebase for `completionStartTime`, `completion_start_time`,
`timeToFirstToken`, `time_to_first`). The Langfuse API supports the field, but
the OpenInference/LlamaIndex/CrewAI/PydanticAI instrumentors in
`src/shared/observability.py` don't populate it.

The dashboard must degrade gracefully: show "—" and "Not available — requires
streaming instrumentation" when TFFT is null. Do not render a broken or
misleading 0ms card.

### Shared CSS classes are shared

The trace page uses `.card`, `.pill`, `.mono`, `.topbar`, `.scroll` — all shared
across every page. None of them should be removed from `globals.css`.

### Agent name mismatch: "market" vs "market_context"

`classifyAgent()` returns `"market"` for the Market Context Agent, but
`runtime_eval.py` pushes RAGAS scores under the agent name `"market_context"`
(line 911: `_run_metrics(pairs, "market_context", trace_id)`).

If both sections use their own naming independently, the agent breakdown table
would show "market" while the RAGAS quality panel would show "market_context"
for the same agent. Fix by adding a normalization map in either:
- `lib/agentColors.ts`: add `SCORE_AGENT_TO_KEY` map: `{ market_context: "market" }`
- Or in the scores API route: normalize parsed agent names before aggregation

All other agents match: orchestrator, rag, quant, analytics, reviewer.

---

## Phase 0 — Extract Shared Langfuse Helper

### 0.1 Create `lib/langfuse.ts`

**File**: `src/web/nextjs-app/lib/langfuse.ts`

Extract the `langfetch()` helper from `app/api/traces/route.ts` into a shared
module so both the existing traces route and new dashboard routes can import it
without duplicating the auth setup:

```typescript
const LF_BASE = process.env.LANGFUSE_BASE_URL || "https://jp.cloud.langfuse.com";
const LF_PK = process.env.LANGFUSE_PUBLIC_KEY || "";
const LF_SK = process.env.LANGFUSE_SECRET_KEY || "";
const AUTH = Buffer.from(`${LF_PK}:${LF_SK}`).toString("base64");

export function langfuseConfigured(): boolean {
  return !!(LF_PK && LF_SK);
}

export async function langfetch(path: string): Promise<any> {
  const r = await fetch(`${LF_BASE}${path}`, {
    headers: { Authorization: `Basic ${AUTH}` },
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error(`Langfuse ${r.status}: ${await r.text()}`);
  return r.json();
}
```

Then update `app/api/traces/route.ts` to `import { langfetch, langfuseConfigured }
from "@/lib/langfuse"` and remove its local copies.

### 0.2 Create `lib/agentColors.ts`

**File**: `src/web/nextjs-app/lib/agentColors.ts`

Extract the agent classification and color-mapping logic from `app/trace/page.tsx`
(the `color()` and `bg()` functions, lines 28–48) into a shared module. Both the
dashboard page and the dashboard API route need to classify names to agents.

```typescript
export type AgentKey = "orchestrator" | "rag" | "quant" | "market" | "analytics" | "reviewer" | "mcp";

export function classifyAgent(name: string): AgentKey {
  const n = (name || "").toLowerCase();
  if (n.includes("rag") || n.includes("llamaindex") || n.includes("filing")) return "rag";
  if (n.includes("quant") || n.includes("langgraph") || n.includes("stress") || n.includes("dcf")) return "quant";
  if (n.includes("market") || n.includes("crewai") || n.includes("peer") || n.includes("macro")) return "market";
  if (n.includes("analytics") || n.includes("pydanticai") || n.includes("trend") || n.includes("forecast")) return "analytics";
  if (n.includes("reviewer") || n.includes("cross-valid")) return "reviewer";
  if (n.includes("mcp") || n.includes("get_price") || n.includes("get_financial") || n.includes("get_news")) return "mcp";
  return "orchestrator";
}

export const AGENT_COLOR: Record<AgentKey, string> = {
  orchestrator: "var(--clay)",
  rag: "var(--rag)",
  quant: "var(--quant)",
  market: "var(--market)",
  analytics: "var(--analytics)",
  reviewer: "var(--reviewer)",
  mcp: "var(--mcp)",
};

export const AGENT_BG: Record<AgentKey, string> = {
  orchestrator: "var(--orch-bg)",
  rag: "var(--rag-bg)",
  quant: "var(--quant-bg)",
  market: "var(--market-bg)",
  analytics: "var(--analytics-bg)",
  reviewer: "var(--reviewer-bg)",
  mcp: "var(--mcp-bg)",
};

// Normalize score agent names (from runtime_eval.py) to AgentKey.
// Needed because runtime_eval pushes "market_context" but classifyAgent returns "market".
export const SCORE_AGENT_TO_KEY: Record<string, AgentKey> = {
  market_context: "market",
  // All others match directly: orchestrator, rag, quant, analytics, reviewer
};

export function normalizeScoreAgent(scoreAgent: string): AgentKey {
  return SCORE_AGENT_TO_KEY[scoreAgent] ?? (scoreAgent as AgentKey);
}
```

---

## Phase 1 — Backend API Layer

New Next.js API routes that aggregate Langfuse data into dashboard-ready shapes.
Both routes import `langfetch` from `@/lib/langfuse` (created in Phase 0).

### 1.1 Create `/api/dashboard/route.ts`

**File**: `src/web/nextjs-app/app/api/dashboard/route.ts`

**Langfuse endpoints to call**:
- `GET /api/public/traces?limit=200&fromTimestamp={iso}` — recent traces
- `GET /api/public/observations?limit=500&type=GENERATION&fromTimestamp={iso}` — LLM spans

`fromTimestamp` is computed as: `new Date(Date.now() - hours * 3600000).toISOString()`

**Important: observation → trace join**: Observations include a `traceId` field.
To compute per-agent token usage and error rates, the route must:
1. Fetch traces → build a `Map<traceId, agentKey>` using `classifyAgent(trace.name)`
2. Fetch observations → for each observation, look up its parent trace's agent
3. Aggregate tokens, errors, and TFFT by the trace's agent, not the observation's name

If the Langfuse API does not support `?type=GENERATION` as a query filter,
fetch all observations and filter client-side: `obs.filter(o => o.type === "GENERATION")`.

**Meaningful-trace filter** (see E14): Before aggregating, apply the same filter
from the existing `app/api/traces/route.ts` lines 42–53 — skip auto-instrumented
micro-spans (`"ChatOpenAI"`, `"Embedding"`, `"get_prices"`) that would inflate
orchestrator counts. Only count traces matching orchestrator/agent name patterns
or with latency > 100ms.

**Sparse bucket generation** (see E6): Generate all time buckets from
`fromTimestamp` to now (stepping by 1 hour if ≤48h, else 1 day). For each
bucket, aggregate matching traces. Emit empty buckets with all fields zeroed
so Recharts draws a continuous line without visual gaps.

**Pagination**: For time windows > 24h, Langfuse may return fewer than the full
set at `limit=200`. Check `meta.totalPages` in the response; if > 1, fetch
subsequent pages using `page=2`, `page=3`, etc. Cap at 5 pages (1000 traces)
to avoid slow responses.

**Response shape** (`GET /api/dashboard?hours=24`):

```typescript
interface DashboardData {
  hours: number;      // echo back the time window for throughput calculations
  truncated: boolean; // true if more data exists than was fetched (see E13)

  kpi: {
    totalTraces: number;
    errorRate: number;          // % of traces containing ≥1 ERROR-level observation
    errorCount: number;         // absolute count for subtitle
    avgLatency: number;         // ms
    p50Latency: number;         // ms
    p95Latency: number;         // ms
    p99Latency: number;         // ms
    totalTokens: number;        // sum of all observation usage.totalTokens
    promptTokens: number;       // sum of usage.promptTokens
    completionTokens: number;   // sum of usage.completionTokens
    avgTfft: number | null;     // ms — null if completionStartTime unavailable
  };

  agentBreakdown: Array<{
    agent: string;              // AgentKey from lib/agentColors.ts
    traceCount: number;
    avgLatency: number;
    errorCount: number;
    totalTokens: number;
    promptTokens: number;
    completionTokens: number;
  }>;

  // Bucketed by hour (≤48h) or day (>48h)
  timeSeries: Array<{
    bucket: string;             // ISO timestamp of bucket start
    traceCount: number;
    avgLatency: number;
    errorCount: number;
    totalTokens: number;
    promptTokens: number;       // needed for stacked area chart (Phase 4.2)
    completionTokens: number;
  }>;
}
```

**Error detection**: An observation with `level === "ERROR"` marks its parent trace
as errored. Count each errored trace once (not once per ERROR observation).

**TFFT**: For each GENERATION observation, if both `startTime` and
`completionStartTime` are present, compute `completionStartTime - startTime` in ms.
Average across all GENERATION observations that have both fields. If zero
observations have `completionStartTime`, return `null`.

### 1.2 Create `/api/dashboard/scores/route.ts`

**File**: `src/web/nextjs-app/app/api/dashboard/scores/route.ts`

**Langfuse endpoint**:
- `GET /api/public/scores?limit=500` — RAGAS scores pushed by `runtime_eval.py`

Score names follow the pattern `ragas/{agent}/{metric}`. The full list of metrics
pushed by `src/shared/runtime_eval.py` (verified from source):

| Agent | Metrics |
|-------|---------|
| orchestrator | AnswerRelevancy, citation_quality, risk_disclosure, recommendation_clarity, response_completeness, no_forward_guarantees |
| rag | Faithfulness, ContextPrecisionWithoutReference, news_coverage, cross_collection_synthesis |
| quant | FactualCorrectness, signal_explanation_quality |
| market_context | Faithfulness, macro_regime_analysis, peer_landscape_analysis |
| analytics | FactualCorrectness, trend_forecast_consistency, anomaly_disclosure |
| reviewer | FactualCorrectness, contradiction_detection_quality, confidence_calibration_quality |

**Parse score names**: Split on `/` — `ragas/orchestrator/citation_quality` →
agent=`orchestrator`, metric=`citation_quality`. Then normalize the agent name
via `normalizeScoreAgent()` from `lib/agentColors.ts` to align with dashboard
agent keys (e.g., `"market_context"` → `"market"`). Skip scores that don't
have exactly 3 segments or don't start with `"ragas/"` (see edge case E8).

**Scale awareness**: RAGAS native metrics (AnswerRelevancy, Faithfulness,
FactualCorrectness, ContextPrecisionWithoutReference) return 0.0–1.0.
DomainSpecificRubrics return 1–5 integer scale. The API should include a
`scale` field per metric so the frontend can render bars correctly.

**Response shape**:

```typescript
interface ScoresData {
  byAgent: Record<string, {
    metrics: Record<string, {
      avg: number;
      min: number;
      max: number;
      count: number;
      recent: number;
      scale: "0-1" | "1-5";    // so frontend knows bar width denominator
    }>;
    overallAvg: number;
  }>;
  recentScores: Array<{
    name: string;               // full "ragas/agent/metric" name
    value: number;
    traceId: string;
    timestamp: string;
  }>;
}
```

**Scale detection**: Use a hardcoded set of known 0–1 metrics (see edge case E9):
```typescript
const ZERO_ONE_METRICS = new Set([
  "AnswerRelevancy", "Faithfulness", "FactualCorrectness",
  "ContextPrecisionWithoutReference"
]);
```
All other metrics are DomainSpecificRubrics on the 1–5 integer scale.

---

## Phase 2 — Dashboard Page Shell

### 2.1 Create the dashboard page

**File**: `src/web/nextjs-app/app/dashboard/page.tsx`

Page structure:

```
<Suspense fallback={<topbar skeleton>}>
  <DashboardContent />
</Suspense>
```

`DashboardContent` component:
- On mount, fetch `/api/dashboard?hours={hours}` and `/api/dashboard/scores`
  in parallel via `Promise.all`
- Track `hours` in local state (default 24), re-fetch both when changed
- Show loading skeleton (`.pulse` class) while fetching
- Layout: topbar → scroll container → max-width 1120px centered content

**Topbar**: Title "Dashboard", subtitle "Observability metrics · agent performance · quality scores".
Right side: time range selector — three `<button>` pills (24h / 7d / 30d) where
the active one gets `background: var(--clay); color: #fff`.

**Content sections** (top to bottom):
1. KPI cards row (Phase 3)
2. Charts 2×2 grid (Phase 4)
3. Agent breakdown table (Phase 4)
4. RAGAS quality panel (Phase 5)

### 2.2 Update sidebar navigation

**File**: `src/web/nextjs-app/components/Sidebar.tsx`

**Changes** (all within this one file):

1. In the `NAV` array (line 9), replace:
   ```typescript
   { href: "/trace", label: "Trace", icon: "M4 4v16M4 8h7M4 14h11M4 20h5" }
   ```
   with:
   ```typescript
   { href: "/dashboard", label: "Dashboard", icon: "M3 3v18h18M7 14v3M11 10v7M15 7v10M19 4v13" }
   ```
   (bar-chart SVG path)

2. Remove the `LfTrace` interface (line 17–19)
3. Remove the `traces` state: `const [traces, setTraces] = useState<LfTrace[]>([]);` (line 25)
4. Remove the `fetch("/api/traces")` call and its `.then()` chain (lines 32–34)
5. Remove the entire "Recent traces" JSX block (lines 61–79):
   ```tsx
   {traces.length > 0 && ( ... )}
   ```
6. Keep `recent` state and "Recent queries" section completely unchanged
7. Remove the `LfTrace` import if it was the only usage (it's locally defined, so just delete the interface)

### 2.3 Delete the old trace page

**Delete**: `src/web/nextjs-app/app/trace/page.tsx`

This is the only file in `app/trace/`. The `app/trace/` directory can be deleted entirely.

**Do NOT delete** `app/api/traces/route.ts` in this phase — the shared
`langfetch()` extraction (Phase 0.1) should land first, and Phase 6.4 handles
final deletion of the old API route after confirming zero consumers.

---

## Phase 3 — KPI Summary Cards

All cards live inside `dashboard/page.tsx`. Extract to `components/dashboard/KpiCards.tsx`
only if the file exceeds ~400 lines.

**Card grid**: CSS grid `grid-template-columns: repeat(5, 1fr)` with `gap: 14px`.
Each card uses the `.card` class with `padding: 18px 20px`.

### 3.1 Error rate card

- Large number: `kpi.errorRate` formatted as `X.X%`
- Label: "Error Rate" (11px uppercase, `var(--text-muted)`)
- Number color: green (`--quant`) if < 5%, gold (`--hold`) if 5–15%, red (`--sell`) if > 15%
- Subtitle: `{kpi.errorCount} errors in {kpi.totalTraces} traces`

### 3.2 Latency card

- Large number: `kpi.p50Latency` formatted via the `ms()` helper
  (copy from trace page: `v < 1000 ? Math.round(v) + "ms" : (v/1000).toFixed(1) + "s"`)
- Label: "P50 Latency"
- Subtitle: `p95 {ms(p95Latency)} · p99 {ms(p99Latency)}`

### 3.3 Token usage card

- Large number: `kpi.totalTokens` with K/M suffix
  (`n >= 1_000_000 ? (n/1e6).toFixed(1) + "M" : n >= 1000 ? (n/1e3).toFixed(1) + "K" : n`)
- Label: "Tokens Used"
- Color: `var(--analytics)`
- Subtitle: `{promptTokens} prompt · {completionTokens} completion` (also with K/M suffix)

### 3.4 TFFT card

- Large number: `kpi.avgTfft !== null ? ms(kpi.avgTfft) : "—"`
- Label: "Avg TFFT"
- Subtitle: if null → "Not available — requires streaming instrumentation";
  otherwise → "Time to First Token"
- Color: `var(--mcp)` when available, `var(--text-muted)` when null

### 3.5 Throughput card

- Large number: `(kpi.totalTraces / data.hours).toFixed(1)` traces/hr
- Label: "Throughput"
- Subtitle: `{kpi.totalTraces} traces in {data.hours}h`
- Color: `var(--clay)`

---

## Phase 4 — Charts

Import recharts with `"use client"` (the page is already client-side).
Recharts 3.x works with React 19. Import directly — no dynamic import needed
since the page is `"use client"`.

All charts render inside `.card` containers with padding `18px 22px` and a
section heading (`<h3>` in `font-size: 15px`).

### 4.1 Latency over time — line chart

**Data**: `timeSeries` array from `/api/dashboard`
**Components**: `LineChart`, `Line`, `XAxis`, `YAxis`, `Tooltip`, `ResponsiveContainer`
- `<ResponsiveContainer width="100%" height={240}>`
- X-axis: `bucket` formatted as `HH:mm` (≤48h) or `MM/DD` (>48h)
- Y-axis: latency in ms
- Line: `stroke="var(--clay)"`, `strokeWidth={2}`, `dot={false}`
- Tooltip: white background, 1px sand border, show `{bucket}: {value}ms`

### 4.2 Token usage over time — stacked area chart

**Data**: `timeSeries` — uses `promptTokens` and `completionTokens` per bucket
**Components**: `AreaChart`, `Area`, `XAxis`, `YAxis`, `Tooltip`, `ResponsiveContainer`
- Stacked: `<Area stackId="1">` for each series
- Prompt tokens: `fill="var(--analytics)"`, `stroke="var(--analytics)"`
- Completion tokens: `fill="var(--analytics-bg)"`, `stroke="var(--analytics)"`
- Y-axis: format with K/M suffix

### 4.3 Error rate over time — bar chart

**Data**: `timeSeries` — compute `(errorCount / traceCount * 100) || 0` per bucket
**Components**: `BarChart`, `Bar`, `XAxis`, `YAxis`, `ReferenceLine`, `Tooltip`, `ResponsiveContainer`
- Bar: `fill="var(--sell)"`
- `<ReferenceLine y={5} stroke="var(--hold)" strokeDasharray="3 3" label="5%" />`
- Y-axis: percentage

### 4.4 Agent latency breakdown — horizontal bar chart

**Data**: `agentBreakdown` array sorted by `avgLatency` descending
**Components**: `BarChart`, `Bar`, `XAxis`, `YAxis`, `Cell`, `Tooltip`, `ResponsiveContainer`
- `layout="vertical"`
- Color each `<Cell>` by `AGENT_COLOR[agent]` from `lib/agentColors.ts`
- Y-axis: agent names
- X-axis: latency in ms

**Chart grid layout**: `display: grid; grid-template-columns: 1fr 1fr; gap: 18px`.

### 4.5 Agent breakdown table

Below the charts. Styled as a `.card` with a table inside.

| Column | Content | Alignment |
|--------|---------|-----------|
| Agent | colored dot + name | left |
| Traces | `traceCount` | right, mono |
| Avg Latency | `ms(avgLatency)` | right, mono |
| Errors | `errorCount` (red if > 0) | right, mono |
| Tokens | `totalTokens` with K/M | right, mono |

Rows: one per agent from `agentBreakdown`, sorted by `traceCount` descending.
Row style: `padding: 10px 16px`, `border-bottom: 1px solid var(--sand)`.

---

## Phase 5 — RAGAS Quality Panel

### 5.1 Per-agent score cards

**Data**: `byAgent` from `/api/dashboard/scores`

Grid: `grid-template-columns: repeat(auto-fill, minmax(320px, 1fr))`.

For each agent with scores, render a `.card` containing:

1. **Header**: colored dot + agent name (bold) + `overallAvg` as large number
2. **Metric list**: for each metric in `metrics`:
   - Metric name (human-readable, replace `_` with spaces, title case)
   - Horizontal bar: width = `(avg / maxScale) * 100%` where `maxScale = scale === "0-1" ? 1 : 5`
   - Bar color by value:
     - `"0-1"` scale: < 0.4 → `--sell`, 0.4–0.7 → `--hold`, > 0.7 → `--quant`
     - `"1-5"` scale: < 2.0 → `--sell`, 2.0–3.5 → `--hold`, > 3.5 → `--quant`
   - Value label at end of bar (mono, 11px)
3. **Footer**: `"Based on {count} evaluations"` in muted text

### 5.2 Recent scores feed

Below agent cards. A `.card` with compact rows:

- Each row: metric name (sans `ragas/` prefix), value (colored by threshold),
  timestamp (relative: "2h ago"), all in 12px
- Rows separated by `border-bottom: 1px solid var(--sand)`
- Show last 15 scores from `recentScores`
- No trace ID links needed (trace page is gone) — show trace ID as a mono
  8-char truncated slug for identification only

---

## Phase 6 — Polish and Cleanup

### 6.1 Loading and error states

- While fetching: show `.card` containers with `.pulse` animated placeholders
  (grey rectangles matching the card content layout)
- API errors: inline error card with red text, same pattern as the former trace
  page error display (`color: var(--sell)`, `fontSize: 13`)
- Empty data (zero traces): show a centered message "No traces in the selected
  time range" with a link to the Research page

### 6.2 Responsive layout

Add a `<style>` block or media queries to the page:

```css
@media (max-width: 1024px) {
  /* KPI grid: 3 + 2 row */
  .kpi-grid { grid-template-columns: repeat(3, 1fr); }
  /* Charts: single column */
  .chart-grid { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
}
```

### 6.3 Auto-refresh

- Topbar toggle button: "Auto" pill that toggles a 30-second refresh interval
- Implementation: `useEffect` with `setInterval` that calls the fetch functions
- Use `AbortController` to cancel in-flight requests when the interval fires again
  or when the component unmounts
- Show "Updated Xs ago" next to the auto toggle, updating every second via
  a separate 1s `setInterval`

### 6.4 Dead code removal

**Files to delete**:

| File | Reason |
|------|--------|
| `app/trace/page.tsx` | Already deleted in Phase 2.3 |
| `app/api/traces/route.ts` | Zero consumers after Phase 2 (sidebar no longer fetches, trace page deleted). If `langfetch` was extracted to `lib/langfuse.ts` in Phase 0.1, this file only contains the `GET` handler with filtering logic — safe to delete. |
| `lib/traceFilter.ts` | 125-line orphaned file. Zero imports anywhere in the codebase (confirmed by grep). Defines `classifyTraceEntry`, `filterAuthTraces`, `countAuthDeniedByReason`, `countAuthDeniedByUser` — none are used. Delete entirely. |

**Files to edit**:

| File | Change |
|------|--------|
| `lib/stores/useAppStore.ts` | Remove `traceOpen: boolean`, `setTraceOpen: (v: boolean) => void` and their initializers. These have zero imports/consumers (confirmed by grep). The remaining store fields (`sidebarOpen`, `userId`) stay. |
| `README.md` | Update: (1) Replace `/trace` row in Pages table with `/dashboard` and new description. (2) Replace `/api/traces` row in API Routes table with `/api/dashboard` and `/api/dashboard/scores`. (3) Update `useAppStore.ts` description to remove `traceOpen`. (4) Update env var table: Langfuse vars are now "For `/api/dashboard`". (5) Update sidebar description: "Left nav — workspace links, recent queries" (remove "recent traces"). |
| `docs/API_REFERENCE.md` | Update `/api/traces` row to reference new `/api/dashboard` and `/api/dashboard/scores` routes. |
| `docs/API_REFERENCE.html` | Same update as `.md` counterpart — update the traces proxy row. |
| `docs/DESIGN_DECISIONS.md` | Section "Why server-side API proxies (`/api/traces`, ...)" — update to reference `/api/dashboard`. The design rationale (keys hidden from browser) still applies. |
| `docs/DESIGN_DECISIONS.html` | Same update as `.md` counterpart. |

### 6.5 Build verification

Run `npm run build` from `src/web/nextjs-app/`. Verify:
- No TypeScript errors from missing imports
- No broken page routes
- No unused export warnings (if ESLint rules are configured)

---

## File Change Summary

| Action | File | Phase |
|--------|------|-------|
| **Create** | `lib/langfuse.ts` | 0.1 |
| **Create** | `lib/agentColors.ts` | 0.2 |
| **Edit** | `app/api/traces/route.ts` (use shared langfetch) | 0.1 |
| **Create** | `app/api/dashboard/route.ts` | 1.1 |
| **Create** | `app/api/dashboard/scores/route.ts` | 1.2 |
| **Create** | `app/dashboard/page.tsx` | 2.1 |
| **Edit** | `components/Sidebar.tsx` | 2.2 |
| **Delete** | `app/trace/page.tsx` | 2.3 |
| **Edit** | `lib/stores/useAppStore.ts` | 6.4 |
| **Delete** | `lib/traceFilter.ts` | 6.4 |
| **Delete** | `app/api/traces/route.ts` | 6.4 |
| **Edit** | `README.md` | 6.4 |
| **Edit** | `docs/API_REFERENCE.md` | 6.4 |
| **Edit** | `docs/API_REFERENCE.html` | 6.4 |
| **Edit** | `docs/DESIGN_DECISIONS.md` | 6.4 |
| **Edit** | `docs/DESIGN_DECISIONS.html` | 6.4 |

Paths for `app/`, `components/`, `lib/` are relative to `src/web/nextjs-app/`.
Paths for `docs/`, `README.md` are relative to project root.

---

## Phase Dependency Graph

```
Phase 0 ─── Phase 1 ─── Phase 2 ─── Phase 3
                                 └── Phase 4
                                 └── Phase 5
                                          └── Phase 6
```

- **Phase 0** must complete first (shared helpers used by Phases 1–5)
- **Phase 1** must complete before Phases 3–5 (API endpoints they consume)
- **Phase 2** can start after Phase 0 (only needs nav change + page shell)
- **Phases 3, 4, 5** can run in parallel (independent sections of the dashboard page)
- **Phase 6** runs last (cleanup depends on all features being in place)

---

## Implementation Notes

- **No new dependencies** — recharts, zustand, and Next.js are already installed.
- **Langfuse API pagination** — the `/api/public/traces?limit=200` cap is sufficient
  for a 24h window. For 7d/30d, use `fromTimestamp` filtering and paginate via
  `page=N` (Langfuse returns `meta.totalPages`). Cap at 5 pages to avoid slow responses.
- **CSS** — all styling uses existing `globals.css` classes and CSS variables.
  No new CSS file needed. Use inline styles or a `<style>` block in the page
  component for dashboard-specific layout (KPI grid, chart grid, responsive breakpoints).
- **Design system** — ivory/sand/clay palette, serif headings, mono numbers,
  `.card` containers with sand borders. Match the operator page's visual weight.
- **`ms()` formatter** — copy from the deleted trace page into `dashboard/page.tsx`
  or into `lib/agentColors.ts` as a shared `formatMs()` export.

---

## Edge Cases

Every computation and rendering path must handle these. Agents implementing
each phase are responsible for guarding against the cases marked for their phase.

### E1 — Division by zero (Phase 1.1, Phase 4.3)

| Calculation | Zero guard |
|-------------|-----------|
| `errorRate = errorCount / totalTraces * 100` | If `totalTraces === 0`, return `0` (not `NaN`) |
| `throughput = totalTraces / hours` | If `hours === 0`, return `0`. Also validate `?hours` query param: clamp to `[1, 720]`, default to 24 for non-numeric/negative/zero values |
| `avgLatency` across traces | If zero traces, return `0` |
| `timeSeries[i].errorCount / traceCount * 100` (chart 4.3) | If bucket `traceCount === 0`, emit `0` not `NaN`. This is the most likely division-by-zero — sparse time ranges create empty buckets |
| `overallAvg` in scores | If agent has zero metrics, return `0` |

### E2 — Empty / missing token `usage` on observations (Phase 1.1)

Not all Langfuse observations carry a `usage` field. Only `GENERATION`-type
observations have token counts, and even those may return `null` for `usage`,
`usage.promptTokens`, or `usage.completionTokens` depending on the LLM provider
and instrumentor.

Guard: when summing tokens, treat any missing/null field as `0`:
```typescript
const prompt = obs.usage?.promptTokens ?? 0;
const completion = obs.usage?.completionTokens ?? 0;
const total = obs.usage?.totalTokens ?? (prompt + completion);
```

### E3 — Observations without matching trace (Phase 1.1)

The API fetches traces and observations as two separate paginated requests with
the same `fromTimestamp`. An observation may reference a `traceId` that isn't in
the fetched trace set (its parent trace was outside the 200-trace page or was
filtered out).

Guard: when looking up `traceIdToAgent.get(obs.traceId)`, if the trace isn't
found, either:
- **Skip** the observation (don't count its tokens/errors), or
- **Attribute to "unknown"** and exclude "unknown" from the agent breakdown

Do not default to "orchestrator" — that would inflate orchestrator numbers.

### E4 — Traces with null/undefined latency (Phase 1.1)

In-progress or failed traces may have `latency: null`. When computing percentiles
and averages, filter to `traces.filter(t => typeof t.latency === "number")` first.
If the filtered set is empty, return `0` for all latency KPIs.

### E5 — Percentile calculation on small datasets (Phase 1.1)

With fewer than ~5 traces, P95 and P99 are essentially the max value. The
implementation should use sorted-array indexing:
```typescript
function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.ceil(sorted.length * p / 100) - 1;
  return sorted[Math.max(0, idx)];
}
```
This is well-defined for any non-empty array. For an empty array, return `0`.

### E6 — Sparse time-series buckets (Phase 1.1, Phase 4)

For 7d/30d windows, many hourly (or daily) buckets may have zero traces. The
API should emit all buckets in the range (including empty ones with zeroed fields)
so the charts don't have misleading gaps. Recharts connects data points — a
missing bucket creates a visual discontinuity.

Generate buckets: iterate from `fromTimestamp` to now, stepping by bucket size
(1 hour or 1 day). For each bucket, aggregate matching traces. Empty buckets
get `{ traceCount: 0, avgLatency: 0, errorCount: 0, totalTokens: 0, promptTokens: 0, completionTokens: 0 }`.

### E7 — Recharts with ≤ 1 data point (Phase 4)

If the time range contains 0 or 1 data points, Recharts renders awkwardly
(no visible line, tooltip issues). Guard: if `timeSeries.length <= 1`, show a
centered message "Not enough data for charts" instead of rendering the chart grid.

### E8 — Non-RAGAS scores in Langfuse (Phase 1.2)

Langfuse may contain scores pushed by other tools or manual annotations that
don't follow the `ragas/{agent}/{metric}` pattern. The scores route must
filter: only process scores whose `name` starts with `"ragas/"` and has exactly
3 slash-separated segments. Skip anything else silently.

### E9 — Scale detection ambiguity (Phase 1.2)

The heuristic "max ≤ 1.0 → 0-1 scale, else 1-5 scale" is fragile. A rubric
metric that happens to only have scores of 1 would be misclassified as 0-1 scale.

Better approach: hardcode the known 0-1 metrics:
```typescript
const ZERO_ONE_METRICS = new Set([
  "AnswerRelevancy", "Faithfulness", "FactualCorrectness",
  "ContextPrecisionWithoutReference"
]);
const scale = ZERO_ONE_METRICS.has(metricName) ? "0-1" : "1-5";
```
This is deterministic and matches `runtime_eval.py` exactly.

### E10 — Race condition on time range change (Phase 2.1, Phase 6.3)

If the user clicks "7d" while a "24h" fetch is in flight, the stale response
could overwrite the fresh one. Guard: use an `AbortController` per fetch cycle.
When the time range changes or auto-refresh fires:
1. Abort any in-flight request
2. Create a new `AbortController`
3. Pass `{ signal: controller.signal }` to both `fetch()` calls
4. On abort, do not update state

### E11 — Recharts and CSS custom properties (Phase 4)

Recharts renders SVG in the DOM. CSS custom properties (`var(--clay)`) work in
inline SVG `fill`/`stroke` attributes in modern browsers because the browser's
CSS engine resolves them. **However**, Recharts may use Canvas rendering in some
configurations, where CSS variables would not resolve.

Guard: Recharts 3.x defaults to SVG. Do not pass `useCanvas={true}` or switch
to Canvas rendering. If an implementer encounters color issues, fall back to
raw hex values from the design system:

| Variable | Hex |
|----------|-----|
| `--clay` | `#8b6f4e` |
| `--rag` | `#2c4a7c` |
| `--quant` | `#2a6b2a` |
| `--market` | `#8b4513` |
| `--analytics` | `#1a7a7a` |
| `--reviewer` | `#9b2335` |
| `--mcp` | `#5a3e7c` |
| `--sell` | `#7a2c2c` |
| `--hold` | `#8b6f00` |

### E12 — Langfuse unconfigured or unreachable (Phase 1.1, Phase 1.2)

If Langfuse keys are missing, both API routes should return a clear error
(not crash). Use `langfuseConfigured()` from `lib/langfuse.ts`:
```typescript
if (!langfuseConfigured()) {
  return NextResponse.json({ error: "Langfuse keys not configured" }, { status: 500 });
}
```

If Langfuse is reachable but returns an error (rate limit, 500, timeout),
catch the error and return `{ error: "Failed to fetch from Langfuse: {message}" }`
with status `502`. The frontend should display this in the error card (Phase 6.1),
not crash with an unhandled promise rejection.

### E13 — Pagination cap undercount (Phase 1.1)

For 30d windows on a busy system, 1000 traces (5 pages × 200) may not cover
the full period. The KPIs would be computed from a subset, making error rate
and throughput inaccurate.

Guard: include a `truncated: boolean` field in the `DashboardData` response.
Set to `true` if `meta.totalPages > 5` (more data exists than was fetched).
The frontend should show a subtle warning: "Showing metrics from the most
recent 1000 traces" when `truncated === true`.

### E14 — `classifyAgent` catch-all inflating orchestrator (Phase 1.1)

Any trace whose name doesn't match a known agent keyword falls to
`return "orchestrator"`. Auto-instrumented OTEL spans (e.g., HTTP client spans,
DB queries) will inflate orchestrator counts.

Guard: in the dashboard API route, after fetching traces, apply the same
meaningful-trace filter from the existing `app/api/traces/route.ts` (lines 42–53):
skip traces named `"ChatOpenAI"`, `"Embedding"`, `"get_prices"`, etc. Only
aggregate traces that pass this filter.

### E15 — Timezone in time-series bucket labels (Phase 4)

The API computes bucket timestamps in UTC. The frontend renders them in the
browser's local timezone via `new Date(bucket).toLocaleTimeString(...)`. This
is correct behavior — but if the implementer formats with `bucket.slice(11, 16)`
(raw string manipulation on UTC ISO timestamps), the labels will show UTC time,
not local time.

Guard: always parse to `Date` first, then format:
```typescript
const label = new Date(bucket).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
```
