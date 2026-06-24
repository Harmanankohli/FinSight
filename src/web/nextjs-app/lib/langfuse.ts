/** Langfuse API client utilities. Provides authenticated fetch and configuration check. */
const LF_BASE = process.env.LANGFUSE_BASE_URL || "https://jp.cloud.langfuse.com";
const LF_PK = process.env.LANGFUSE_PUBLIC_KEY || "";
const LF_SK = process.env.LANGFUSE_SECRET_KEY || "";
const AUTH = Buffer.from(`${LF_PK}:${LF_SK}`).toString("base64");

/** Returns true if both Langfuse public and secret keys are set in the environment. */
export function langfuseConfigured(): boolean {
  return !!(LF_PK && LF_SK);
}

/** Makes an authenticated GET request to the Langfuse public API at the given path. */
export async function langfetch<T>(path: string): Promise<T> {
  const r = await fetch(`${LF_BASE}${path}`, {
    headers: { Authorization: `Basic ${AUTH}` },
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error(`Langfuse ${r.status}: ${await r.text()}`);
  return r.json() as Promise<T>;
}
