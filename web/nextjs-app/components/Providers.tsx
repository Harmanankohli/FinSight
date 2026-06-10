"use client";

import { usePathname } from "next/navigation";
import { CopilotKit } from "@copilotkit/react-core";
import { AuthProvider } from "@/contexts/AuthContext";
import { Sidebar } from "./Sidebar";

const NO_SIDEBAR = ["/login"];

export function Providers({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const showSidebar = !NO_SIDEBAR.includes(pathname);

  return (
    <AuthProvider>
      <CopilotKit runtimeUrl="/api/copilotkit" agent="finsight" publicApiKey={process.env.NEXT_PUBLIC_COPILOTKIT_API_KEY}>
        <div className="app">
          {showSidebar && <Sidebar />}
          <div className="main">
            {children}
          </div>
        </div>
      </CopilotKit>
    </AuthProvider>
  );
}
