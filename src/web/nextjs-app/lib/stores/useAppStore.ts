/** Global app state managed by Zustand: sidebar toggle and current user ID. */
"use client";
import { create } from "zustand";

interface AppStore {
  sidebarOpen: boolean;
  setSidebarOpen: (v: boolean) => void;
  userId: string;
  setUserId: (id: string) => void;
}

/** Zustand store exposing `sidebarOpen` and `userId` state with their setters. */
export const useAppStore = create<AppStore>((set) => ({
  sidebarOpen: true,
  setSidebarOpen: (v) => set({ sidebarOpen: v }),
  userId: "",
  setUserId: (id) => set({ userId: id }),
}));
