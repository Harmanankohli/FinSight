/** Memory page displaying stored investment briefs with search, expand, and download capabilities. */
"use client";

import { Suspense, useEffect, useState, FormEvent } from "react";
import { useSearchParams } from "next/navigation";

/** Downloads a brief report by its database ID in the requested format. */
async function downloadReportById(briefId: string, ticker: string, format: "html" | "pdf") {
  const res = await fetch(`/api/orch/api/reports/${briefId}/${format}`);
  if (!res.ok) return;
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `FinSight_${ticker}_${new Date().toISOString().slice(0, 10)}.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}

interface Brief {
  id: string; ticker: string; recommendation: string; confidence: number;
  brief_json: string; created_at: string; analysis_date: string;
}

/** Memory page component wrapped in Suspense. Renders search form and brief cards. */
export default function MemoryPage() {
  return (
    <Suspense fallback={<div className="topbar"><div><h1>Memory</h1></div></div>}>
      <MemoryContent />
    </Suspense>
  );
}

function MemoryContent() {
  const searchParams = useSearchParams();
  const urlTicker = searchParams.get("ticker")?.toUpperCase() || "";
  const [briefs, setBriefs] = useState<Brief[]>([]);
  const [input, setInput] = useState(urlTicker);
  const [searchedTicker, setSearchedTicker] = useState("all");
  const [loading, setLoading] = useState(!!urlTicker);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [downloading, setDownloading] = useState<string | null>(null); // "{id}-{format}"

  const handleDownload = async (e: React.MouseEvent, b: Brief, format: "html" | "pdf") => {
    e.stopPropagation();
    const key = `${b.id}-${format}`;
    setDownloading(key);
    await downloadReportById(b.id, b.ticker, format);
    setDownloading(null);
  };

  const fetchBriefs = async (sym: string) => {
    setLoading(true);
    setSearchedTicker(sym.toUpperCase());
    try {
      const r = await fetch(`/api/orch/api/memory/ticker/${sym.toUpperCase()}`);
      if (r.ok) setBriefs(await r.json());
      else setBriefs([]);
    } catch { setBriefs([]); }
    setLoading(false);
  };

  useEffect(() => {
    if (!urlTicker) return;
    const controller = new AbortController();
    const { signal } = controller;
    fetch(`/api/orch/api/memory/ticker/${urlTicker}`, { signal })
      .then((r) => r.ok ? r.json() : [])
      .then((data) => { if (!signal.aborted) { setSearchedTicker(urlTicker); setBriefs(data); } })
      .catch(() => { if (!signal.aborted) setBriefs([]); })
      .finally(() => { if (!signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [urlTicker]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (input.trim()) fetchBriefs(input.trim());
  };

  const toggleExpand = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const parseText = (b: Brief): string => {
    try {
      const raw = b.brief_json;
      const data = typeof raw === "string" ? JSON.parse(raw) : raw;
      return data?.response_text || "";
    } catch { return ""; }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Memory</h1>
          <div className="sub">Persistent briefs, portfolio, and recommendation history · SQLite</div>
        </div>
        <div style={{ marginLeft: "auto" }} />
        <span className="pill mono">{loading ? "…" : `${briefs.length} brief(s)`}</span>
      </div>
      <div className="scroll">
        <div style={{ maxWidth: 1060, margin: "0 auto", padding: "24px 30px 60px" }}>
          {/* Search */}
          <form onSubmit={handleSubmit} style={{ display: "flex", gap: 8, marginBottom: 24, alignItems: "center" }}>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value.toUpperCase())}
              className="mono"
              style={{ padding: "8px 14px", borderRadius: 8, border: "1px solid var(--sand)", background: "#fff", fontSize: 14, fontWeight: 700, width: 120, color: "var(--clay-deep)" }}
              placeholder="TICKER"
            />
            <button type="submit" className="pill" style={{ cursor: "pointer", padding: "8px 16px" }}>Search</button>
          </form>

          {/* Brief cards */}
          <div style={{ display: "grid", gap: 13 }}>
            {briefs.map((b) => {
              const text = parseText(b);
              const isOpen = expanded.has(b.id);
              const preview = text.slice(0, 200);
              const signal = extractSignal(b.recommendation);

              return (
                <div
                  key={b.id}
                  className="card"
                  style={{ padding: 0, cursor: "pointer", transition: "border-color 0.15s" }}
                  onClick={() => toggleExpand(b.id)}
                >
                  {/* Header row — always visible */}
                  <div style={{ display: "flex", gap: 18, alignItems: "center", padding: "16px 20px" }}>
                    <div style={{ flexShrink: 0, width: 70 }}>
                      <div className="mono" style={{ fontSize: 18, fontWeight: 700, color: "var(--clay-deep)" }}>{b.ticker}</div>
                      <div style={{ fontSize: 10.5, color: "var(--text-muted)", marginTop: 2 }}>
                        {b.analysis_date || b.created_at?.split("T")[0]}
                      </div>
                    </div>
                    <div style={{ width: 1, height: 36, background: "var(--sand)", flexShrink: 0 }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <p style={{ fontSize: 12.5, lineHeight: 1.5, color: "var(--text-secondary)", margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: isOpen ? undefined : "nowrap" }}>
                        {isOpen ? "" : (preview || "No summary")}
                      </p>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
                      <span className={`signal ${signal}`} style={{ padding: "3px 11px", fontSize: 11 }}>
                        {b.recommendation}
                      </span>
                      <span className="mono" style={{ fontSize: 10, color: "var(--text-muted)" }}>
                        {b.confidence?.toFixed(2)}
                      </span>
                      {(["html", "pdf"] as const).map((fmt) => {
                        const key = `${b.id}-${fmt}`;
                        return (
                          <button
                            key={fmt}
                            onClick={(e) => handleDownload(e, b, fmt)}
                            disabled={downloading !== null}
                            title={`Download ${fmt.toUpperCase()}`}
                            style={{
                              padding: "3px 8px", borderRadius: 4, cursor: downloading ? "not-allowed" : "pointer",
                              border: "1px solid var(--sand)", background: "var(--ivory-dark)",
                              fontSize: 10, fontWeight: 600, color: "var(--clay)",
                              fontFamily: "var(--font-sans, system-ui)",
                              opacity: downloading && downloading !== key ? 0.5 : 1,
                            }}
                          >
                            {downloading === key ? "…" : fmt.toUpperCase()}
                          </button>
                        );
                      })}
                      <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{isOpen ? "▲" : "▼"}</span>
                    </div>
                  </div>

                  {/* Expanded content */}
                  {isOpen && text && (
                    <div style={{ padding: "0 20px 18px", borderTop: "1px solid var(--sand)" }}>
                      <div style={{ fontSize: 13, lineHeight: 1.62, color: "var(--text-primary)", paddingTop: 14, whiteSpace: "pre-wrap" }}>
                        {text}
                      </div>
                      <div className="mono" style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 12, paddingTop: 10, borderTop: "1px solid var(--sand)" }}>
                        session: {b.id?.slice(0, 12)} · conf: {b.confidence?.toFixed(2)} · {b.created_at}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
            {!loading && briefs.length === 0 && (
              <div style={{ textAlign: "center", padding: 40, color: "var(--text-muted)", fontSize: 13 }}>
                {searchedTicker === "all" ? "Search for a ticker to view stored briefs." : `No briefs for ${searchedTicker}.`}
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

function extractSignal(rec: string): string {
  if (rec === "BUY") return "buy";
  if (rec === "SELL") return "sell";
  return "hold";
}
