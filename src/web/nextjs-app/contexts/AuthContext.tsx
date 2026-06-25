/** Authentication context provider. Manages user session, token refresh, login/logout, and route guards. */
"use client";

import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react";
import { useRouter, usePathname } from "next/navigation";
import {
  loginAPI, logoutAPI, refreshTokenAPI,
  type AuthUser,
} from "@/lib/auth";

interface AuthContextValue {
  user: AuthUser | null;
  accessToken: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const AUTH_ENABLED = process.env.NEXT_PUBLIC_AUTH_ENABLED !== "false";

const ANONYMOUS_USER: AuthUser = { id: "anonymous", username: "anonymous", role: "user" };

/** Provides auth state to the component tree. Handles session restoration, login/logout actions, and automatic redirect for unauthenticated users. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<AuthUser | null>(AUTH_ENABLED ? null : ANONYMOUS_USER);
  const [accessToken, setAccessToken] = useState<string | null>(AUTH_ENABLED ? null : "disabled");
  const [isLoading, setIsLoading] = useState(AUTH_ENABLED);

  useEffect(() => {
    if (!AUTH_ENABLED) return;
    const controller = new AbortController();
    const restore = async () => {
      const refreshed = await refreshTokenAPI();
      if (controller.signal.aborted) return;
      if (refreshed) {
        setUser(refreshed.user);
        setAccessToken(refreshed.access_token);
      }
      setIsLoading(false);
    };
    restore();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!AUTH_ENABLED || isLoading) return;
    if (!user && pathname !== "/login") {
      router.replace(`/login?redirect=${encodeURIComponent(pathname)}`);
    } else if (user && pathname === "/login") {
      const sp = new URLSearchParams(window.location.search);
      router.replace(sp.get("redirect") || "/research");
    }
  }, [isLoading, user, pathname, router]);

  const login = useCallback(async (username: string, password: string) => {
    const data = await loginAPI(username, password);
    setUser(data.user);
    setAccessToken(data.access_token);
  }, []);

  const logout = useCallback(async () => {
    await logoutAPI();
    setUser(null);
    setAccessToken(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, accessToken, isAuthenticated: !!user, isLoading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

/** Hook to access the auth context. Throws if used outside AuthProvider. */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
