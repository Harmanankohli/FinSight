# Plan: Add Analytics & Reviewer Agent Tiles to Frontend

## Context

The system has 5 sub-agents but the frontend Research page only shows 3 agent tiles during processing (Financial RAG, Quant Analysis, Market Context). The **Analytics Agent** (port 8005, PydanticAI) and **Reviewer Agent** (port 8006, OpenAI Agents SDK) are missing from the UI. The backend AG-UI bridge (`src/orchestrator/agui_bridge.py:70-82`) already maps and emits these agents in `active_agents` state — only the frontend needs updating.

**Processing flow:** Phase 1 runs RAG + Quant + Market + Analytics in parallel, then Phase 2 runs Reviewer sequentially after Phase 1 completes.

**Backend `active_agents` lifecycle** (verified in `agui_bridge.py:435-531`):
- Agents are **appended** to the list when dispatched (never removed individually)
- The entire list is **cleared to `[]`** only after successful run completion (line 522-531)
- **On error/cancel** (lines 556-561): the `active_agents` clear is SKIPPED because it's inside the `try` block — the list stays stale
- During Phase 1: list contains 4 agents. During Phase 2: list contains all 5. After normal completion: empty.

---

## Files to Modify (5 files)

### 1. `src/web/nextjs-app/app/globals.css`

**1a. Add CSS custom properties** — insert after line 23 (`--market-bg: #fce8d9;`), before `--orch`:
```css
--analytics: #1a7a7a;  --analytics-bg: #ddf0f0;
--reviewer: #9b2335;   --reviewer-bg: #f4dde1;
```
Rationale: Teal for analytics (data/stats feel), crimson for reviewer (distinct from `--sell: #7a2c2c`).

**1b. Add tile color rules** — insert after line 164 (`.tile.market`):
```css
.tile.analytics { border-color: var(--analytics); background: var(--analytics-bg); }
.tile.reviewer  { border-color: var(--reviewer);  background: var(--reviewer-bg); }
```

---

### 2. `src/web/nextjs-app/app/research/page.tsx` (5 edits)

**2a. Expand `AGENTS` array** (lines 26-30) — add `phase` field to all entries + 2 new agents:
```typescript
const AGENTS = [
  { key: "rag", name: "Financial RAG", sub: "LlamaIndex · filings", color: "--rag", match: ["financial rag", "rag agent"], phase: 1 },
  { key: "quant", name: "Quant Analysis", sub: "LangGraph · metrics", color: "--quant", match: ["quant", "quant analysis"], phase: 1 },
  { key: "market", name: "Market Context", sub: "CrewAI · macro + peers", color: "--market", match: ["market context", "sentiment"], phase: 1 },
  { key: "analytics", name: "Analytics", sub: "PydanticAI · trends", color: "--analytics", match: ["analytics"], phase: 1 },
  { key: "reviewer", name: "Reviewer", sub: "OpenAI SDK · validation", color: "--reviewer", match: ["reviewer"], phase: 2 },
] as const;
```
Match strings work via `.toLowerCase().includes()`: `"analytics"` matches `"Analytics Agent"`, `"reviewer"` matches `"Reviewer Agent"`.

**2b. Fix `tileStatus` function** (lines 32-36) — make phase-aware AND handle stale state:
```typescript
function tileStatus(cfg: typeof AGENTS[number], active: string[], running: boolean) {
  if (active.some((a) => cfg.match.some((m) => a.toLowerCase().includes(m)))) {
    return running ? "working" : "done";
  }
  if (active.length > 0 && cfg.phase === 1) return running ? "done" : "done";
  return "idle";
}
```

**Why the `running` parameter matters (no stop button edge case):**

Without `running`, if the backend errors and never clears `active_agents`:
- `running` = false (stream ended), but `anyActive` = true (stale list)
- Tiles stay visible via `(running || (hasMessages && anyActive))`
- All matched agents show "working" forever — misleading since the run is over
- The topbar says "Complete" but tiles say "working" — contradictory

With `running`:
- When agent IS in the stale list but `running` is false → show "done" (not "working")
- This gives honest UI: the run ended, these agents completed what they could

**Full state table for `tileStatus(cfg, active, running)`:**

| Agent in list? | Phase | `running` | Result | Scenario |
|---|---|---|---|---|
| Yes | 1 or 2 | true | "working" | Normal: agent is active |
| Yes | 1 or 2 | false | "done" | Error/stale: run ended but list not cleared |
| No | 1 | true | "done" | Normal: Phase 1 agents dispatched together |
| No | 1 | false | "done" | Normal completion or error |
| No | 2 | true | "idle" | Normal: Reviewer hasn't started yet |
| No | 2 | false | "idle" | Normal: Reviewer never ran (Phase 1 error) |

**2c. Update tile rendering call site** (line 170) — pass `running`:
```typescript
const s = tileStatus(a, activeAgents, running);
```

**2d. Update orchestrator strip hardcoded counts** (lines 156, 158):
- Line 156: `"3 agents"` → `"5 agents"`
- Line 158: `"send_message ×3"` → `"send_message ×5"`

**2e. Update empty-state text** (line 195):
`"three specialized agents"` → `"five specialized agents"`

**No tile layout changes needed.** Keep all 5 tiles in a single `.tiles` row with `flex: 1`. The `.tname` class already has `overflow: hidden; text-overflow: ellipsis` (CSS line 156) for graceful truncation. During Phase 1, the Reviewer tile appears dimmed (`opacity: 0.5` via `.tile.idle`) which naturally communicates it hasn't started yet.

---

### 3. `src/web/nextjs-app/app/page.tsx` — Overview page (3 edits)

**3a. Hero text** (line 23):
`"four specialized agents"` → `"five specialized agents"`

**3b. Agent cards in flow diagram** (lines 51-54) — add Analytics to the existing array:
```typescript
{ name: "RAG Agent", color: "--rag", meta: "LlamaIndex · :8002" },
{ name: "Quant Agent", color: "--quant", meta: "LangGraph · :8003" },
{ name: "Market Context", color: "--market", meta: "CrewAI · :8004" },
{ name: "Analytics", color: "--analytics", meta: "PydanticAI · :8005" },
```
Then insert a Phase 2 section **between** the Phase 1 agent row (line 64) and the MCP connector (line 65):
- A dashed connector card: `"Phase 2 · sequential cross-validation ↓"`
- A single Reviewer Agent card (constrained to `maxWidth: "calc(25% - 10.5px)"` to match Phase 1 tile width)

Visual ordering: Orchestrator → A2A → Phase 1 agents (4) → Phase 2 connector → Reviewer → MCP connector → MCP server.

**3c. Feature card 04** (line 87):
`"all four agent processes"` → `"all five agent processes"`

---

### 4. `src/web/nextjs-app/app/operator/page.tsx` — Agent capability colors (1 edit)

**Lines 90-91:** The ternary chain falls back to `--market` / `--market-bg` for all unrecognized agents. Expand with Analytics and Reviewer before the fallback:

```typescript
background: a.name.includes("RAG") ? "var(--rag-bg)"
  : a.name.includes("Quant") ? "var(--quant-bg)"
  : a.name.includes("Market") ? "var(--market-bg)"
  : a.name.includes("Analytics") ? "var(--analytics-bg)"
  : a.name.includes("Reviewer") ? "var(--reviewer-bg)"
  : "var(--orch-bg)",
color: a.name.includes("RAG") ? "var(--rag)"
  : a.name.includes("Quant") ? "var(--quant)"
  : a.name.includes("Market") ? "var(--market)"
  : a.name.includes("Analytics") ? "var(--analytics)"
  : a.name.includes("Reviewer") ? "var(--reviewer)"
  : "var(--clay)",
```
**Note:** The SERVICES health list (lines 8-16) already includes both agents — no changes needed there.

---

### 5. `src/web/nextjs-app/app/trace/page.tsx` — Trace span colors + legend (3 edits)

**5a. `color()` function** (lines 28-34) — add 2 lines after the market check (line 32), before the MCP check (line 33):
```typescript
if (n.includes("analytics") || n.includes("pydanticai") || n.includes("trend") || n.includes("forecast")) return "var(--analytics)";
if (n.includes("reviewer") || n.includes("cross-valid")) return "var(--reviewer)";
```

**5b. `bg()` function** (lines 37-43) — add 2 lines after the market check (line 41), before the MCP check (line 42):
```typescript
if (n.includes("analytics") || n.includes("pydanticai")) return "var(--analytics-bg)";
if (n.includes("reviewer")) return "var(--reviewer-bg)";
```

**5c. Legend array** (lines 286-291) — add 2 entries after "Market Context" (line 290), before "MCP tool" (line 291):
```typescript
{ label: "Analytics", c: "var(--analytics)" },
{ label: "Reviewer", c: "var(--reviewer)" },
```

---

## Edge Cases: No Stop Button

Since there is no cancel/stop button, the frontend must handle these scenarios gracefully:

### Scenario 1: Backend errors during Phase 1 (before Reviewer starts)
- `active_agents` stays stale as `["Financial RAG Agent", "Quant Analysis Agent", "Market Context Agent", "Analytics Agent"]`
- `running` becomes `false` (stream emits `RunFinishedEvent` on line 563, always — even after errors)
- **With our fix**: `tileStatus` returns `"done"` for Phase 1 agents (agent in list + not running = "done"), Reviewer shows "idle" (never started). Topbar shows "Complete". Consistent UI.
- **Without our fix** (current code): tiles show "working" forever while topbar says "Complete" — contradictory.

### Scenario 2: Backend errors during Phase 2
- `active_agents` stays stale as all 5 agents
- `running` becomes `false`
- **With our fix**: all 5 tiles show "done". Honest — they all ran (partially). Topbar shows "Complete".

### Scenario 3: Backend timeout (line 415-425)
- The timeout triggers a `break`, falls through to the `active_agents` clear at line 522. **This path DOES clear the list.**
- Tiles disappear normally. No issue.

### Scenario 4: User navigates away and back
- CopilotKit state may or may not persist depending on session. If it persists, stale `active_agents` could show old tiles. Same mitigation: `running` being `false` shows "done" instead of "working".

### Scenario 5: User submits new query while loading
- `handleSubmit` (line 109) already guards: `if (!input.trim() || isLoading) return;`
- Input is also disabled: `disabled={isLoading}` (line 318)
- **No issue** — duplicate submissions are prevented.

---

## What Does NOT Need Changing

| Component | Why |
|---|---|
| `src/orchestrator/agui_bridge.py` | Already maps both agents and emits them in `active_agents` (lines 70-82) |
| Operator page SERVICES array | Already lists Analytics (:8005) and Reviewer (:8006) (lines 13-14) |
| Sidebar (`components/Sidebar.tsx`) | No agent references |
| Layout (`app/layout.tsx`) | No agent references |
| Backend agent cards / servers | Already fully configured |

---

## Risk Assessment

| Risk | Impact | Mitigation |
|---|---|---|
| TypeScript type change from adding `phase` | Low | `as const` infers literal types; `typeof AGENTS[number]` auto-includes `phase: 1 \| 2` |
| 5 tiles slightly narrower than 3 | Low | `.tname` already has `text-overflow: ellipsis` (CSS line 156) |
| `"analytics"` match string collision in trace spans | Very low | Agent display names are well-structured (`"Analytics Agent"`); unlikely false matches |
| Phase 1 agents stay "working" during Phase 2 | None | Already the current behavior for existing agents — backend never removes them individually |
| Stale `active_agents` on error (no stop button) | Low | `tileStatus` now uses `running` flag to show "done" instead of "working" when stream ends |

---

## Verification

1. **Build check**: `cd src/web/nextjs-app && npm run build` — confirm no TypeScript errors
2. **Dev server**: `npm run dev` — open `http://localhost:3000`
3. **Research page** (`/research`): Submit a query and confirm:
   - 5 agent tiles appear in one row
   - Phase 1 tiles (RAG, Quant, Market, Analytics) light up with correct colors when active
   - Reviewer tile stays dimmed/idle during Phase 1, lights up during Phase 2
   - Orchestrator strip says "5 agents" and "send_message ×5"
   - Empty state says "five specialized agents"
   - After run completes: tiles show checkmarks (done state), not stuck on "working"
4. **Error scenario**: If the backend is down or errors mid-run, tiles should transition to "done" (not stay "working") once the stream ends
5. **Overview page** (`/`): Confirm architecture diagram shows 4 Phase 1 agents + Reviewer, hero text says "five"
6. **Operator page** (`/operator`): Confirm Analytics and Reviewer capability card icons use teal/crimson (not market brown)
7. **Trace page** (`/trace`): Confirm analytics/reviewer spans use correct colors and legend shows 7 entries
