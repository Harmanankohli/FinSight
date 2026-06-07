"use client";

const KEY = "finsight_recent_queries";
const MAX = 12;

export interface RecentQuery {
  text: string;
  threadId: string;
  ts: number;
}

export function getRecentQueries(): RecentQuery[] {
  if (typeof window === "undefined") return [];
  try {
    const raw: unknown[] = JSON.parse(localStorage.getItem(KEY) || "[]");
    return raw.filter(
      (q): q is RecentQuery =>
        typeof q === "object" &&
        q !== null &&
        typeof (q as RecentQuery).text === "string" &&
        typeof (q as RecentQuery).ts === "number"
    );
  } catch {
    return [];
  }
}

export function addRecentQuery(text: string, threadId: string): void {
  const existing = getRecentQueries().filter((q) => q.threadId !== threadId);
  const next = [{ text, threadId, ts: Date.now() }, ...existing].slice(0, MAX);
  localStorage.setItem(KEY, JSON.stringify(next));
  window.dispatchEvent(new CustomEvent("finsight:recent-queries-changed"));
}
