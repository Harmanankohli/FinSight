/** Research chat page with CopilotKit agent orchestration, agent tiles, and AG-UI trace rail. */
"use client";

import { useRef, useState, FormEvent, useEffect } from "react";
import { useCoAgent, useCopilotChatHeadless_c } from "@copilotkit/react-core";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { addRecentQuery } from "@/lib/recentQueries";

/** Downloads a research report for the given ticker in the requested format. */
async function downloadReport(ticker: string, format: "html" | "pdf") {
  const res = await fetch(`/api/orch/api/reports/ticker/${ticker}/latest/${format}`);
  if (!res.ok) return;
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `FinSight_${ticker}_${new Date().toISOString().slice(0, 10)}.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}

interface FinsightState {
  active_agents?: string[];
  active_agent?: string | null;
  ticker?: string | null;
}

const AGENTS = [
  { key: "rag", name: "Financial RAG", sub: "LlamaIndex · filings", color: "--rag", match: ["financial rag", "rag agent"], phase: 1 },
  { key: "quant", name: "Quant Analysis", sub: "LangGraph · metrics", color: "--quant", match: ["quant", "quant analysis"], phase: 1 },
  { key: "market", name: "Market Context", sub: "CrewAI · macro + peers", color: "--market", match: ["market context", "sentiment"], phase: 1 },
  { key: "analytics", name: "Analytics", sub: "PydanticAI · trends", color: "--analytics", match: ["analytics"], phase: 1 },
  { key: "reviewer", name: "Reviewer", sub: "OpenAI SDK · validation", color: "--reviewer", match: ["reviewer"], phase: 2 },
] as const;

/** Returns the display status ("working", "done", or "idle") for an agent tile based on active agents. */
function tileStatus(cfg: typeof AGENTS[number], active: string[], running: boolean) {
  if (active.some((a) => cfg.match.some((m) => a.toLowerCase().includes(m)))) {
    return running ? "working" : "done";
  }
  if (active.length > 0 && cfg.phase === 1) return running ? "working" : "done";
  return "idle";
}

/** Extracts a BUY/HOLD/SELL signal from the assistant's response text. */
function extractSignal(text: string): { signal: string; cls: string } | null {
  const m = text.match(/\b(BUY|HOLD|SELL)\b/);
  if (!m) return null;
  return { signal: m[1], cls: m[1].toLowerCase() };
}

/** Main research page. Renders CopilotKit chat composer, agent status tiles, orchestrator progress bar, and AG-UI event trail. */
export default function ResearchPage() {
  const { state, running } = useCoAgent<FinsightState>({ name: "finsight" });
  const { messages, sendMessage, isLoading } = useCopilotChatHeadless_c();
  const [input, setInput] = useState("");
  const [downloading, setDownloading] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const activeAgents = state?.active_agents ?? [];
  const anyActive = activeAgents.length > 0;

  const handleDownload = async (format: "html" | "pdf") => {
    if (!state?.ticker) return;
    setDownloading(format);
    await downloadReport(state.ticker, format);
    setDownloading(null);
  };

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, isLoading]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;
    const text = input.trim();
    addRecentQuery(text, crypto.randomUUID());
    sendMessage({ id: crypto.randomUUID(), role: "user", content: text });
    setInput("");
  };

  const orchStep = (label: string, status: "idle" | "active" | "done", detail?: string) => (
    <div className={`step ${status}`} key={label}>
      <span className="glyph">{status === "done" ? "●" : status === "active" ? "◎" : "○"}</span>
      <div>
        <div className="lbl">{label}</div>
        {detail && <div className="det">{detail}</div>}
      </div>
      {status === "active" && <span className="bounce-dots" style={{ marginLeft: "auto" }}><i /><i /><i /></span>}
    </div>
  );

  const hasMessages = messages.length > 0;
  const showOrch = running || hasMessages;

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Investment Research</h1>
          <div className="sub">Ask about any stock for a BUY / HOLD / SELL brief</div>
        </div>
        <div style={{ marginLeft: "auto" }} />
        {running && (
          <span className="pill" style={{ background: "var(--orch-bg)", borderColor: "var(--clay-light)", color: "var(--clay-deep)" }}>
            <span className="status-dot pulse" style={{ background: "var(--clay)", width: 7, height: 7 }} /> Running…
          </span>
        )}
        {!running && hasMessages && (
          <span className="pill" style={{ background: "var(--quant-bg)", borderColor: "var(--quant)", color: "var(--quant)" }}>
            ● Complete
          </span>
        )}
      </div>

      <div className="research">
        <div className="chatcol">

          {/* ── Orchestrator strip ──────────────────────── */}
          {showOrch && (
            <div className="runbar">
              {orchStep("Connect", hasMessages ? "done" : "idle", hasMessages ? "5 agents" : undefined)}
              {orchStep("Delegate", anyActive ? "active" : hasMessages ? "done" : "idle",
                anyActive ? `parallel · ${activeAgents.length} agents` : hasMessages ? "send_message ×5" : undefined)}
              {orchStep("Synthesize", running && !anyActive ? "active" : !running && hasMessages ? "done" : "idle",
                !running && hasMessages ? extractSignal(messages.filter(m => m.role === "assistant").pop()?.content as string || "")?.signal : undefined)}
              {orchStep("Save", !running && hasMessages ? "done" : "idle",
                !running && hasMessages ? "brief saved" : undefined)}
            </div>
          )}

          {/* ── Agent tiles ────────────────────────────── */}
          {(running || (hasMessages && anyActive)) && (
            <div className="tiles">
              {AGENTS.map((a) => {
                const s = tileStatus(a, activeAgents, running);
                const active = s === "working" || s === "done";
                return (
                  <div key={a.key} className={`tile ${active ? a.key : ""} ${s}`}>
                    <div className="th">
                      <div style={{ minWidth: 0 }}>
                        <div className="tname" style={{ color: active ? `var(${a.color})` : undefined }}>{a.name}</div>
                        <div className="tsub">{a.sub}</div>
                      </div>
                      {s === "done" && <span className="check" style={{ color: `var(${a.color})` }}>✓</span>}
                      {s === "working" && <span className="bounce-dots" style={{ marginLeft: "auto" }}><i /><i /><i /></span>}
                    </div>
                    {s === "working" && <div className="tbody">Analyzing…</div>}
                  </div>
                );
              })}
            </div>
          )}

          {/* ── Chat thread ────────────────────────────── */}
          <div className="thread" ref={threadRef}>
            {!hasMessages && !running && (
              <div style={{ textAlign: "center", paddingTop: 100 }}>
                <h2 style={{ fontSize: 24, marginBottom: 8 }}>Investment Research</h2>
                <p style={{ color: "var(--text-muted)", fontSize: 14, maxWidth: "40ch", margin: "0 auto" }}>
                  Ask about any stock for a citation-backed BUY / HOLD / SELL brief from five specialized agents.
                </p>
              </div>
            )}

            {messages.map((m, idx) => {
              if (m.role === "user") {
                const text = typeof m.content === "string" ? m.content : "";
                return (
                  <div key={`msg-${idx}`} className="turn">
                    <div className="msg-user">
                      <div className="bubble-user">{text}</div>
                    </div>
                  </div>
                );
              }
              if (m.role === "assistant") {
                const text = typeof m.content === "string" ? m.content : "";
                if (!text) return null;
                const sig = extractSignal(text);
                const isLast = idx === messages.length - 1 || messages.slice(idx + 1).every(mm => mm.role !== "assistant");
                const showDownload = sig && !running && isLast && state?.ticker;
                return (
                  <div key={`msg-${idx}`} className="turn">
                    <div className="msg-asst">
                      <div className="ava"><span /></div>
                      <div className="asst-body">
                        <div className="asst-name">Investment Orchestrator</div>
                        <div className="synth">
                          {sig && (
                            <div className="shead">
                              <span className={`signal ${sig.cls}`}>{sig.signal}</span>
                              <span className="stitle">Investment Recommendation</span>
                            </div>
                          )}
                          <div style={{ fontSize: 13.5, lineHeight: 1.62 }}>
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
                          </div>
                        </div>
                        {showDownload && (
                          <div style={{
                            display: "flex", alignItems: "center", gap: 10, marginTop: 14,
                            padding: "10px 14px", borderRadius: 8,
                            background: "var(--ivory-dark)", border: "1px solid var(--sand)",
                          }}>
                            <span style={{ fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-sans, system-ui)" }}>
                              Export report:
                            </span>
                            {(["html", "pdf"] as const).map((fmt) => (
                              <button
                                key={fmt}
                                onClick={() => handleDownload(fmt)}
                                disabled={downloading !== null}
                                style={{
                                  padding: "5px 14px", borderRadius: 6, cursor: downloading ? "not-allowed" : "pointer",
                                  border: fmt === "html" ? "1px solid var(--clay)" : "1px solid var(--clay-light)",
                                  background: fmt === "html" ? "var(--clay)" : "white",
                                  color: fmt === "html" ? "white" : "var(--clay-deep)",
                                  fontSize: 12, fontWeight: 600,
                                  fontFamily: "var(--font-sans, system-ui)",
                                  opacity: downloading && downloading !== fmt ? 0.5 : 1,
                                }}
                              >
                                {downloading === fmt ? "…" : `⬇ ${fmt.toUpperCase()}`}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                );
              }
              return null;
            })}

            {isLoading && (
              <div className="turn">
                <div className="msg-asst">
                  <div className="ava"><span /></div>
                  <div className="asst-body">
                    <div className="asst-name">Investment Orchestrator</div>
                    <div style={{ color: "var(--text-muted)", fontSize: 13 }}>
                      <span className="bounce-dots"><i /><i /><i /></span>{" "}
                      {anyActive ? "Agents working…" : "Synthesizing…"}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* ── Composer ───────────────────────────────── */}
          <div className="composer">
            {!hasMessages && (
              <div style={{ display: "flex", gap: 8, marginBottom: 11, flexWrap: "wrap" }}>
                {[
                  "Should I invest in NVIDIA?",
                  "Analyze AAPL for a long hold",
                  "TSLA vs auto peers",
                  "Is MSFT a buy?",
                ].map((s) => (
                  <button
                    key={s}
                    onClick={() => { setInput(s); inputRef.current?.focus(); }}
                    className="sugg"
                    style={{
                      fontSize: 12, padding: "6px 13px", borderRadius: 999, cursor: "pointer",
                      border: "1px solid var(--sand)", background: "var(--ivory-dark)", color: "var(--text-secondary)",
                    }}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
            <form onSubmit={handleSubmit}>
              <div className="inputbox">
                <input
                  ref={inputRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder='Ask about any stock — e.g. "Analyze NVDA" or "Should I buy AAPL?"'
                  disabled={isLoading}
                />
                <button type="submit" className="send-btn" disabled={isLoading || !input.trim()}>
                  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2}><path d="M5 12h14M13 6l6 6-6 6" /></svg>
                </button>
              </div>
            </form>
          </div>
        </div>

        {/* ── Trace rail ───────────────────────────────── */}
        <div className="trail">
          <div className="trail-head">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--clay)" strokeWidth={2}><path d="M4 4v16M4 8h7M4 14h11M4 20h5" /></svg>
            <span className="tt">AG-UI events</span>
            <span className="tc">{hasMessages ? `${messages.filter(m => m.role === "user" || m.role === "assistant" || m.role === "tool").length} events` : "—"}</span>
          </div>
          <div className="trail-body">
            {!hasMessages ? (
              <div style={{ textAlign: "center", padding: "40px 0", color: "var(--text-muted)", fontSize: 12 }}>
                Events appear here during a run
              </div>
            ) : (
              (() => {
                const seen = new Set<string>();
                return messages
                  .filter((m) => {
                    if (m.role !== "user" && m.role !== "assistant" && m.role !== "tool") return false;
                    const key = `${m.role}-${m.id}-${typeof m.content === "string" ? m.content.slice(0, 50) : ""}`;
                    if (seen.has(key)) return false;
                    seen.add(key);
                    return true;
                  })
                  .map((m, i) => {
                    const isUser = m.role === "user";
                    const isAsst = m.role === "assistant";
                    const isTool = m.role === "tool";
                    const text = typeof m.content === "string" ? m.content : "";
                    const tag = isUser ? "USER" : isAsst ? "ASST" : isTool ? "TOOL" : "SYS";
                    const tagCls = isUser ? "call" : isAsst ? "res" : isTool ? "state" : "state";

                    let label = text.slice(0, 60);
                    if (isTool) {
                      try {
                        const parsed = JSON.parse(text);
                        const result = typeof parsed.result === "string" ? parsed.result : JSON.stringify(parsed.result);
                        label = result.slice(0, 60);
                      } catch { label = text.slice(0, 60); }
                    }

                    return (
                      <div className="ev" key={`trail-${i}`}>
                        <div className="ev-h">
                          <span className={`ev-tag ${tagCls}`}>{tag}</span>
                          <span className="ev-name">{label || "…"}{(text.length > 60 || label.length >= 60) ? "…" : ""}</span>
                        </div>
                      </div>
                    );
                  });
              })()
            )}
          </div>
        </div>
      </div>
    </>
  );
}
