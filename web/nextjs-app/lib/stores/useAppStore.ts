"use client";
import { create } from "zustand";

interface AppStore {
  traceOpen: boolean;
  setTraceOpen: (v: boolean) => void;
  sidebarOpen: boolean;
  setSidebarOpen: (v: boolean) => void;
  userId: string;
  setUserId: (id: string) => void;
}

export const useAppStore = create<AppStore>((set) => ({
  traceOpen: false,
  setTraceOpen: (v) => set({ traceOpen: v }),
  sidebarOpen: true,
  setSidebarOpen: (v) => set({ sidebarOpen: v }),
  userId: "",
  setUserId: (id) => set({ userId: id }),
}));
