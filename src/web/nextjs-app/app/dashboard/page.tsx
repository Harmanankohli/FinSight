/** Dashboard page displaying observability metrics, agent performance KPIs, and RAGAS quality scores from Langfuse. */
"use client";

import { Suspense, useEffect, useState, useRef, useSyncExternalStore } from "react";
import { LineChart, Line, AreaChart, Area, BarChart, Bar, Cell, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { AGENT_COLOR, type AgentKey } from "@/lib/agentColors";

function ms(v?: number | null) {
  if (v == null) return "\u2014";
  return v < 1000 ? `${Math.round(v)}ms` : `${(v / 1000).toFixed(1)}s`;
}

function tokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1e3).toFixed(1)}K`;
  return String(n);
}

function relativeTime(ts: string): string {
  if (!ts) return "";
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

interface DashboardData {
  hours: number; truncated: boolean;
  kpi: {
    totalTraces: number; errorRate: number; errorCount: number;
    avgLatency: number; p50Latency: number; p95Latency: number; p99Latency: number;
    totalTokens: number; promptTokens: number; completionTokens: number;
    avgTfft: number | null;
  };
  agentBreakdown: Array<{
    agent: string; traceCount: number; avgLatency: number; errorCount: number;
    totalTokens: number; promptTokens: number; completionTokens: number;
  }>;
  timeSeries: Array<{
    bucket: string; traceCount: number; avgLatency: number; errorCount: number;
    totalTokens: number; promptTokens: number; completionTokens: number;
  }>;
}

interface ScoresData {
  byAgent: Record<string, {
    metrics: Record<string, {
      avg: number; min: number; max: number; count: number; recent: number;
      scale: "0-1" | "1-5";
    }>;
    overallAvg: number;
  }>;
  recentScores: Array<{ name: string; value: number; traceId: string; timestamp: string }>;
}

function Skeleton({ h = 20, w = "100%" }: { h?: number; w?: string | number }) {
  return <div className="pulse" style={{ height: h, width: w, background: "var(--sand)", borderRadius: 6 }} />;
}

function LoadingSkeleton() {
  return (
    <>
      <div className="kpi-grid" style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 14, marginBottom: 24 }}>
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="card" style={{ padding: "18px 20px" }}>
            <Skeleton h={28} w={80} />
            <Skeleton h={12} w={60} />
            <Skeleton h={11} w="70%" />
          </div>
        ))}
      </div>
      <div className="chart-grid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, marginBottom: 24 }}>
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="card" style={{ padding: "18px 22px" }}>
            <Skeleton h={16} w={120} />
            <Skeleton h={240} />
          </div>
        ))}
      </div>
    </>
  );
}

interface TooltipEntry {
  name: string;
  value: number | string;
  color?: string;
}

function ChartTooltip({ active, payload, label }: { active?: boolean; payload?: TooltipEntry[]; label?: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div style={{
      background: "#fff", border: "1px solid var(--sand)", borderRadius: 6,
      padding: "8px 12px", fontSize: 12, fontFamily: "var(--mono)",
    }}>
      <div style={{ color: "var(--text-muted)", marginBottom: 4 }}>{label}</div>
      {payload.map((p, i) => (
        <div key={i} style={{ color: p.color }}>{p.name}: {typeof p.value === "number" ? p.value.toLocaleString() : p.value}</div>
      ))}
    </div>
  );
}

interface FetchState {
  data: DashboardData | null;
  scores: ScoresData | null;
  loading: boolean;
  error: string | null;
  lastRefresh: Date;
}

const fetchStore = {
  snapshot: {
    data: null as DashboardData | null,
    scores: null as ScoresData | null,
    loading: true,
    error: null as string | null,
    lastRefresh: new Date(),
  } satisfies FetchState as FetchState,
  listeners: new Set<() => void>(),
  controller: null as AbortController | null,

  subscribe(cb: () => void) {
    this.listeners.add(cb);
    return () => { this.listeners.delete(cb); };
  },
  getSnapshot() {
    return this.snapshot;
  },
  update(partial: Partial<FetchState>) {
    this.snapshot = { ...this.snapshot, ...partial };
    queueMicrotask(() => { for (const cb of this.listeners) cb(); });
  },
  async fetch(h: number) {
    this.controller?.abort();
    const c = new AbortController();
    this.controller = c;
    this.update({ loading: true });
    try {
      const [dashRes, scoreRes] = await Promise.all([
        fetch(`/api/dashboard?hours=${h}`, { signal: c.signal }),
        fetch("/api/dashboard/scores", { signal: c.signal }),
      ]);
      if (!dashRes.ok || !scoreRes.ok) throw new Error("API error");
      const dashJson = await dashRes.json();
      const scoreJson = await scoreRes.json();
      if (dashJson.error) throw new Error(dashJson.error);
      if (scoreJson.error) throw new Error(scoreJson.error);
      this.update({ data: dashJson, scores: scoreJson, error: null, lastRefresh: new Date(), loading: false });
    } catch (e: unknown) {
      if (e instanceof Error && e.name !== "AbortError") {
        this.update({ error: e.message, loading: false });
      }
    }
  },
};

function DashboardContent() {
  const [hours, setHours] = useState(24);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [secondsAgo, setSecondsAgo] = useState(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const { data, scores, loading, error, lastRefresh } = useSyncExternalStore(
    fetchStore.subscribe.bind(fetchStore),
    fetchStore.getSnapshot.bind(fetchStore),
  );

  useEffect(() => {
    fetchStore.fetch(hours);
  }, [hours]);

  useEffect(() => {
    if (tickRef.current) clearInterval(tickRef.current);
    tickRef.current = setInterval(() => {
      setSecondsAgo(Math.floor((Date.now() - lastRefresh.getTime()) / 1000));
    }, 1000);
    return () => { if (tickRef.current) clearInterval(tickRef.current); };
  }, [lastRefresh]);

  useEffect(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    if (autoRefresh) {
      intervalRef.current = setInterval(() => {
        fetchStore.fetch(hours);
      }, 30000);
    }
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [autoRefresh, hours]);

  const kpi = data?.kpi;
  const errorColor = kpi ? (kpi.errorRate < 5 ? "var(--quant)" : kpi.errorRate <= 15 ? "var(--hold)" : "var(--sell)") : "var(--text-muted)";
  const tfftColor = kpi?.avgTfft != null ? "var(--mcp)" : "var(--text-muted)";
  const formatBucket = (b: string) => {
    const d = new Date(b);
    return hours <= 48
      ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString([], { month: "2-digit", day: "2-digit" });
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Dashboard</h1>
          <div className="sub">Observability metrics · agent performance · quality scores</div>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
          {secondsAgo > 0 && <span style={{ fontSize: 11, color: "var(--text-muted)" }}>Updated {secondsAgo}s ago</span>}
          <button
            onClick={() => setAutoRefresh(!autoRefresh)}
            className="pill"
            style={{
              cursor: "pointer",
              background: autoRefresh ? "var(--clay)" : undefined,
              color: autoRefresh ? "#fff" : undefined,
            }}
          >
            Auto
          </button>
          {([24, 168, 720] as const).map((h) => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className="pill"
              style={{
                cursor: "pointer",
                background: hours === h ? "var(--clay)" : undefined,
                color: hours === h ? "#fff" : undefined,
              }}
            >
              {h === 24 ? "24h" : h === 168 ? "7d" : "30d"}
            </button>
          ))}
        </div>
      </div>
      <div className="scroll">
        <div style={{ maxWidth: 1120, margin: "0 auto", padding: "24px 30px 60px" }}>
          {error && (
            <div className="card" style={{ padding: 14, marginBottom: 20, color: "var(--sell)", fontSize: 13 }}>
              {error}
            </div>
          )}

          {loading && !data ? <LoadingSkeleton /> : !data || !scores ? null : (
            <>
              {data.truncated && (
                <div className="card" style={{ padding: 10, marginBottom: 18, color: "var(--hold)", fontSize: 12, textAlign: "center" }}>
                  Showing metrics from the most recent 1000 traces
                </div>
              )}

              {data.kpi.totalTraces === 0 ? (
                <div className="card" style={{ padding: 60, textAlign: "center", color: "var(--text-muted)", fontSize: 14 }}>
                  No traces in the selected time range. <a href="/research" style={{ color: "var(--clay)" }}>Start a research query</a>
                </div>
              ) : (
                <>
                  {/* ── KPI Cards ── */}
                  <div className="kpi-grid" style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 14, marginBottom: 24 }}>
                    <div className="card" style={{ padding: "18px 20px" }}>
                      <div style={{ fontSize: 28, fontWeight: 700, fontFamily: "var(--serif)", color: errorColor }}>
                        {kpi!.errorRate.toFixed(1)}%
                      </div>
                      <div style={{ fontSize: 11, textTransform: "uppercase", color: "var(--text-muted)", marginTop: 2 }}>Error Rate</div>
                      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                        {kpi!.errorCount} errors in {kpi!.totalTraces} traces
                      </div>
                    </div>

                    <div className="card" style={{ padding: "18px 20px" }}>
                      <div style={{ fontSize: 28, fontWeight: 700, fontFamily: "var(--serif)" }}>
                        {ms(kpi!.p50Latency)}
                      </div>
                      <div style={{ fontSize: 11, textTransform: "uppercase", color: "var(--text-muted)", marginTop: 2 }}>P50 Latency</div>
                      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                        p95 {ms(kpi!.p95Latency)} · p99 {ms(kpi!.p99Latency)}
                      </div>
                    </div>

                    <div className="card" style={{ padding: "18px 20px" }}>
                      <div style={{ fontSize: 28, fontWeight: 700, fontFamily: "var(--serif)", color: "var(--analytics)" }}>
                        {tokens(kpi!.totalTokens)}
                      </div>
                      <div style={{ fontSize: 11, textTransform: "uppercase", color: "var(--text-muted)", marginTop: 2 }}>Tokens Used</div>
                      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                        {tokens(kpi!.promptTokens)} prompt · {tokens(kpi!.completionTokens)} completion
                      </div>
                    </div>

                    <div className="card" style={{ padding: "18px 20px" }}>
                      <div style={{ fontSize: 28, fontWeight: 700, fontFamily: "var(--serif)", color: tfftColor }}>
                        {kpi!.avgTfft != null ? ms(kpi!.avgTfft) : "\u2014"}
                      </div>
                      <div style={{ fontSize: 11, textTransform: "uppercase", color: "var(--text-muted)", marginTop: 2 }}>Avg TFFT</div>
                      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                        {kpi!.avgTfft != null ? "Time to First Token" : "Not available \u2014 requires streaming instrumentation"}
                      </div>
                    </div>

                    <div className="card" style={{ padding: "18px 20px" }}>
                      <div style={{ fontSize: 28, fontWeight: 700, fontFamily: "var(--serif)", color: "var(--clay)" }}>
                        {(kpi!.totalTraces / (data?.hours || 24)).toFixed(1)}
                      </div>
                      <div style={{ fontSize: 11, textTransform: "uppercase", color: "var(--text-muted)", marginTop: 2 }}>Throughput</div>
                      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                        {kpi!.totalTraces} traces in {data.hours}h
                      </div>
                    </div>
                  </div>

                  {/* ── Charts ── */}
                  {data.timeSeries.length > 1 ? (
                    <div className="chart-grid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, marginBottom: 24 }}>
                      {/* Latency over time */}
                      <div className="card" style={{ padding: "18px 22px" }}>
                        <h3 style={{ fontSize: 15, marginBottom: 14 }}>Latency Over Time</h3>
                        <ResponsiveContainer width="100%" height={240}>
                          <LineChart data={data.timeSeries}>
                            <XAxis dataKey="bucket" tickFormatter={formatBucket} tick={{ fontSize: 10, fill: "#888" }} />
                            <YAxis tick={{ fontSize: 10, fill: "#888" }} />
                            <Tooltip content={<ChartTooltip />} />
                            <Line type="monotone" dataKey="avgLatency" stroke="var(--clay)" strokeWidth={2} dot={false} name="Avg Latency (ms)" />
                          </LineChart>
                        </ResponsiveContainer>
                      </div>

                      {/* Token usage over time */}
                      <div className="card" style={{ padding: "18px 22px" }}>
                        <h3 style={{ fontSize: 15, marginBottom: 14 }}>Token Usage Over Time</h3>
                        <ResponsiveContainer width="100%" height={240}>
                          <AreaChart data={data.timeSeries}>
                            <XAxis dataKey="bucket" tickFormatter={formatBucket} tick={{ fontSize: 10, fill: "#888" }} />
                            <YAxis tick={{ fontSize: 10, fill: "#888" }} tickFormatter={tokens} />
                            <Tooltip content={<ChartTooltip />} />
                            <Area stackId="1" type="monotone" dataKey="promptTokens" stroke="var(--analytics)" fill="var(--analytics)" name="Prompt Tokens" />
                            <Area stackId="1" type="monotone" dataKey="completionTokens" stroke="var(--analytics)" fill="var(--analytics-bg)" name="Completion Tokens" />
                          </AreaChart>
                        </ResponsiveContainer>
                      </div>

                      {/* Error rate over time */}
                      <div className="card" style={{ padding: "18px 22px" }}>
                        <h3 style={{ fontSize: 15, marginBottom: 14 }}>Error Rate Over Time</h3>
                        <ResponsiveContainer width="100%" height={240}>
                          <BarChart data={data.timeSeries.map(d => ({
                            ...d,
                            errorPct: d.traceCount > 0 ? parseFloat(((d.errorCount / d.traceCount) * 100).toFixed(1)) : 0,
                          }))}>
                            <XAxis dataKey="bucket" tickFormatter={formatBucket} tick={{ fontSize: 10, fill: "#888" }} />
                            <YAxis tick={{ fontSize: 10, fill: "#888" }} tickFormatter={(v: number) => `${v}%`} />
                            <Tooltip content={<ChartTooltip />} />
                            <ReferenceLine y={5} stroke="var(--hold)" strokeDasharray="3 3" label={{ value: "5%", position: "right", fontSize: 10, fill: "#888" }} />
                            <Bar dataKey="errorPct" fill="var(--sell)" name="Error Rate %" />
                          </BarChart>
                        </ResponsiveContainer>
                      </div>

                      {/* Agent latency breakdown */}
                      <div className="card" style={{ padding: "18px 22px" }}>
                        <h3 style={{ fontSize: 15, marginBottom: 14 }}>Agent Latency</h3>
                        <ResponsiveContainer width="100%" height={240}>
                          <BarChart layout="vertical" data={[...data.agentBreakdown].sort((a, b) => b.avgLatency - a.avgLatency)}>
                            <XAxis type="number" tick={{ fontSize: 10, fill: "#888" }} />
                            <YAxis type="category" dataKey="agent" tick={{ fontSize: 11, fill: "#444" }} width={90} />
                            <Tooltip content={<ChartTooltip />} />
                            <Bar dataKey="avgLatency" name="Avg Latency (ms)">
                              {[...data.agentBreakdown].sort((a, b) => b.avgLatency - a.avgLatency).map((entry) => (
                                <Cell key={entry.agent} fill={AGENT_COLOR[entry.agent as AgentKey] || "var(--clay)"} />
                              ))}
                            </Bar>
                          </BarChart>
                        </ResponsiveContainer>
                      </div>
                    </div>
                  ) : (
                    <div className="card" style={{ padding: 40, textAlign: "center", color: "var(--text-muted)", fontSize: 13, marginBottom: 24 }}>
                      Not enough data for charts
                    </div>
                  )}

                  {/* ── Agent Breakdown Table ── */}
                  <div className="card" style={{ padding: 0, marginBottom: 24 }}>
                    <div style={{ padding: "16px 20px 12px", fontSize: 15, fontWeight: 600 }}>Agent Breakdown</div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 100px 110px 80px 110px", padding: "0 20px 8px", fontSize: 11, textTransform: "uppercase", color: "var(--text-muted)" }}>
                      <span>Agent</span><span style={{ textAlign: "right" }}>Traces</span><span style={{ textAlign: "right" }}>Avg Latency</span><span style={{ textAlign: "right" }}>Errors</span><span style={{ textAlign: "right" }}>Tokens</span>
                    </div>
                    {[...data.agentBreakdown].sort((a, b) => b.traceCount - a.traceCount).map((row, i) => (
                      <div
                        key={row.agent}
                        style={{
                          display: "grid", gridTemplateColumns: "1fr 100px 110px 80px 110px",
                          padding: "10px 20px", fontSize: 13,
                          borderTop: i > 0 ? "1px solid var(--sand)" : undefined,
                        }}
                      >
                        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <span style={{ width: 10, height: 10, borderRadius: "50%", background: AGENT_COLOR[row.agent as AgentKey] || "var(--clay)" }} />
                          <span style={{ fontWeight: 600 }}>{row.agent}</span>
                        </span>
                        <span className="mono" style={{ textAlign: "right" }}>{row.traceCount}</span>
                        <span className="mono" style={{ textAlign: "right" }}>{ms(row.avgLatency)}</span>
                        <span className="mono" style={{ textAlign: "right", color: row.errorCount > 0 ? "var(--sell)" : undefined }}>{row.errorCount}</span>
                        <span className="mono" style={{ textAlign: "right" }}>{tokens(row.totalTokens)}</span>
                      </div>
                    ))}
                  </div>

                  {/* ── RAGAS Quality Panel ── */}
                  {Object.keys(scores.byAgent).length > 0 && (
                    <>
                      <h2 style={{ fontSize: 17, marginBottom: 14 }}>RAGAS Quality Scores</h2>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 14, marginBottom: 24 }}>
                        {Object.entries(scores.byAgent).map(([agent, agentData]) => (
                          <div key={agent} className="card" style={{ padding: "18px 22px" }}>
                            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
                              <span style={{ width: 12, height: 12, borderRadius: "50%", background: AGENT_COLOR[agent as AgentKey] || "var(--clay)" }} />
                              <span style={{ fontWeight: 600, fontSize: 14, textTransform: "capitalize" }}>{agent}</span>
                              <span className="mono" style={{ fontSize: 22, fontWeight: 700, fontFamily: "var(--serif)", marginLeft: "auto" }}>
                                {agentData.overallAvg}
                              </span>
                            </div>
                            {Object.entries(agentData.metrics).map(([metric, m]) => {
                              const maxScale = m.scale === "0-1" ? 1 : 5;
                              const pct = Math.min(100, (m.avg / maxScale) * 100);
                              const barColor = m.scale === "0-1"
                                ? (m.avg < 0.4 ? "var(--sell)" : m.avg <= 0.7 ? "var(--hold)" : "var(--quant)")
                                : (m.avg < 2.0 ? "var(--sell)" : m.avg <= 3.5 ? "var(--hold)" : "var(--quant)");
                              return (
                                <div key={metric} style={{ marginBottom: 10 }}>
                                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 3 }}>
                                    <span>{metric.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())}</span>
                                    <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>{m.avg}</span>
                                  </div>
                                  <div style={{ height: 6, background: "var(--sand)", borderRadius: 3 }}>
                                    <div style={{ height: "100%", width: `${pct}%`, background: barColor, borderRadius: 3 }} />
                                  </div>
                                </div>
                              );
                            })}
                            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
                              Based on {Object.values(agentData.metrics).reduce((sum, m) => sum + m.count, 0)} evaluations
                            </div>
                          </div>
                        ))}
                      </div>
                    </>
                  )}

                  {/* ── Recent Scores Feed ── */}
                  {scores.recentScores.length > 0 && (
                    <div className="card" style={{ padding: 0 }}>
                      <div style={{ padding: "16px 20px 12px", fontSize: 15, fontWeight: 600 }}>Recent Scores</div>
                      {scores.recentScores.map((s, i) => {
                        const parts = s.name.split("/");
                        const metric = parts[2]?.replace(/_/g, " ") || s.name;
                        const agent = parts[1] || "";
                        const isZeroOne = ["AnswerRelevancy", "Faithfulness", "FactualCorrectness", "ContextPrecisionWithoutReference"].includes(parts[2] || "");
                        const valColor = isZeroOne
                          ? (s.value < 0.4 ? "var(--sell)" : s.value <= 0.7 ? "var(--hold)" : "var(--quant)")
                          : (s.value < 2.0 ? "var(--sell)" : s.value <= 3.5 ? "var(--hold)" : "var(--quant)");
                        return (
                          <div
                            key={`${s.name}-${i}`}
                            style={{
                              display: "flex", alignItems: "center", gap: 12,
                              padding: "8px 20px", fontSize: 12,
                              borderTop: i > 0 ? "1px solid var(--sand)" : undefined,
                            }}
                          >
                            <span style={{ color: "var(--text-muted)", textTransform: "capitalize", minWidth: 90 }}>{agent}</span>
                            <span style={{ flex: 1 }}>{metric}</span>
                            <span className="mono" style={{ fontWeight: 600, color: valColor, minWidth: 40, textAlign: "right" }}>{s.value}</span>
                            <span className="mono" style={{ fontSize: 10, color: "var(--text-muted)", minWidth: 60, textAlign: "right" }}>{s.traceId.slice(0, 8)}</span>
                            <span style={{ fontSize: 11, color: "var(--text-muted)", minWidth: 60, textAlign: "right" }}>{relativeTime(s.timestamp)}</span>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}

/** Dashboard page component wrapped in Suspense. Renders KPI cards, time-series charts, agent breakdown table, and RAGAS score panels. */
export default function DashboardPage() {
  return (
    <Suspense fallback={<div className="topbar"><div><h1>Dashboard</h1></div></div>}>
      <style>{`
        @media (max-width: 1024px) {
          .kpi-grid { grid-template-columns: repeat(3, 1fr) !important; }
          .chart-grid { grid-template-columns: 1fr !important; }
        }
        @media (max-width: 640px) {
          .kpi-grid { grid-template-columns: repeat(2, 1fr) !important; }
        }
      `}</style>
      <DashboardContent />
    </Suspense>
  );
}
