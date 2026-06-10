"use client";

export interface AuthUser {
  id: string;
  username: string;
  role: string;
}

export interface LoginResponse {
  access_token: string;
  expires_in: number;
  token_type: string;
  user: AuthUser;
}

const TOKEN_COOKIE = "finsight_token";

export function setTokenCookie(token: string) {
  document.cookie = `${TOKEN_COOKIE}=${token}; path=/; max-age=86400; samesite=lax`;
}

export function clearTokenCookie() {
  document.cookie = `${TOKEN_COOKIE}=; path=/; max-age=0`;
}

export function getTokenFromCookie(): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${TOKEN_COOKIE}=([^;]*)`));
  return match ? match[1] : null;
}

export async function loginAPI(username: string, password: string): Promise<LoginResponse> {
  const resp = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: { message: "Login failed" } }));
    throw new Error(err.error?.message || "Login failed");
  }
  return resp.json();
}

export async function refreshTokenAPI(): Promise<LoginResponse | null> {
  const resp = await fetch("/auth/refresh", { method: "POST" });
  if (!resp.ok) return null;
  return resp.json();
}

export async function logoutAPI(): Promise<void> {
  await fetch("/auth/logout", { method: "POST" }).catch(() => {});
}

export async function fetchMe(token: string): Promise<AuthUser | null> {
  const resp = await fetch("/auth/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) return null;
  return resp.json();
}
