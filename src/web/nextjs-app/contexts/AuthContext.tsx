"use client";

import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import {
  loginAPI, logoutAPI, refreshTokenAPI, fetchMe,
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

export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    const restore = async () => {
      // Try refresh — user may have a valid httpOnly refresh cookie
      const refreshed = await refreshTokenAPI();
      if (controller.signal.aborted) return;
      if (refreshed) {
        setUser(refreshed.user);
        setAccessToken(refreshed.access_token);
        document.cookie = `finsight_session=1; path=/; max-age=${refreshed.expires_in}; SameSite=Lax`;
        if (window.location.pathname === "/login") {
          const sp = new URLSearchParams(window.location.search);
          window.location.href = sp.get("redirect") || "/research";
        }
      }
      setIsLoading(false);
    };
    restore();
    return () => controller.abort();
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const data = await loginAPI(username, password);
    setUser(data.user);
    setAccessToken(data.access_token);
    document.cookie = `finsight_session=1; path=/; max-age=${data.expires_in}; SameSite=Lax`;
  }, []);

  const logout = useCallback(async () => {
    await logoutAPI();
    setUser(null);
    setAccessToken(null);
    document.cookie = "finsight_session=; path=/; max-age=0";
    router.push("/login");
  }, [router]);

  return (
    <AuthContext.Provider value={{ user, accessToken, isAuthenticated: !!user, isLoading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
