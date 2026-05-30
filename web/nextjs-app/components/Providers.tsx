"use client";

import { CopilotKit } from "@copilotkit/react-core";
import { Sidebar } from "./Sidebar";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <CopilotKit runtimeUrl="/api/copilotkit" agent="finsight" publicApiKey={process.env.NEXT_PUBLIC_COPILOTKIT_API_KEY}>
      <div className="app">
        <Sidebar />
        <div className="main">
          {children}
        </div>
      </div>
    </CopilotKit>
  );
}
