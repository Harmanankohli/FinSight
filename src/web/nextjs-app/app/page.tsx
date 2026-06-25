/** Landing page describing FinSight's multi-agent architecture, agent flow diagram, and platform features. */
import Link from "next/link";

/** Renders the overview landing page with hero section, A2A flow diagram, and feature cards. */
export default function OverviewPage() {
  return (
    <>
      <div className="topbar">
        <div>
          <h1>Overview</h1>
          <div className="sub">What FinSight is and how its agents work together</div>
        </div>
        <div style={{ marginLeft: "auto" }} />
        <span className="pill mono">AG-UI · CopilotKit v2</span>
      </div>
      <div className="scroll">
        <div style={{ maxWidth: 940, margin: "0 auto", padding: "0 30px 60px" }}>
          {/* Hero */}
          <section style={{ padding: "60px 0 30px" }}>
            <div className="eyebrow" style={{ marginBottom: 18 }}>Multi-agent · A2A protocol · AG-UI frontend</div>
            <h1 style={{ fontSize: 46, lineHeight: 1.08, maxWidth: "16ch", letterSpacing: "-0.02em" }}>
              An analyst team that answers <em style={{ fontStyle: "italic", color: "var(--clay)" }}>&ldquo;should I invest?&rdquo;</em>
            </h1>
            <p style={{ fontSize: 17, lineHeight: 1.6, color: "var(--text-secondary)", maxWidth: "60ch", marginTop: 22 }}>
              FinSight coordinates five specialized agents — each built on a different framework —
              into a single, citation-backed investment brief. The orchestrator delegates over the
              Agent-to-Agent protocol, every agent pulls live data through one MCP server, and the
              whole run streams to a CopilotKit v2 chat surface over AG-UI.
            </p>
            <div style={{ display: "flex", gap: 12, marginTop: 30, alignItems: "center" }}>
              <Link href="/research" className="pill" style={{ background: "var(--clay)", color: "#fff", borderColor: "var(--clay)", padding: "11px 20px", fontWeight: 600, fontSize: 14, borderRadius: 999 }}>
                Open Research →
              </Link>
              <Link href="/operator" className="pill" style={{ padding: "11px 20px", fontWeight: 600, fontSize: 14, borderRadius: 999 }}>
                System status
              </Link>
            </div>
          </section>

          {/* Flow diagram */}
          <section style={{ margin: "46px 0" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <div className="card" style={{ padding: "16px 18px", borderColor: "var(--clay-light)", background: "var(--orch-bg)" }}>
                <div style={{ fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", gap: 8 }}>
                  <span className="swatch" style={{ background: "var(--clay)" }} />Investment Orchestrator
                </div>
                <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 5 }}>Google ADK · LlmAgent · send_message tool · :8001</div>
              </div>
              <div className="card" style={{ textAlign: "center", background: "var(--ivory)", borderStyle: "dashed", padding: 9, fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-muted)" }}>
                A2A Protocol · JSON-RPC over HTTP · streaming · parallel fan-out ↓
              </div>
              <div style={{ display: "flex", gap: 14 }}>
                {[
                  { name: "RAG Agent", color: "--rag", meta: "LlamaIndex · :8002" },
                  { name: "Quant Agent", color: "--quant", meta: "LangGraph · :8003" },
                  { name: "Market Context", color: "--market", meta: "CrewAI · :8004" },
                  { name: "Analytics", color: "--analytics", meta: "PydanticAI · :8005" },
                ].map((a) => (
                  <div key={a.name} className="card" style={{ flex: 1, padding: "16px 18px" }}>
                    <div style={{ height: 3, borderRadius: 2, background: `var(${a.color})`, marginBottom: 11 }} />
                    <div style={{ fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", gap: 8 }}>
                      <span className="swatch" style={{ background: `var(${a.color})` }} />{a.name}
                    </div>
                    <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 5 }}>{a.meta}</div>
                  </div>
                ))}
              </div>
              <div className="card" style={{ textAlign: "center", background: "var(--ivory)", borderStyle: "dashed", padding: 9, fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-muted)" }}>
                Phase 2 · sequential cross-validation ↓
              </div>
              <div style={{ display: "flex", gap: 14 }}>
                {[{ name: "Reviewer Agent", color: "--reviewer", meta: "OpenAI Agents SDK · :8006" }].map((a) => (
                  <div key={a.name} className="card" style={{ flex: 1, padding: "16px 18px", maxWidth: "calc(25% - 10.5px)" }}>
                    <div style={{ height: 3, borderRadius: 2, background: `var(${a.color})`, marginBottom: 11 }} />
                    <div style={{ fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", gap: 8 }}>
                      <span className="swatch" style={{ background: `var(${a.color})` }} />{a.name}
                    </div>
                    <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 5 }}>{a.meta}</div>
                  </div>
                ))}
              </div>
              <div className="card" style={{ textAlign: "center", background: "var(--ivory)", borderStyle: "dashed", padding: 9, fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-muted)" }}>
                MCP (Model Context Protocol) · SSE · multi-tier TTL cache ↓
              </div>
              <div className="card" style={{ padding: "16px 18px", borderColor: "var(--mcp)", background: "var(--mcp-bg)" }}>
                <div style={{ fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", gap: 8 }}>
                  <span className="swatch" style={{ background: "var(--mcp)" }} />Unified finsight-mcp Server
                  <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)", fontWeight: 400 }}>· :8010</span>
                </div>
                <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 5 }}>
                  get_prices · get_financials · get_company_filings · get_news_sentiment · get_peers · find_agent
                </div>
              </div>
            </div>
          </section>

          {/* Features */}
          <h2 style={{ fontSize: 26, marginBottom: 20, marginTop: 50 }}>What makes it sturdy</h2>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16 }}>
            {[
              { n: "01", t: "Multi-tier caching", d: "TTL tool cache (1 min prices → 7 day shocks), LangChain SQLite LLM cache, and ChromaDB semantic cache." },
              { n: "02", t: "Input & output guardrails", d: "Off-topic filter, pre-flight ticker validation, empty-response guard, and BUY/HOLD/SELL signal enforcement." },
              { n: "03", t: "Persistent memory", d: "SQLite ticker briefs, portfolio holdings, and recommendation tracking with live price snapshots." },
              { n: "04", t: "Distributed tracing", d: "Langfuse joins all five agent processes into one trace tree via A2A context propagation." },
              { n: "05", t: "Runtime RAGAS eval", d: "Per-query Faithfulness, FactualCorrectness and relevancy scored in the background." },
              { n: "06", t: "Local-first inference", d: "Every agent runs on LM Studio (OpenAI-compatible) with local embeddings — no cloud dependency." },
            ].map((f) => (
              <div key={f.n} className="card" style={{ padding: 20 }}>
                <div className="mono" style={{ fontSize: 11, color: "var(--clay)", fontWeight: 600 }}>{f.n}</div>
                <h4 style={{ fontSize: 14, marginBottom: 7 }}>{f.t}</h4>
                <p style={{ fontSize: 12.5, lineHeight: 1.55, color: "var(--text-secondary)", margin: 0 }}>{f.d}</p>
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}
