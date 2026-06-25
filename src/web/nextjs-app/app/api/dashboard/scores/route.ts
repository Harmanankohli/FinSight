/** RAGAS quality scores API. Fetches evaluation scores from Langfuse and aggregates them by agent and metric. */
import { NextResponse } from "next/server";
import { langfetch, langfuseConfigured } from "@/lib/langfuse";
import { normalizeScoreAgent } from "@/lib/agentColors";

const ZERO_ONE_METRICS = new Set([
  "AnswerRelevancy", "Faithfulness", "FactualCorrectness",
  "ContextPrecisionWithoutReference",
]);

interface LfScore {
  id: string; name?: string; value?: number; traceId?: string;
  timestamp?: string; createdAt?: string;
}

interface MetricAgg {
  avg: number; min: number; max: number; count: number; recent: number;
  scale: "0-1" | "1-5";
}

/** Fetches RAGAS scores from Langfuse, groups them by agent/metric, computes aggregates (avg, min, max, count, recent), and returns the 15 most recent scores. */
export async function GET() {
  if (!langfuseConfigured()) {
    return NextResponse.json({ error: "Langfuse keys not configured" }, { status: 500 });
  }

  try {
    const scorePages: LfScore[][] = [];
    const firstPage = await langfetch<{ data: LfScore[]; meta?: { totalPages?: number } }>("/api/public/scores?limit=100");
    scorePages.push((firstPage.data || []) as LfScore[]);
    const scoreTotalPages = Math.min(firstPage.meta?.totalPages ?? 1, 5);
    for (let p = 2; p <= scoreTotalPages; p++) {
      const page = await langfetch<{ data: LfScore[] }>(`/api/public/scores?limit=100&page=${p}`);
      scorePages.push((page.data || []) as LfScore[]);
    }
    const allScores = scorePages.flat();

    const ragasScores = allScores.filter(s => {
      if (!s.name) return false;
      const parts = s.name.split("/");
      return parts.length === 3 && parts[0] === "ragas";
    });

    const byAgent = new Map<string, Map<string, number[]>>();
    const recentScores: Array<{ name: string; value: number; traceId: string; timestamp: string }> = [];

    for (const s of ragasScores) {
      const parts = s.name!.split("/");
      const agent = normalizeScoreAgent(parts[1]);
      const metric = parts[2];
      const value = s.value ?? 0;

      if (!byAgent.has(agent)) byAgent.set(agent, new Map());
      const agentMetrics = byAgent.get(agent)!;
      if (!agentMetrics.has(metric)) agentMetrics.set(metric, []);
      agentMetrics.get(metric)!.push(value);

      recentScores.push({
        name: s.name!,
        value,
        traceId: s.traceId || "",
        timestamp: s.timestamp || s.createdAt || "",
      });
    }

    recentScores.sort((a, b) => b.timestamp.localeCompare(a.timestamp));

    const result: Record<string, { metrics: Record<string, MetricAgg>; overallAvg: number }> = {};

    for (const [agent, metrics] of byAgent) {
      const metricEntries: Record<string, MetricAgg> = {};
      let sumAll = 0, countAll = 0;

      for (const [metric, values] of metrics) {
        const sorted = [...values].sort((a, b) => a - b);
        const avg = values.reduce((a, b) => a + b, 0) / values.length;
        const scale: "0-1" | "1-5" = ZERO_ONE_METRICS.has(metric) ? "0-1" : "1-5";
        metricEntries[metric] = {
          avg: parseFloat(avg.toFixed(3)),
          min: sorted[0],
          max: sorted[sorted.length - 1],
          count: values.length,
          recent: values[values.length - 1],
          scale,
        };
        sumAll += avg;
        countAll++;
      }

      result[agent] = {
        metrics: metricEntries,
        overallAvg: countAll > 0 ? parseFloat((sumAll / countAll).toFixed(3)) : 0,
      };
    }

    return NextResponse.json({
      byAgent: result,
      recentScores: recentScores.slice(0, 15),
    });
  } catch (e) {
    return NextResponse.json({ error: `Failed to fetch scores from Langfuse: ${String(e)}` }, { status: 502 });
  }
}
