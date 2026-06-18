const LF_BASE = process.env.LANGFUSE_BASE_URL || "https://jp.cloud.langfuse.com";
const LF_PK = process.env.LANGFUSE_PUBLIC_KEY || "";
const LF_SK = process.env.LANGFUSE_SECRET_KEY || "";
const AUTH = Buffer.from(`${LF_PK}:${LF_SK}`).toString("base64");

export function langfuseConfigured(): boolean {
  return !!(LF_PK && LF_SK);
}

export async function langfetch(path: string): Promise<any> {
  const r = await fetch(`${LF_BASE}${path}`, {
    headers: { Authorization: `Basic ${AUTH}` },
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error(`Langfuse ${r.status}: ${await r.text()}`);
  return r.json();
}
