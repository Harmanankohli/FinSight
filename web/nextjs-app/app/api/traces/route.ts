import { NextRequest, NextResponse } from "next/server";

const LF_BASE = process.env.LANGFUSE_BASE_URL || "https://jp.cloud.langfuse.com";
const LF_PK = process.env.LANGFUSE_PUBLIC_KEY || "";
const LF_SK = process.env.LANGFUSE_SECRET_KEY || "";

const AUTH = Buffer.from(`${LF_PK}:${LF_SK}`).toString("base64");

async function langfetch(path: string) {
  const r = await fetch(`${LF_BASE}${path}`, {
    headers: { Authorization: `Basic ${AUTH}` },
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error(`Langfuse ${r.status}: ${await r.text()}`);
  return r.json();
}

export async function GET(req: NextRequest) {
  const traceId = req.nextUrl.searchParams.get("traceId");

  if (!LF_PK || !LF_SK) {
    return NextResponse.json({ error: "Langfuse keys not configured" }, { status: 500 });
  }

  try {
    if (traceId) {
      const [trace, obs] = await Promise.all([
        langfetch(`/api/public/traces/${traceId}`),
        langfetch(`/api/public/observations?traceId=${traceId}&limit=100`),
      ]);
      return NextResponse.json({ trace, observations: obs.data || [] });
    } else {
      // Fetch more traces, then group: observations that share a traceId
      // are part of the same chain. We want traces that have children.
      const data = await langfetch("/api/public/traces?limit=100");
      const allTraces = (data.data || []) as Record<string, unknown>[];

      // Group by finding traces with observations > 0 or significant latency
      // Also collapse: many auto-instrumented spans create separate traces
      // for the same logical run. Group by sessionId if available, otherwise
      // keep traces that have observationCount or latency > 500ms
      const meaningful = allTraces.filter((t: Record<string, unknown>) => {
        const latency = (t.latency as number) || 0;
        const name = (t.name as string) || "";
        // Keep orchestrator traces, agent traces, and anything > 500ms
        if (name.includes("finsight") || name.includes("orchestrator")) return true;
        if (name.includes("SubAgentClient") || name.includes("send_message")) return true;
        if (name.includes("rag") || name.includes("quant") || name.includes("market") || name.includes("crewai")) return true;
        if (latency > 500) return true;
        // Keep traces that aren't tiny auto-instrumented ones
        if (name === "ChatOpenAI" || name.includes("Embedding") || name.includes("get_prices")) return false;
        return latency > 100;
      });

      return NextResponse.json({ traces: meaningful.slice(0, 30) });
    }
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    return NextResponse.json({ error: msg }, { status: 502 });
  }
}
