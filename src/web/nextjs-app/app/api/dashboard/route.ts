/** Dashboard metrics API. Aggregates traces, observations, and token usage from Langfuse into KPIs, agent breakdown, and time-series data. */
import { NextRequest, NextResponse } from "next/server";
import { langfetch, langfuseConfigured } from "@/lib/langfuse";
import { classifyAgent, type AgentKey } from "@/lib/agentColors";

const SKIP_NAMES = new Set(["ChatOpenAI", "Embedding", "get_prices"]);
const SKIP_PREFIXES = ["get_prices"];

interface LfTrace {
  id: string; name?: string; latency?: number | null;
  timestamp?: string; startTime?: string;
  input?: unknown; output?: unknown;
}

interface LfObs {
  id: string; name?: string; type?: string;
  traceId?: string; startTime?: string; completionStartTime?: string;
  level?: string; usage?: { promptTokens?: number; completionTokens?: number; totalTokens?: number } | null;
}

interface LfResponse { data: unknown[]; meta?: { totalPages?: number; totalItems?: number } };

function isMeaningfulTrace(t: LfTrace): boolean {
  const name = (t.name || "").toLowerCase();
  if (SKIP_NAMES.has(t.name || "")) return false;
  if (SKIP_PREFIXES.some(p => name.startsWith(p))) return false;
  if (name.includes("finsight") || name.includes("orchestrator")) return true;
  if (name.includes("subagentclient") || name.includes("send_message")) return true;
  if (name.includes("rag") || name.includes("quant") || name.includes("market") || name.includes("crewai")) return true;
  if (name.includes("analytics") || name.includes("reviewer") || name.includes("mcp")) return true;
  if ((t.latency ?? 0) > 100) return true;
  return false;
}

function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.ceil(sorted.length * p / 100) - 1;
  return sorted[Math.max(0, idx)];
}

function formatBuckets(from: Date, to: Date, traces: LfTrace[], obsByTrace: Map<string, LfObs[]>, erroredTraceIds: Set<string>): Array<{
  bucket: string; traceCount: number; avgLatency: number; errorCount: number;
  totalTokens: number; promptTokens: number; completionTokens: number;
}> {
  const ms = to.getTime() - from.getTime();
  const step = ms <= 48 * 3600000 ? 3600000 : 86400000;
  const buckets: Array<{
    ts: number; traceCount: number; totalLatency: number; errorCount: number;
    totalTokens: number; promptTokens: number; completionTokens: number;
  }> = [];

  for (let t = from.getTime(); t < to.getTime(); t += step) {
    buckets.push({ ts: t, traceCount: 0, totalLatency: 0, errorCount: 0, totalTokens: 0, promptTokens: 0, completionTokens: 0 });
  }

  const traceToBucket = (ts: string | undefined): number => {
    if (!ts) return -1;
    const t = new Date(ts).getTime();
    const idx = Math.floor((t - from.getTime()) / step);
    return idx >= 0 && idx < buckets.length ? idx : -1;
  };

  const traceById = new Map<string, LfTrace>();
  for (const t of traces) traceById.set(t.id, t);

  for (const trace of traces) {
    const idx = traceToBucket(trace.timestamp || trace.startTime);
    if (idx < 0) continue;
    const b = buckets[idx];
    b.traceCount++;
    b.totalLatency += trace.latency ?? 0;
    if (erroredTraceIds.has(trace.id)) b.errorCount++;
  }

  for (const [traceId, obsList] of obsByTrace) {
    const trace = traceById.get(traceId);
    const idx = trace ? traceToBucket(trace.timestamp || trace.startTime) : -1;
    if (idx < 0) continue;
    const b = buckets[idx];
    for (const o of obsList) {
      if (o.type !== "GENERATION") continue;
      const prompt = o.usage?.promptTokens ?? 0;
      const completion = o.usage?.completionTokens ?? 0;
      const total = o.usage?.totalTokens ?? (prompt + completion);
      b.promptTokens += prompt;
      b.completionTokens += completion;
      b.totalTokens += total;
    }
  }

  return buckets.map(b => ({
    bucket: new Date(b.ts).toISOString(),
    traceCount: b.traceCount,
    avgLatency: b.traceCount > 0 ? Math.round(b.totalLatency / b.traceCount) : 0,
    errorCount: b.errorCount,
    totalTokens: b.totalTokens,
    promptTokens: b.promptTokens,
    completionTokens: b.completionTokens,
  }));
}

/** Fetches trace and observation data from Langfuse for the given time range, computes KPIs (latency percentiles, error rates, token usage, TFFT), agent breakdown, and bucketed time-series. */
export async function GET(req: NextRequest) {
  if (!langfuseConfigured()) {
    return NextResponse.json({ error: "Langfuse keys not configured" }, { status: 500 });
  }

  const rawHours = parseInt(req.nextUrl.searchParams.get("hours") || "24", 10);
  const hours = Math.min(720, Math.max(1, isNaN(rawHours) || rawHours <= 0 ? 24 : rawHours));
  const fromTimestamp = new Date(Date.now() - hours * 3600000).toISOString();

  try {
    const tracePages: LfTrace[][] = [];
    const tracesData: LfResponse = await langfetch<LfResponse>(`/api/public/traces?limit=100&fromTimestamp=${fromTimestamp}`);
    tracePages.push((tracesData.data || []) as LfTrace[]);

    const totalPages = tracesData.meta?.totalPages ?? 1;
    const maxPages = Math.min(totalPages, 10);
    for (let p = 2; p <= maxPages; p++) {
      const page: LfResponse = await langfetch<LfResponse>(`/api/public/traces?limit=100&fromTimestamp=${fromTimestamp}&page=${p}`);
      tracePages.push((page.data || []) as LfTrace[]);
    }

    const allTraces = tracePages.flat().filter(isMeaningfulTrace);
    const truncated = totalPages > 5;

    const traceMap = new Map<string, AgentKey>();
    for (const t of allTraces) traceMap.set(t.id, classifyAgent(t.name || ""));

    const obsPages: LfObs[][] = [];
    const obsData: LfResponse = await langfetch<LfResponse>(`/api/public/observations?limit=100&fromTimestamp=${fromTimestamp}`);
    obsPages.push((obsData.data || []) as LfObs[]);
    const obsTotalPages = Math.min(obsData.meta?.totalPages ?? 1, 5);
    for (let p = 2; p <= obsTotalPages; p++) {
      const page: LfResponse = await langfetch<LfResponse>(`/api/public/observations?limit=100&fromTimestamp=${fromTimestamp}&page=${p}`);
      obsPages.push((page.data || []) as LfObs[]);
    }
    const allObs = obsPages.flat();

    const obsByTrace = new Map<string, LfObs[]>();
    for (const o of allObs) {
      if (o.type !== "GENERATION" || !o.traceId) continue;
      if (!traceMap.has(o.traceId)) continue;
      const list = obsByTrace.get(o.traceId) || [];
      list.push(o);
      obsByTrace.set(o.traceId, list);
    }

    const tracesWithLatency = allTraces.filter(t => typeof t.latency === "number");
    const latencies = tracesWithLatency.map(t => t.latency as number).sort((a, b) => a - b);

    const erroredTraceIds = new Set<string>();
    for (const [traceId, obsList] of obsByTrace) {
      for (const o of obsList) {
        if (o.level === "ERROR") { erroredTraceIds.add(traceId); break; }
      }
    }

    let totalTokens = 0, promptTokens = 0, completionTokens = 0;
    for (const obsList of obsByTrace.values()) {
      for (const o of obsList) {
        const prompt = o.usage?.promptTokens ?? 0;
        const completion = o.usage?.completionTokens ?? 0;
        const total = o.usage?.totalTokens ?? (prompt + completion);
        promptTokens += prompt;
        completionTokens += completion;
        totalTokens += total;
      }
    }

    let tfftSum = 0, tfftCount = 0;
    for (const obsList of obsByTrace.values()) {
      for (const o of obsList) {
        if (o.completionStartTime && o.startTime) {
          tfftSum += new Date(o.completionStartTime).getTime() - new Date(o.startTime).getTime();
          tfftCount++;
        }
      }
    }

    const agentMap = new Map<string, {
      traceCount: number; totalLatency: number; errorCount: number;
      totalTokens: number; promptTokens: number; completionTokens: number;
    }>();

    for (const t of allTraces) {
      const agent = traceMap.get(t.id) || "orchestrator";
      const entry = agentMap.get(agent) || { traceCount: 0, totalLatency: 0, errorCount: 0, totalTokens: 0, promptTokens: 0, completionTokens: 0 };
      entry.traceCount++;
      entry.totalLatency += t.latency ?? 0;
      if (erroredTraceIds.has(t.id)) entry.errorCount++;
      agentMap.set(agent, entry);
    }

    for (const [traceId, obsList] of obsByTrace) {
      const agent = traceMap.get(traceId);
      if (!agent) continue;
      const entry = agentMap.get(agent);
      if (!entry) continue;
      for (const o of obsList) {
        const prompt = o.usage?.promptTokens ?? 0;
        const completion = o.usage?.completionTokens ?? 0;
        const total = o.usage?.totalTokens ?? (prompt + completion);
        entry.promptTokens += prompt;
        entry.completionTokens += completion;
        entry.totalTokens += total;
      }
    }

    const agentBreakdown = Array.from(agentMap.entries()).map(([agent, v]) => ({
      agent,
      traceCount: v.traceCount,
      avgLatency: v.traceCount > 0 ? Math.round(v.totalLatency / v.traceCount) : 0,
      errorCount: v.errorCount,
      totalTokens: v.totalTokens,
      promptTokens: v.promptTokens,
      completionTokens: v.completionTokens,
    }));

    const timeSeries = formatBuckets(new Date(fromTimestamp), new Date(), allTraces, obsByTrace, erroredTraceIds);

    return NextResponse.json({
      hours,
      truncated,
      kpi: {
        totalTraces: allTraces.length,
        errorRate: allTraces.length > 0 ? parseFloat(((erroredTraceIds.size / allTraces.length) * 100).toFixed(1)) : 0,
        errorCount: erroredTraceIds.size,
        avgLatency: latencies.length > 0 ? Math.round(latencies.reduce((a, b) => a + b, 0) / latencies.length) : 0,
        p50Latency: percentile(latencies, 50),
        p95Latency: percentile(latencies, 95),
        p99Latency: percentile(latencies, 99),
        totalTokens,
        promptTokens,
        completionTokens,
        avgTfft: tfftCount > 0 ? Math.round(tfftSum / tfftCount) : null,
      },
      agentBreakdown,
      timeSeries,
    });
  } catch (e) {
    return NextResponse.json({ error: `Failed to fetch from Langfuse: ${String(e)}` }, { status: 502 });
  }
}
