/** API functions for authentication: login, logout, token refresh, and user profile retrieval. */
"use client";

/** Authenticated user profile returned by the auth API. */
export interface AuthUser {
  id: string;
  username: string;
  role: string;
}

/** Response shape from the login and token-refresh endpoints. */
export interface LoginResponse {
  access_token: string;
  expires_in: number;
  token_type: string;
  user: AuthUser;
}

/** Authenticates a user with username and password. Returns access token and user profile. */
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

/** Attempts to refresh the current auth token via the server. Returns null if refresh fails. */
export async function refreshTokenAPI(): Promise<LoginResponse | null> {
  const resp = await fetch("/auth/refresh", { method: "POST" });
  if (!resp.ok) return null;
  return resp.json();
}

/** Logs the user out by calling the server logout endpoint. Errors are silently caught. */
export async function logoutAPI(): Promise<void> {
  await fetch("/auth/logout", { method: "POST" }).catch(() => {});
}

/** Fetches the authenticated user's profile using the given bearer token. */
export async function fetchMe(token: string): Promise<AuthUser | null> {
  const resp = await fetch("/auth/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) return null;
  return resp.json();
}
