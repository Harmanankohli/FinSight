/** Agent identifier keys used throughout the dashboard and UI for color-coding. */
export type AgentKey = "orchestrator" | "rag" | "quant" | "market" | "analytics" | "reviewer" | "mcp";

/** Maps a trace/observation name to an {@link AgentKey} based on keyword matching. */
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

/** Maps each agent key to its accent CSS variable for UI color-coding. */
export const AGENT_COLOR: Record<AgentKey, string> = {
  orchestrator: "var(--clay)",
  rag: "var(--rag)",
  quant: "var(--quant)",
  market: "var(--market)",
  analytics: "var(--analytics)",
  reviewer: "var(--reviewer)",
  mcp: "var(--mcp)",
};

/** Maps each agent key to its background CSS variable for UI tiles. */
export const AGENT_BG: Record<AgentKey, string> = {
  orchestrator: "var(--orch-bg)",
  rag: "var(--rag-bg)",
  quant: "var(--quant-bg)",
  market: "var(--market-bg)",
  analytics: "var(--analytics-bg)",
  reviewer: "var(--reviewer-bg)",
  mcp: "var(--mcp-bg)",
};

/** Maps Langfuse score agent names that differ from the canonical key (e.g. market_context → market). */
export const SCORE_AGENT_TO_KEY: Record<string, AgentKey> = {
  market_context: "market",
};

/** Normalizes Langfuse score agent names (e.g. "market_context") to the canonical {@link AgentKey}. */
export function normalizeScoreAgent(scoreAgent: string): AgentKey {
  return SCORE_AGENT_TO_KEY[scoreAgent] ?? (scoreAgent as AgentKey);
}
