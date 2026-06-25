/** Recent queries storage backed by localStorage. Persists user research queries across sessions. */
"use client";

const KEY = "finsight_recent_queries";
const MAX = 12;

/** A single recent query entry with text, thread ID, and timestamp. */
export interface RecentQuery {
  text: string;
  threadId: string;
  ts: number;
}

const EMPTY: RecentQuery[] = [];
let _cachedRaw: string | null = null;
let _cachedResult: RecentQuery[] = EMPTY;

function parseQueries(): RecentQuery[] {
  const raw = localStorage.getItem(KEY) || "[]";
  if (raw === _cachedRaw) return _cachedResult;
  try {
    const parsed: unknown[] = JSON.parse(raw);
    _cachedResult = parsed.filter(
      (q): q is RecentQuery =>
        typeof q === "object" &&
        q !== null &&
        typeof (q as RecentQuery).text === "string" &&
        typeof (q as RecentQuery).ts === "number"
    );
  } catch {
    _cachedResult = EMPTY;
  }
  _cachedRaw = raw;
  return _cachedResult;
}

/** Reads recent queries from localStorage. Returns a stable reference when content hasn't changed. */
export function getRecentQueries(): RecentQuery[] {
  if (typeof window === "undefined") return EMPTY;
  return parseQueries();
}

/** Adds a query to the recent queries list (deduped by threadId, capped at 12). Dispatches a custom event to notify the sidebar. */
export function addRecentQuery(text: string, threadId: string): void {
  const existing = getRecentQueries().filter((q) => q.threadId !== threadId);
  const next = [{ text, threadId, ts: Date.now() }, ...existing].slice(0, MAX);
  localStorage.setItem(KEY, JSON.stringify(next));
  window.dispatchEvent(new CustomEvent("finsight:recent-queries-changed"));
}
