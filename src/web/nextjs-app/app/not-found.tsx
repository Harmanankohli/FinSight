import Link from "next/link";

export default function NotFound() {
  return (
    <>
      <div className="topbar">
        <div>
          <h1>Not Found</h1>
          <div className="sub">The page you requested does not exist</div>
        </div>
      </div>
      <div className="scroll">
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center",
          justifyContent: "center", padding: "100px 30px", textAlign: "center",
        }}>
          <div style={{
            fontSize: 72, fontWeight: 700, fontFamily: "var(--serif)",
            color: "var(--clay-light)", lineHeight: 1,
          }}>404</div>
          <p style={{
            fontSize: 17, color: "var(--text-secondary)",
            marginTop: 16, maxWidth: "40ch", lineHeight: 1.5,
          }}>
            This page could not be found. It may have been moved or deleted.
          </p>
          <Link href="/" className="pill" style={{
            marginTop: 28, background: "var(--clay)", color: "#fff",
            borderColor: "var(--clay)", padding: "11px 22px",
            fontWeight: 600, fontSize: 14, borderRadius: 999,
          }}>Back to Overview</Link>
        </div>
      </div>
    </>
  );
}
