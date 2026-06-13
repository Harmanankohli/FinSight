"use client";

import { Suspense, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";

function LoginForm() {
  const router = useRouter();
  const sp = useSearchParams();
  const { login, isLoading } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (isLoading) {
    return (
      <div className="login-card"><p style={{ color: "var(--text-muted)" }}>Restoring session…</p></div>
    );
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(username, password);
      router.push(sp.get("redirect") || "/research");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-card">
      <div className="login-logo">
        <div className="sb-mark"><span></span></div>
        <div>
          <div className="login-name">FinSight</div>
          <div className="login-tag">Investment Research</div>
        </div>
      </div>

      <form onSubmit={handleSubmit}>
        {error && <div className="login-error">{error}</div>}

        <label>
          <span className="login-label">Username</span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            disabled={busy}
            placeholder="Enter your username"
          />
        </label>

        <label>
          <span className="login-label">Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={busy}
            placeholder="Enter your password"
          />
        </label>

        <button className="login-btn" type="submit" disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

export default function LoginPage() {
  return (
    <div className="login-page">
      <Suspense fallback={<div className="login-card"><p style={{ color: "var(--text-muted)" }}>Loading…</p></div>}>
        <LoginForm />
      </Suspense>
    </div>
  );
}
