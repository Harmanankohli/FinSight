# FinSight Web Frontend — QA Findings

**Date:** 2026-06-27
**Scope:** `src/web/nextjs-app/` (Next.js 16.2.6 + React 19.2.4 + CopilotKit 1.59.1)
**Test method:** Local dev server on port 3000, browser-based systematic QA
**Backend:** NOT running (orchestrator on :8001 offline) — auth-gated pages could not be fully exercised
**Pages reviewed:** Overview (`/`), Research (`/research`), Dashboard (`/dashboard`), Memory (`/memory`), Operator (`/operator`), Login (`/login`)

---

## Summary

| # | Severity | Category | Issue |
|---|----------|----------|-------|
| 1 | HIGH | Functional | `tileStatus()` logic bug — all phase-1 tiles show "done" immediately |
| 2 | MEDIUM | Build/Deploy | `@ag-ui/client` missing from `package.json` dependencies |
| 3 | MEDIUM | Type Safety | `normalizeScoreAgent()` can return a non-AgentKey string |
| 4 | MEDIUM | Config | Langfuse default URL hardcoded to `jp.cloud`, README says `cloud` |
| 5 | MEDIUM | Performance | Memory page fires 8 parallel fetches on every mount, no cache/abort |
| 6 | LOW | Data Accuracy | Dashboard `truncated` flag threshold doesn't match fetch cap |
| 7 | LOW | Dead Code | `useAppStore` Zustand store declared but never consumed |
| 8 | LOW | Security | Login form has no rate limiting or brute-force protection |
| 9 | LOW | Docs | `/auth/*` rewrite missing from README |
| 10 | LOW | UX | No custom 404 / not-found page |

---

## Detailed Findings

### Issue 1 — Logic bug in `tileStatus()` makes all phase-1 tiles show "done` immediately

**Severity: HIGH | Category: Functional**
**File:** `app/research/page.tsx:38-44`

```ts
function tileStatus(cfg: typeof AGENTS[number], active: string[], running: boolean) {
  if (active.some((a) => cfg.match.some((m) => a.toLowerCase().includes(m)))) {
    return running ? "working" : "done";
  }
  if (active.length > 0 && cfg.phase === 1) return running ? "done" : "done";  // <-- BUG
  return "idle";
}
```

**Problem:** The second branch returns `"done"` regardless of the `running` parameter. The ternary `running ? "done" : "done"` is a no-op — every phase-1 tile jumps straight to "done" as soon as ANY agent is active, even while the run is still in progress.

**Impact:** During a live research run, all 4 phase-1 tiles (RAG, Quant, Market, Analytics) light up as "done" the moment the first agent starts working. This defeats the purpose of the progress tiles — users see all agents as completed while the orchestrator is still running.

**Expected behavior:** The second branch should likely be `running ? "working" : "done"` to mirror the first branch's logic, so tiles show "working" while the run is in progress and only flip to "done" after completion.

---

### Issue 2 — `@ag-ui/client` missing from `package.json` dependencies

**Severity: MEDIUM | Category: Build/Deployability**
**File:** `package.json`

**Problem:** `app/api/copilotkit/route.ts` imports `HttpAgent` from `@ag-ui/client`, but this package is NOT listed in `package.json` `dependencies`. It currently resolves only because it's pulled in transitively by `@copilotkit/runtime`.

**Impact:** This is fragile — a CopilotKit version bump could drop or relocate the transitive dependency and break the build without warning. The build currently works by accident, not by declaration.

**Fix:** Add `"@ag-ui/client": "^0.0.53"` (or matching version) to `dependencies` in `package.json`.

---

### Issue 3 — `normalizeScoreAgent()` can return a non-AgentKey string

**Severity: MEDIUM | Category: Type Safety**
**File:** `lib/agentColors.ts:44-46`

```ts
export function normalizeScoreAgent(scoreAgent: string): AgentKey {
  return SCORE_AGENT_TO_KEY[scoreAgent] ?? (scoreAgent as AgentKey);
}
```

**Problem:** The `SCORE_AGENT_TO_KEY` map only has one entry (`market_context -> market`). If Langfuse returns a score with an agent name not in this map (e.g. `"financial_rag"`, `"quant_analysis"`, `"market_context_agent"`), the function returns the raw string cast to `AgentKey`. The `as AgentKey` cast bypasses TypeScript's type checker.

**Impact:** In the dashboard RAGAS scores panel, `AGENT_COLOR[agent as AgentKey]` returns `undefined` for unmapped agent names, so the agent dot renders with no color. The score card still renders but with a missing/misleading visual indicator. Additionally, the `byAgent` key won't match any known agent, potentially causing duplicate or orphaned entries.

**Fix:** Add more mappings to `SCORE_AGENT_TO_KEY` or add a fallback that logs a warning and defaults to `"orchestrator"`.

---

### Issue 4 — Langfuse default base URL hardcoded to a specific cloud region

**Severity: MEDIUM | Category: Configurability**
**File:** `lib/langfuse.ts:2`

```ts
const LF_BASE = process.env.LANGFUSE_BASE_URL || "https://jp.cloud.langfuse.com";
```

**Problem:** The README (`src/web/nextjs-app/README.md:106`) says the default is `https://cloud.langfuse.com`, but the code defaults to `https://jp.cloud.langfuse.com` (the Japan region).

**Impact:** If someone sets only their Langfuse keys but not the base URL, their dashboard metrics data goes to a region they may not have access to. The README and code disagree, which is confusing during troubleshooting.

**Fix:** Either change the code default to match the README (`https://cloud.langfuse.com`), or update the README to document the Japan region default.

---

### Issue 5 — Memory page fetches all 8 tickers in parallel on every mount

**Severity: MEDIUM | Category: Performance**
**File:** `app/memory/page.tsx:66-79` and `line 81-105`

**Problem:** When no `?ticker=` query param is present, the Memory page fires 8 parallel fetches (`/api/orch/api/memory/ticker/{NVDA,AAPL,MSFT,TSLA,GOOGL,AMZN,META,LLY}`) on every mount. There is no:
- Client-side caching (localStorage / SWR / React Query)
- Request deduplication
- AbortController for cleanup on unmount
- Debounce on re-renders

**Impact:** Navigating away and back to `/memory` re-fires all 8 requests. With a slow backend this is 8x the necessary load on the orchestrator. If the user navigates away mid-fetch, the abandoned promises still resolve and call `setBriefs` on an unmounted component (React warning in dev mode).

**Fix:** Add a caching layer (even a simple module-level Map with TTL), use `AbortController` for cleanup, and consider lazy-loading tickers on demand instead of all 8 at once.

---

### Issue 6 — Dashboard `truncated` flag logic is off-by-one

**Severity: LOW | Category: Data Accuracy**
**File:** `app/api/dashboard/route.ts:129`

```ts
const truncated = totalPages > 5;
```

**Problem:** But the loop on line 123 fetches up to `maxPages = Math.min(totalPages, 10)`. So if there are exactly 6-10 pages of traces, `truncated` is `true` (because 6 > 5) but the code actually fetches ALL pages.

**Impact:** The banner "Showing metrics from the most recent 1000 traces" would be displayed even when all data has been fetched, misleading users into thinking data is incomplete when it isn't.

**Fix:** Change the threshold to `totalPages > maxPages` (i.e., `totalPages > 10`) to match the actual fetch cap.

---

### Issue 7 — `useAppStore` (Zustand) is declared but never consumed

**Severity: LOW | Category: Dead Code**
**File:** `lib/stores/useAppStore.ts`

**Problem:** The Zustand store exposes `sidebarOpen` and `userId` state with setters, but no component in the codebase reads or writes these values. The sidebar has no toggle button and `userId` is never set.

**Impact:** Dead code — adds bundle size ( Zustand is included in `package.json` ) without providing value. Either this is unfinished functionality (sidebar toggle was planned) or leftover from a previous design.

**Fix:** Either implement the sidebar toggle UI and wire it to the store, or remove the store and the Zustand dependency if it's truly unused.

---

### Issue 8 — Login form has no rate limiting or brute-force protection

**Severity: LOW | Category: Security**
**File:** `app/login/page.tsx`

**Problem:** The Sign in button is disabled while a request is in flight (`busy` state), but there is no:
- Exponential backoff between attempts
- CAPTCHA after N failures
- Account lockout
- Client-side attempt counter

**Impact:** Each click fires a fresh `/auth/login` POST to the orchestrator. While this is standard for a local-first tool, it's worth noting if this ever gets deployed publicly. The backend may have its own rate limiting, but the frontend provides no feedback about remaining attempts or lockout state.

**Fix:** For a local tool this is acceptable. If deploying publicly, add a client-side attempt counter with increasing delays and surface lockout state from the backend.

---

### Issue 9 — `next.config.ts` rewrites for `/auth/*` and `/reports/*` are undocumented

**Severity: LOW | Category: Documentation**
**File:** `next.config.ts:17-24` vs `src/web/nextjs-app/README.md:54-58`

**Problem:** The README documents the `/api/orch/:path*` and `/reports/:path*` rewrites but is MISSING the `/auth/:path*` rewrite that proxies login/refresh/logout to the orchestrator.

**Impact:** Anyone reading the docs to understand the auth flow won't find the `/auth/*` rewrite. This makes debugging auth issues harder — developers might not realize login requests are proxied to port 8001.

**Fix:** Add the `/auth/:path*` rewrite to the README's rewrites table.

---

### Issue 10 — No custom 404 / not-found page

**Severity: LOW | Category: UX**
**File:** `app/not-found.tsx` (does not exist)

**Problem:** Navigating to an unknown route (e.g. `/foo`) shows Next.js's default 404 page with no FinSight branding, no sidebar, and no way to navigate back to the app.

**Impact:** Users who follow a broken link or mistype a URL see an unstyled error page with no navigation options. This is a poor experience for a polished product.

**Fix:** Create `app/not-found.tsx` with the FinSight design system (clay/ivory palette, sidebar or nav links, friendly message).

---

## Additional Observations

### Auth flow works correctly
The login form properly disables the submit button when fields are empty or while busy. The "Login failed" error displays correctly when the backend is unreachable. The auth redirect logic (redirect to `/login` when unauthenticated, redirect away from `/login` when authenticated) is sound.

### Overview page renders cleanly
No console errors, no visual issues. The architecture diagram, feature grid, and CTAs all render correctly. The page is a static server component with no data dependencies.

### Console is clean
No JavaScript errors on any page. The only console messages are:
- React DevTools suggestion (info)
- `[HMR] connected` (dev mode)
- `Lit is in dev mode` (warning from CopilotKit's Lit dependency — harmless in dev)
- `[CopilotKit Inspector] anonymous interaction telemetry enabled` (info — worth noting for privacy)

### CopilotKit telemetry notice
The console shows `[CopilotKit Inspector] anonymous interaction telemetry enabled`. If user privacy is a concern, this can be opted out per the docs.copilotkit.ai/telemetry link.

---

## Recommended Fix Priority

1. **Issue 1** (tileStatus bug) — functional defect, affects core user-facing feature
2. **Issue 2** (missing dependency) — could break build on next CopilotKit update
3. **Issue 3** (normalizeScoreAgent) — visual bug in dashboard scores panel
4. **Issue 5** (memory page fetches) — performance, unnecessary backend load
5. **Issue 4** (Langfuse URL mismatch) — docs/code disagreement
6. **Issue 6** (truncated flag) — minor data accuracy
7. **Issue 7** (dead store) — code hygiene
8. **Issues 8-10** — nice-to-have improvements
