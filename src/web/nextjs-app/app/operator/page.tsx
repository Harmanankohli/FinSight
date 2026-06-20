/** Operator console page displaying service health status, agent capabilities, and system configuration. */
"use client";

import { useEffect, useState } from "react";

interface SvcStatus { name: string; url: string; port: string; fw: string; status: "ok" | "warn" | "down" | "loading"; latency?: number; }
interface AgentInfo { name: string; description: string; skills: string; }

const SERVICES: Omit<SvcStatus, "status" | "latency">[] = [
  { name: "Investment Orchestrator", url: "/api/health?svc=orchestrator", port: ":8001", fw: "Google ADK" },
  { name: "Financial RAG Agent", url: "/api/health?svc=rag", port: ":8002", fw: "LlamaIndex" },
  { name: "Quant Analysis Agent", url: "/api/health?svc=quant", port: ":8003", fw: "LangGraph" },
  { name: "Market Context Agent", url: "/api/health?svc=market", port: ":8004", fw: "CrewAI" },
  { name: "Analytics Agent", url: "/api/health?svc=analytics", port: ":8005", fw: "PydanticAI" },
  { name: "Reviewer Agent", url: "/api/health?svc=reviewer", port: ":8006", fw: "OpenAI Agents SDK" },
  { name: "finsight-mcp Server", url: "/api/health?svc=mcp", port: ":8010", fw: "FastMCP" },
];

const LED = { ok: "var(--quant)", warn: "var(--hold)", down: "var(--sell)", loading: "var(--sand-deep)" };

/** Renders the operator console: service health grid, agent capability cards, and configuration table. */
export default function OperatorPage() {
  const [svcs, setSvcs] = useState<SvcStatus[]>(SERVICES.map((s) => ({ ...s, status: "loading" })));
  const [agents, setAgents] = useState<AgentInfo[]>([]);

  useEffect(() => {
    SERVICES.forEach(async (svc, i) => {
      try {
        const r = await fetch(svc.url);
        const data = await r.json();
        const status = data.status === "ok" ? "ok" : data.status === "warn" ? "warn" : "down";
        setSvcs((p) => { const n = [...p]; n[i] = { ...svc, status: status as SvcStatus["status"], latency: data.latency }; return n; });
      } catch {
        setSvcs((p) => { const n = [...p]; n[i] = { ...svc, status: "down" }; return n; });
      }
    });
    fetch("/api/orch/api/agents").then((r) => r.ok ? r.json() : []).then((d) => setAgents(Array.isArray(d) ? d : [])).catch(() => {});
  }, []);

  const okCount = svcs.filter((s) => s.status === "ok").length;

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Operator Console</h1>
          <div className="sub">Service health, agent capabilities, and system status</div>
        </div>
        <div style={{ marginLeft: "auto" }} />
        <span className="pill mono">{okCount}/{SERVICES.length} healthy</span>
      </div>
      <div className="scroll">
        <div style={{ maxWidth: 1080, margin: "0 auto", padding: "26px 30px 60px" }}>

          {/* Service health */}
          <h2 style={{ fontSize: 18, marginBottom: 14 }}>Service health</h2>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: 34 }}>
            {svcs.map((s) => (
              <div key={s.name} className="card" style={{ padding: "16px 18px", display: "flex", alignItems: "center", gap: 14 }}>
                <div style={{
                  width: 11, height: 11, borderRadius: "50%", flexShrink: 0,
                  background: LED[s.status],
                  boxShadow: s.status !== "loading" ? `0 0 0 4px ${s.status === "ok" ? "var(--quant-bg)" : s.status === "warn" ? "var(--hold-bg)" : "var(--sell-bg)"}` : undefined,
                }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 14, fontWeight: 600 }}>{s.name}</div>
                  <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>{s.port} · {s.fw}</div>
                </div>
                <div style={{ textAlign: "right", flexShrink: 0 }}>
                  <div className="mono" style={{ fontSize: 15, fontWeight: 600, color: s.status === "ok" ? "var(--quant)" : s.status === "warn" ? "var(--hold)" : "var(--sell)" }}>
                    {s.status === "loading" ? "…" : s.latency ? `${s.latency}ms` : s.status.toUpperCase()}
                  </div>
                  <div style={{ fontSize: 10.5, color: "var(--text-muted)", marginTop: 2 }}>
                    {s.status === "ok" ? "OK" : s.status === "warn" ? "Degraded" : s.status === "down" ? "Down" : "Checking…"}
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Agent capabilities */}
          <h2 style={{ fontSize: 18, marginBottom: 14 }}>Agent capabilities</h2>
          <div style={{ display: "grid", gap: 12, marginBottom: 34 }}>
            {agents.length === 0 ? (
              <div style={{ color: "var(--text-muted)", fontSize: 13, padding: 20 }}>No agents discovered</div>
            ) : agents.map((a) => (
              <div key={a.name} className="card" style={{ padding: "16px 18px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 11, marginBottom: 9 }}>
                  <div style={{
                    width: 34, height: 34, borderRadius: 9, display: "grid", placeItems: "center",
                    fontFamily: "var(--serif)", fontWeight: 700, fontSize: 15, flexShrink: 0,
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
                  }}>
                    {a.name[0]}
                  </div>
                  <div>
                    <div style={{ fontSize: 14, fontWeight: 600 }}>{a.name}</div>
                    <div className="mono" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>streaming</div>
                  </div>
                </div>
                <p style={{ fontSize: 12.5, color: "var(--text-secondary)", margin: "0 0 10px", lineHeight: 1.5 }}>{a.description}</p>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {a.skills.split(", ").map((s) => <span key={s} className="pill mono">{s}</span>)}
                </div>
              </div>
            ))}
          </div>

          {/* Config */}
          <h2 style={{ fontSize: 18, marginBottom: 14 }}>Configuration</h2>
          <div className="card" style={{ padding: "18px 22px" }}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 36px" }}>
              {[
                ["AG-UI endpoint", "/a2a-agui"],
                ["RAGAS eval endpoint", "/agentic_chat"],
                ["Orchestrator port", "8001"],
                ["Frontend port", "3000"],
              ].map(([k, v]) => (
                <div key={k} style={{ display: "flex", justifyContent: "space-between", gap: 14, padding: "9px 0", borderBottom: "1px solid var(--sand)", fontSize: 12.5 }}>
                  <span style={{ color: "var(--text-muted)" }}>{k}</span>
                  <span className="mono" style={{ fontSize: 11.5, color: "var(--text-primary)", textAlign: "right" }}>{v}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
