/**
 * Trace filter heuristics for auth-related log entries.
 *
 * WP 3.3: filters and highlights auth.denied log entries in the trace viewer,
 * enabling operators to quickly identify authentication failures.
 */

export interface TraceEntry {
  ts: string;
  level: string;
  service: string;
  logger: string;
  message: string;
  trace_id?: string;
  session_id?: string;
  user_id?: string;
  [key: string]: unknown;
}

export type TraceFilterResult = {
  matched: boolean;
  category: "auth_denied" | "auth_success" | "auth_lockout" | "other";
  reason?: string;
  user_id?: string;
};

const AUTH_DENIED_RE = /auth\.denied\s+reason=(\S+)/;
const AUTH_LOCKOUT_RE = /(?:Account locked|Login failure)/;
const AUTH_SUCCESS_RE = /Login success/;
const REFRESH_REUSE_RE = /Refresh reuse detected/;

/**
 * Classify a single trace entry by auth-related heuristics.
 */
export function classifyTraceEntry(entry: TraceEntry): TraceFilterResult {
  const msg = entry.message || "";

  // Auth denied (structured log from middleware)
  const deniedMatch = msg.match(AUTH_DENIED_RE);
  if (deniedMatch) {
    return {
      matched: true,
      category: "auth_denied",
      reason: deniedMatch[1],
      user_id: entry.user_id,
    };
  }

  // Lockout events
  if (AUTH_LOCKOUT_RE.test(msg)) {
    return {
      matched: true,
      category: "auth_lockout",
      user_id: entry.user_id,
    };
  }

  // Refresh reuse detection
  if (REFRESH_REUSE_RE.test(msg)) {
    return {
      matched: true,
      category: "auth_denied",
      reason: "refresh_reuse",
      user_id: entry.user_id,
    };
  }

  // Auth success
  if (AUTH_SUCCESS_RE.test(msg)) {
    return {
      matched: true,
      category: "auth_success",
      user_id: entry.user_id,
    };
  }

  return { matched: false, category: "other" };
}

/**
 * Filter an array of trace entries, returning only auth-related entries.
 */
export function filterAuthTraces(
  entries: TraceEntry[],
  categories?: Array<"auth_denied" | "auth_success" | "auth_lockout">,
): TraceEntry[] {
  return entries.filter((e) => {
    const result = classifyTraceEntry(e);
    if (!result.matched) return false;
    if (categories && !categories.includes(result.category)) return false;
    return true;
  });
}

/**
 * Count auth denied events grouped by reason.
 */
export function countAuthDeniedByReason(
  entries: TraceEntry[],
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const e of entries) {
    const result = classifyTraceEntry(e);
    if (result.category === "auth_denied" && result.reason) {
      counts[result.reason] = (counts[result.reason] || 0) + 1;
    }
  }
  return counts;
}

/**
 * Count auth denied events grouped by user_id.
 */
export function countAuthDeniedByUser(
  entries: TraceEntry[],
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const e of entries) {
    const result = classifyTraceEntry(e);
    if (result.category === "auth_denied" && result.user_id) {
      counts[result.user_id] = (counts[result.user_id] || 0) + 1;
    }
  }
  return counts;
}
