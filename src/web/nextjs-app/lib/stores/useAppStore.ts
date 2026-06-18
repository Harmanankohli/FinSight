"use client";
import { create } from "zustand";

interface AppStore {
  sidebarOpen: boolean;
  setSidebarOpen: (v: boolean) => void;
  userId: string;
  setUserId: (id: string) => void;
}

export const useAppStore = create<AppStore>((set) => ({
  sidebarOpen: true,
  setSidebarOpen: (v) => set({ sidebarOpen: v }),
  userId: "",
  setUserId: (id) => set({ userId: id }),
}));
