/** Sidebar navigation component with links, recent queries, and user session info. */
"use client";

import { useSyncExternalStore } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { getRecentQueries, type RecentQuery } from "@/lib/recentQueries";
import { useAuth } from "@/contexts/AuthContext";

const SERVER_EMPTY: RecentQuery[] = [];

const NAV = [
  { href: "/", label: "Overview", icon: "M3 9.5 12 3l9 6.5V21H3z" },
  { href: "/research", label: "Research", icon: "M21 11.5a8.4 8.4 0 0 1-12 7.6L3 21l1.9-6A8.5 8.5 0 1 1 21 11.5z" },
  { href: "/dashboard", label: "Dashboard", icon: "M3 3v18h18M7 14v3M11 10v7M15 7v10M19 4v13" },
  { href: "/memory", label: "Memory", icon: "M12 3a9 9 0 1 0 9 9M12 3v9l5 3" },
  { href: "/operator", label: "Operator", icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" },
];

/** Renders the sidebar: logo, navigation links, recent query shortcuts, and footer with user info and sign-out. */
export function Sidebar() {
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const recent = useSyncExternalStore(
    (cb) => {
      window.addEventListener("finsight:recent-queries-changed", cb);
      return () => window.removeEventListener("finsight:recent-queries-changed", cb);
    },
    getRecentQueries,
    () => SERVER_EMPTY,
  );

  return (
    <aside className="sidebar">
      <div className="sb-logo">
        <div className="sb-mark"><span></span></div>
        <div className="sb-word">
          <div className="name">FinSight</div>
          <div className="tag">Investment Research</div>
        </div>
      </div>

      <div className="sb-sec">Workspace</div>
      <nav className="sb-nav">
        {NAV.map((n) => (
          <Link key={n.href} href={n.href} className={`sb-link ${pathname === n.href ? "active" : ""}`}>
            <svg className="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><path d={n.icon} /></svg>
            {n.label}
          </Link>
        ))}
      </nav>

      {/* Recent queries */}
      {recent.length > 0 && (
        <>
          <div className="sb-sec">Recent queries</div>
          <nav className="sb-nav" style={{ paddingTop: 2 }}>
            {recent.slice(0, 5).map((q, i) => {
              const ticker = q.text.match(/\b([A-Z]{2,5})\b/)?.[1];
              return (
                <Link
                  key={`r-${q.ts}-${i}`}
                  href={ticker ? `/memory?ticker=${ticker}` : "/memory"}
                  className="sb-link"
                  style={{ fontWeight: 400, fontSize: "12.5px", color: "var(--text-muted)" }}
                  title={q.text}
                >
                  {q.text.length > 28 ? q.text.slice(0, 28) + "…" : q.text}
                </Link>
              );
            })}
          </nav>
        </>
      )}

      <div className="sb-foot">
        {user && (
          <>
            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--clay-deep)" }}>{user.username}</span>
            <span style={{ flex: 1 }} />
            <button onClick={logout} className="sb-logout" title="Sign out">↩</button>
          </>
        )}
        {!user && <span className="dot-live"></span>}
        <span style={{ marginLeft: user ? 6 : 0, fontSize: 10, color: "var(--text-muted)" }}>5 services · AG-UI</span>
      </div>
    </aside>
  );
}
