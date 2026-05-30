"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

interface LfTrace {
  id: string; name?: string; input?: unknown; output?: unknown;
  timestamp?: string; latency?: number; sessionId?: string;
}

interface LfObs {
  id: string; name?: string; type?: string; startTime?: string; endTime?: string;
  latency?: number; model?: string; level?: string;
  parentObservationId?: string | null;
  input?: unknown; output?: unknown;
}

interface TreeNode extends LfObs { children: TreeNode[]; depth: number; }

export default function TracePage() {
  return (
    <Suspense fallback={<div className="topbar"><div><h1>Trace Inspector</h1></div></div>}>
      <TraceContent />
    </Suspense>
  );
}

function color(name: string) {
  const n = (name || "").toLowerCase();
  if (n.includes("rag") || n.includes("llamaindex") || n.includes("filing")) return "var(--rag)";
  if (n.includes("quant") || n.includes("langgraph") || n.includes("stress") || n.includes("dcf")) return "var(--quant)";
  if (n.includes("market") || n.includes("crewai") || n.includes("peer") || n.includes("macro")) return "var(--market)";
  if (n.includes("mcp") || n.includes("get_price") || n.includes("get_financial") || n.includes("get_news")) return "var(--mcp)";
  return "var(--clay)";
}

function bg(name: string) {
  const n = (name || "").toLowerCase();
  if (n.includes("rag") || n.includes("llamaindex")) return "var(--rag-bg)";
  if (n.includes("quant") || n.includes("langgraph")) return "var(--quant-bg)";
  if (n.includes("market") || n.includes("crewai")) return "var(--market-bg)";
  if (n.includes("mcp")) return "var(--mcp-bg)";
  return "var(--orch-bg)";
}

function ms(v?: number) {
  if (!v && v !== 0) return "—";
  return v < 1000 ? `${Math.round(v)}ms` : `${(v / 1000).toFixed(1)}s`;
}

function buildTree(obs: LfObs[]): TreeNode[] {
  const map = new Map<string, TreeNode>();
  for (const o of obs) map.set(o.id, { ...o, children: [], depth: 0 });
  const roots: TreeNode[] = [];
  for (const node of map.values()) {
    if (node.parentObservationId && map.has(node.parentObservationId)) {
      map.get(node.parentObservationId)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  function setDepth(nodes: TreeNode[], d: number) {
    for (const n of nodes) { n.depth = d; setDepth(n.children, d + 1); }
  }
  setDepth(roots, 0);
  roots.sort((a, b) => (a.startTime || "").localeCompare(b.startTime || ""));
  return roots;
}

function flattenTree(nodes: TreeNode[]): TreeNode[] {
  const out: TreeNode[] = [];
  function walk(list: TreeNode[]) {
    const sorted = [...list].sort((a, b) => (a.startTime || "").localeCompare(b.startTime || ""));
    for (const n of sorted) { out.push(n); walk(n.children); }
  }
  walk(nodes);
  return out;
}

function TraceContent() {
  const searchParams = useSearchParams();
  const urlTraceId = searchParams.get("traceId") || null;

  const [traces, setTraces] = useState<LfTrace[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(urlTraceId);
  const [trace, setTrace] = useState<LfTrace | null>(null);
  const [obs, setObs] = useState<LfObs[]>([]);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/traces")
      .then((r) => r.json())
      .then((d) => {
        if (d.error) { setError(d.error); setLoading(false); return; }
        setTraces(d.traces || []);
        setLoading(false);
        if (urlTraceId) loadTrace(urlTraceId);
      })
      .catch((e) => { setError(e.message); setLoading(false); });
  }, [urlTraceId]);

  const loadTrace = async (id: string) => {
    setSelectedId(id);
    setDetailLoading(true);
    setError(null);
    try {
      const r = await fetch(`/api/traces?traceId=${id}`);
      const d = await r.json();
      if (d.error) { setError(d.error); setDetailLoading(false); return; }
      setTrace(d.trace || null);
      setObs(d.observations || []);
    } catch (e) { setError(String(e)); setObs([]); }
    setDetailLoading(false);
  };

  const tree = buildTree(obs);
  const flat = flattenTree(tree);
  const signal = (() => {
    const o = trace?.output;
    const s = typeof o === "string" ? o : JSON.stringify(o || "");
    return s.match(/\b(BUY|HOLD|SELL)\b/)?.[1] || null;
  })();
  const query = (() => {
    const inp = trace?.input;
    if (typeof inp === "string") return inp.slice(0, 100);
    if (inp && typeof inp === "object") {
      const obj = inp as Record<string, unknown>;
      if (typeof obj.query === "string") return obj.query.slice(0, 100);
    }
    return trace?.name || "";
  })();

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Trace Inspector</h1>
          <div className="sub">Langfuse traces · full agent chain with nested spans</div>
        </div>
        <div style={{ marginLeft: "auto" }} />
        {selectedId && <span className="pill mono">trace · {selectedId.slice(0, 10)}…</span>}
      </div>
      <div className="scroll">
        <div style={{ maxWidth: 1120, margin: "0 auto", padding: "24px 30px 60px" }}>
          {error && (
            <div className="card" style={{ padding: 14, marginBottom: 20, color: "var(--sell)", fontSize: 13 }}>
              {error}
            </div>
          )}
          <div style={{ display: "grid", gridTemplateColumns: "280px 1fr", gap: 24 }}>

            {/* ── Trace list ──────────────────── */}
            <div>
              <h2 style={{ fontSize: 17, marginBottom: 14 }}>Recent traces</h2>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {loading && <div style={{ color: "var(--text-muted)", fontSize: 12 }}>Loading…</div>}
                {traces.map((t, i) => (
                  <button
                    key={`t-${i}`}
                    onClick={() => loadTrace(t.id)}
                    className="card"
                    style={{
                      padding: "12px 14px", textAlign: "left", cursor: "pointer",
                      border: selectedId === t.id ? "1.5px solid var(--clay)" : undefined,
                      background: selectedId === t.id ? "var(--orch-bg)" : undefined,
                    }}
                  >
                    <div style={{ fontSize: 12, fontWeight: 600, color: "var(--clay-deep)" }}>
                      {t.name || "trace"}
                    </div>
                    <div className="mono" style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                      {t.id.slice(0, 10)}… · {ms(t.latency)}
                    </div>
                    <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                      {t.timestamp?.split("T")[0] || ""}
                    </div>
                  </button>
                ))}
                {!loading && traces.length === 0 && !error && (
                  <div style={{ fontSize: 12, color: "var(--text-muted)", padding: 16 }}>No traces found</div>
                )}
              </div>
            </div>

            {/* ── Trace detail ────────────────── */}
            <div>
              {!selectedId ? (
                <div style={{ color: "var(--text-muted)", fontSize: 13, padding: 60, textAlign: "center" }}>
                  Select a trace to inspect its span chain
                </div>
              ) : detailLoading ? (
                <div style={{ color: "var(--text-muted)", fontSize: 13, padding: 40, textAlign: "center" }}>Loading spans…</div>
              ) : (
                <>
                  {/* Header */}
                  <div className="card" style={{ padding: "18px 22px", display: "flex", alignItems: "center", gap: 22, marginBottom: 22 }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>QUERY</div>
                      <div style={{ fontSize: 16, fontFamily: "var(--serif)" }}>
                        {query ? `"${query}"` : "—"}
                      </div>
                      <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
                        {trace?.name} · {selectedId?.slice(0, 16)}…
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: 20 }}>
                      <div style={{ textAlign: "center" }}>
                        <div style={{ fontFamily: "var(--serif)", fontSize: 22, fontWeight: 700 }}>{ms(trace?.latency)}</div>
                        <div style={{ fontSize: 10.5, color: "var(--text-muted)", marginTop: 2 }}>Total latency</div>
                      </div>
                      <div style={{ textAlign: "center" }}>
                        <div style={{ fontFamily: "var(--serif)", fontSize: 22, fontWeight: 700 }}>{flat.length}</div>
                        <div style={{ fontSize: 10.5, color: "var(--text-muted)", marginTop: 2 }}>Spans</div>
                      </div>
                      {signal && (
                        <div style={{ textAlign: "center" }}>
                          <div style={{ fontFamily: "var(--serif)", fontSize: 22, fontWeight: 700,
                            color: signal === "BUY" ? "var(--quant)" : signal === "SELL" ? "var(--sell)" : "var(--hold)" }}>
                            {signal}
                          </div>
                          <div style={{ fontSize: 10.5, color: "var(--text-muted)", marginTop: 2 }}>Outcome</div>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Span tree */}
                  <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 14 }}>
                    <h2 style={{ fontSize: 17 }}>Span chain</h2>
                    <span style={{ fontSize: 12, color: "var(--text-muted)" }}>nested by parent → child</span>
                  </div>
                  <div className="card" style={{ padding: "8px 0", maxHeight: 600, overflowY: "auto" }}>
                    {flat.map((n, i) => {
                      const c = color(n.name || "");
                      const b = bg(n.name || "");
                      const isGen = n.type === "GENERATION";
                      const tag = isGen ? "LLM" : n.type === "SPAN" ? "SPAN" : n.type || "?";
                      const indent = n.depth * 20;

                      return (
                        <div
                          key={`n-${i}`}
                          style={{
                            display: "flex", alignItems: "center", gap: 8,
                            padding: "7px 14px 7px 14px",
                            paddingLeft: 14 + indent,
                            borderBottom: i < flat.length - 1 ? "1px solid var(--sand)" : undefined,
                          }}
                        >
                          {/* Depth connector */}
                          {n.depth > 0 && (
                            <span style={{ color: "var(--sand-deep)", fontSize: 10, marginRight: -4 }}>└</span>
                          )}
                          {/* Dot */}
                          <span style={{ width: 8, height: 8, borderRadius: "50%", background: c, flexShrink: 0 }} />
                          {/* Tag */}
                          <span className="ev-tag" style={{ background: b, color: c }}>{tag}</span>
                          {/* Name */}
                          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)", flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {n.name || "—"}
                          </span>
                          {/* Model */}
                          {n.model && (
                            <span className="mono" style={{ fontSize: 10, color: "var(--text-muted)", flexShrink: 0 }}>{n.model}</span>
                          )}
                          {/* Latency */}
                          <span className="mono" style={{ fontSize: 10, color: "var(--text-muted)", flexShrink: 0, width: 50, textAlign: "right" }}>
                            {ms(n.latency)}
                          </span>
                          {/* Level */}
                          {n.level && n.level !== "DEFAULT" && (
                            <span style={{ fontSize: 9, color: n.level === "ERROR" ? "var(--sell)" : "var(--hold)", fontWeight: 600 }}>{n.level}</span>
                          )}
                        </div>
                      );
                    })}
                    {flat.length === 0 && (
                      <div style={{ textAlign: "center", padding: 24, color: "var(--text-muted)", fontSize: 12 }}>No spans</div>
                    )}
                  </div>

                  {/* Legend */}
                  <div style={{ display: "flex", gap: 16, padding: "14px 0", flexWrap: "wrap" }}>
                    {[
                      { label: "Orchestrator", c: "var(--clay)" },
                      { label: "RAG", c: "var(--rag)" },
                      { label: "Quant", c: "var(--quant)" },
                      { label: "Market Context", c: "var(--market)" },
                      { label: "MCP tool", c: "var(--mcp)" },
                    ].map((l) => (
                      <div key={l.label} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11.5, color: "var(--text-secondary)" }}>
                        <span style={{ width: 11, height: 11, borderRadius: 3, background: l.c }} />
                        {l.label}
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
