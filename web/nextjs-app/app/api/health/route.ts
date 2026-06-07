import { NextRequest, NextResponse } from "next/server";

const TARGETS: Record<string, string> = {
  orchestrator: "http://localhost:8001/health",
  rag: "http://localhost:8002/health",
  quant: "http://localhost:8003/health",
  market: "http://localhost:8004/health",
  mcp: "http://localhost:8010/health",
};

export async function GET(req: NextRequest) {
  const svc = req.nextUrl.searchParams.get("svc");
  const url = svc ? TARGETS[svc] : null;
  if (!url) {
    return NextResponse.json({ error: "unknown service" }, { status: 400 });
  }
  const t0 = Date.now();
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(4000) });
    const latency = Date.now() - t0;
    const body = await r.json().catch(() => ({}));
    return NextResponse.json({ status: r.ok ? "ok" : "warn", latency, detail: body });
  } catch {
    return NextResponse.json({ status: "down", latency: Date.now() - t0 });
  }
}
