# FinSight Web Frontend — QA Fix Plan

## Context

The QA audit (`docs/fix-ui.md`) found 10 issues (1 high, 4 medium, 5 low) across the Next.js frontend at `src/web/nextjs-app/`. This plan fixes 9 of 10 issues in 4 phases, ordered by severity. Issue 8 (login rate limiting) is skipped — acceptable for a local tool per QA doc.

**All file paths below are relative to `src/web/nextjs-app/`.**

---

## Phase 1: Critical Functional Fixes

### Step 1.1 — Fix `tileStatus()` ternary bug (Issue #1, HIGH)

**File:** `app/research/page.tsx`, line 42

**Change:** Replace the no-op ternary:
```ts
// BEFORE (line 42)
if (active.length > 0 && cfg.phase === 1) return running ? "done" : "done";

// AFTER
if (active.length > 0 && cfg.phase === 1) return running ? "working" : "done";
```

**Why:** Both branches return `"done"`, so all phase-1 tiles (RAG, Quant, Market, Analytics) show a checkmark the moment any agent is active — even while the run is in progress. The fix makes tiles show bouncing-dots ("working") until the run completes.

---

### Step 1.2 — Fix dashboard `truncated` flag off-by-one (Issue #6, LOW)

**File:** `app/api/dashboard/route.ts`, line 129

**Change:**
```ts
// BEFORE (line 129)
const truncated = totalPages > 5;

// AFTER
const truncated = totalPages > maxPages;
```

**Why:** `maxPages` (line 122) is `Math.min(totalPages, 10)`. The loop fetches up to 10 pages, but the flag triggers at 5. Result: if there are 6–10 pages (all fetched), the UI falsely says data is truncated. Reference the existing `maxPages` variable so the flag fires only when pages were actually skipped.

**Downstream check:** The dashboard UI (`app/dashboard/page.tsx:254–257`) renders a banner "Showing metrics from the most recent 1000 traces" when `data.truncated` is true. Since maxPages=10 and each page has limit=100, the cap is indeed 1000 traces — the message remains accurate after this fix.

---

## Phase 2: Dependency & Type Safety

### Step 2.1 — Add `@ag-ui/client` to explicit dependencies (Issue #2, MEDIUM)

**File:** `package.json`

**Change:** Add `"@ag-ui/client": "^0.0.53"` to `dependencies` (alphabetically first, before `@copilotkit` entries):
```json
"dependencies": {
  "@ag-ui/client": "^0.0.53",
  "@copilotkit/react-core": "^1.59.1",
  ...
```

**Why:** `app/api/copilotkit/route.ts:7` imports `HttpAgent` from `@ag-ui/client`, but it resolves only as a transitive dep of `@copilotkit/runtime`. A CopilotKit version bump could break the build without warning.

---

### Step 2.2 — Validate `normalizeScoreAgent()` against known keys (Issue #3, MEDIUM)

**File:** `lib/agentColors.ts`, lines 38–46

**Change:** Add a runtime validation set and update the function:
```ts
// Keep existing SCORE_AGENT_TO_KEY map (line 39-41) unchanged

// ADD after the map (before the function):
const VALID_AGENT_KEYS: ReadonlySet<string> = new Set(Object.keys(AGENT_COLOR));

// REPLACE the function (lines 44-46):
export function normalizeScoreAgent(scoreAgent: string): AgentKey {
  const mapped = SCORE_AGENT_TO_KEY[scoreAgent];
  if (mapped) return mapped;
  if (VALID_AGENT_KEYS.has(scoreAgent)) return scoreAgent as AgentKey;
  return "orchestrator";
}
```

**Why:** The current `as AgentKey` cast bypasses TypeScript's type checker. If Langfuse sends an unknown agent name, `AGENT_COLOR[key]` returns `undefined` and the score dot renders with no color. The fix validates at runtime and falls back to `"orchestrator"`.

**Consumer:** `app/api/dashboard/scores/route.ts:49` calls `normalizeScoreAgent(parts[1])` where `parts` come from score names like `"ragas/market_context/Faithfulness"`. The fix is backwards-compatible — all currently valid inputs produce the same outputs.

---

### Step 2.3 — Fix Langfuse default URL to match README (Issue #4, MEDIUM)

**File:** `lib/langfuse.ts`, line 2

**Change:**
```ts
// BEFORE
const LF_BASE = process.env.LANGFUSE_BASE_URL || "https://jp.cloud.langfuse.com";

// AFTER
const LF_BASE = process.env.LANGFUSE_BASE_URL || "https://cloud.langfuse.com";
```

**Why:** README line 106 documents the default as `https://cloud.langfuse.com`. The code has the Japan region URL. Users who don't set the env var get routed to the wrong region.

---

## Phase 3: Performance Fix

### Step 3.1 — Add AbortController to Memory page useEffect (Issue #5, MEDIUM)

**File:** `app/memory/page.tsx`, lines 81–105

**Change:** Wrap the existing useEffect body with AbortController:

1. Create `const controller = new AbortController()` and `const { signal } = controller` at the top of the effect.
2. Pass `{ signal }` to every `fetch()` call (both the single-ticker branch and the 8-ticker `Promise.all` branch).
3. Guard all state-setting callbacks (`setBriefs`, `setSearchedTicker`, `setLoading`) with `if (!signal.aborted)`.
4. Return `() => controller.abort()` as the cleanup function.

**Full replacement code for lines 81–105:**
```ts
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    const ticker = urlTicker;
    if (ticker) {
      fetch(`/api/orch/api/memory/ticker/${ticker.toUpperCase()}`, { signal })
        .then((r) => r.ok ? r.json() : [])
        .then((data) => { if (!signal.aborted) { setSearchedTicker(ticker.toUpperCase()); setBriefs(data); } })
        .catch(() => { if (!signal.aborted) setBriefs([]); })
        .finally(() => { if (!signal.aborted) setLoading(false); });
    } else {
      const all: Brief[] = [];
      const seen = new Set<string>();
      Promise.all(COMMON_TICKERS.map((t) =>
        fetch(`/api/orch/api/memory/ticker/${t}`, { signal })
          .then((r) => r.ok ? r.json() : [])
          .then((items: Brief[]) => { for (const b of items) if (!seen.has(b.id)) { seen.add(b.id); all.push(b); } })
          .catch(() => {})
      ))
        .then(() => {
          if (!signal.aborted) {
            all.sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
            setSearchedTicker("all");
            setBriefs(all);
          }
        })
        .finally(() => { if (!signal.aborted) setLoading(false); });
    }
    return () => controller.abort();
  }, [urlTicker]);
```

**Why:** Without cleanup, navigating away mid-fetch leaves 8 abandoned promises that call `setBriefs` on an unmounted component (React warning in dev). The AbortController cancels in-flight requests on unmount or dependency change.

**Note:** The standalone `fetchBriefs()` (line 54) and `fetchAll()` (line 65) functions are called by user-triggered handlers (form submit, "Show all" button). They do NOT need AbortController — they're one-shot user actions, not mount-lifecycle fetches. Only the `useEffect` on lines 81–105 needs cleanup.

---

## Phase 4: Cleanup & UX

### Step 4.1 — Remove dead Zustand store (Issue #7, LOW)

Three sub-steps:

**4.1a** — Delete `lib/stores/useAppStore.ts` and the empty `lib/stores/` directory.
Confirmed: no component imports `useAppStore` (only referenced in its own file and README).

**4.1b** — Remove the README reference.
**File:** `README.md`, line 95 — delete the row `| \`lib/stores/useAppStore.ts\` | Zustand store — \`sidebarOpen\`, \`userId\` |`

**4.1c** — Remove `zustand` from `package.json` dependencies.
**File:** `package.json` — remove `"zustand": "^5.0.14"` line. Ensure no trailing comma on the preceding `remark-gfm` line.
Confirmed: no other file in the project imports from `zustand`.

---

### Step 4.2 — Add missing `/auth/*` rewrite to README (Issue #9, LOW)

**File:** `README.md`, lines 55–58 (Rewrites table)

**Change:** Add a row for `/auth/:path*` between the existing two rows:
```markdown
| `/api/orch/:path*` | `http://localhost:8001/:path*` (orchestrator REST) |
| `/auth/:path*` | `http://localhost:8001/auth/:path*` (login/refresh/logout) |
| `/reports/:path*` | `http://localhost:8001/reports/:path*` (report downloads) |
```

**Why:** `next.config.ts` lines 18–20 define this rewrite, but the README omits it. Developers debugging auth issues won't know login requests are proxied.

---

### Step 4.3 — Create custom 404 page (Issue #10, LOW)

**File to create:** `app/not-found.tsx`

Create a server component (no `"use client"`) using existing design tokens:
- Reuse `.topbar` + `.scroll` layout pattern (same as every other page)
- Large `404` text in `var(--clay-light)` with `var(--serif)` font
- Descriptive message in `var(--text-secondary)`
- "Back to Overview" link styled as a `.pill` button with `var(--clay)` background
- No sidebar needed — `Providers.tsx` already wraps all pages with the sidebar

```tsx
import Link from "next/link";

export default function NotFound() {
  return (
    <>
      <div className="topbar">
        <div>
          <h1>Not Found</h1>
          <div className="sub">The page you requested does not exist</div>
        </div>
      </div>
      <div className="scroll">
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center",
          justifyContent: "center", padding: "100px 30px", textAlign: "center",
        }}>
          <div style={{
            fontSize: 72, fontWeight: 700, fontFamily: "var(--serif)",
            color: "var(--clay-light)", lineHeight: 1,
          }}>404</div>
          <p style={{
            fontSize: 17, color: "var(--text-secondary)",
            marginTop: 16, maxWidth: "40ch", lineHeight: 1.5,
          }}>
            This page could not be found. It may have been moved or deleted.
          </p>
          <Link href="/" className="pill" style={{
            marginTop: 28, background: "var(--clay)", color: "#fff",
            borderColor: "var(--clay)", padding: "11px 22px",
            fontWeight: 600, fontSize: 14, borderRadius: 999,
          }}>Back to Overview</Link>
        </div>
      </div>
    </>
  );
}
```

---

## Post-Implementation

After all phases:
1. Run `npm install` (syncs lockfile for package.json changes in 2.1 and 4.1c)
2. Run `npm run build` — confirm zero compilation errors
3. Run `npm run lint` — confirm no ESLint violations
4. Manual smoke test with `npm run dev`:
   - `/research` — submit a query, verify tiles show "working" during run (Phase 1)
   - `/dashboard` — verify truncated banner logic (Phase 1)
   - `/memory` — navigate away quickly, confirm no React warnings (Phase 3)
   - `/nonexistent` — verify custom 404 page renders with sidebar (Phase 4)

## Skipped

- **Issue 8** (Login rate limiting) — acceptable for local tool per QA doc
