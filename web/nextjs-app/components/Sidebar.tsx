"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { getRecentQueries, RecentQuery } from "@/lib/recentQueries";

const NAV = [
  { href: "/", label: "Overview", icon: "M3 9.5 12 3l9 6.5V21H3z" },
  { href: "/research", label: "Research", icon: "M21 11.5a8.4 8.4 0 0 1-12 7.6L3 21l1.9-6A8.5 8.5 0 1 1 21 11.5z" },
  { href: "/trace", label: "Trace", icon: "M4 4v16M4 8h7M4 14h11M4 20h5" },
  { href: "/memory", label: "Memory", icon: "M12 3a9 9 0 1 0 9 9M12 3v9l5 3" },
  { href: "/operator", label: "Operator", icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" },
];

interface LfTrace {
  id: string; name?: string; latency?: number; timestamp?: string;
}

export function Sidebar() {
  const pathname = usePathname();
  const [recent, setRecent] = useState<RecentQuery[]>([]);
  const [traces, setTraces] = useState<LfTrace[]>([]);

  useEffect(() => {
    setRecent(getRecentQueries());
    const h = () => setRecent(getRecentQueries());
    window.addEventListener("finsight:recent-queries-changed", h);

    fetch("/api/traces")
      .then((r) => r.ok ? r.json() : { traces: [] })
      .then((data) => setTraces((data.traces || []).slice(0, 5)))
      .catch(() => {});

    return () => window.removeEventListener("finsight:recent-queries-changed", h);
  }, []);

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

      {/* Langfuse traces */}
      {traces.length > 0 && (
        <>
          <div className="sb-sec">Recent traces</div>
          <nav className="sb-nav" style={{ paddingTop: 2 }}>
            {traces.map((t, i) => (
              <Link
                key={`t-${i}`}
                href={`/trace?traceId=${t.id}`}
                className="sb-link"
                style={{ fontWeight: 400, fontSize: "12px", color: "var(--text-muted)" }}
              >
                <span style={{ fontSize: 11 }}>{t.name || "trace"}</span>
                <span className="mono" style={{ marginLeft: "auto", fontSize: 10, opacity: 0.6 }}>
                  {t.latency ? `${(t.latency / 1000).toFixed(0)}s` : ""}
                </span>
              </Link>
            ))}
          </nav>
        </>
      )}

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
        <span className="dot-live"></span> 5 services · AG-UI
      </div>
    </aside>
  );
}
