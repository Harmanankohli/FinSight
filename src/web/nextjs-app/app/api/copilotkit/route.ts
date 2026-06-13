import {
  CopilotRuntime,
  ExperimentalEmptyAdapter,
  copilotRuntimeNextJSAppRouterEndpoint,
} from "@copilotkit/runtime";
import { HttpAgent } from "@ag-ui/client";
import { NextRequest } from "next/server";

const ORCHESTRATOR_URL =
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL || "http://localhost:8001";

export const POST = async (req: NextRequest) => {
  let userId =
    req.headers.get("x-finsight-user-id") ||
    req.cookies.get("finsight_user_id")?.value ||
    "";
  const isNewAnon = !userId;
  if (isNewAnon) {
    userId = `anon-${crypto.randomUUID()}`;
  }
  const token = req.cookies.get("finsight_token")?.value || "";

  const headers: Record<string, string> = {};
  headers["X-FinSight-User-Id"] = userId;
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const agent = new HttpAgent({
    url: `${ORCHESTRATOR_URL}/a2a-agui`,
    headers,
  });

  const runtime = new CopilotRuntime({
    agents: { finsight: agent },
  });

  const serviceAdapter = new ExperimentalEmptyAdapter();

  const { handleRequest } = copilotRuntimeNextJSAppRouterEndpoint({
    runtime,
    serviceAdapter,
    endpoint: "/api/copilotkit",
  });

  const response = await handleRequest(req);

  if (isNewAnon) {
    response.headers.set(
      "Set-Cookie",
      `finsight_user_id=${userId}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax; Secure`
    );
  }

  return response;
};
