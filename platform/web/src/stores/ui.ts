import { create } from 'zustand';
import type { Role } from '../utils/permissions';

export interface UiUser {
  username: string;
  role: Role;
}

interface UiState {
  user: UiUser | null;
  setUser: (user: UiUser | null) => void;
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;
}

export const useUiStore = create<UiState>((set) => ({
  user: null,
  setUser: (user) => set({ user }),
  sidebarCollapsed: false,
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
}));
