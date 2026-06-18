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

export const SCORE_AGENT_TO_KEY: Record<string, AgentKey> = {
  market_context: "market",
};

export function normalizeScoreAgent(scoreAgent: string): AgentKey {
  return SCORE_AGENT_TO_KEY[scoreAgent] ?? (scoreAgent as AgentKey);
}
