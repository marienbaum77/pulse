import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type { Project, Role, User } from "./types";

/* ---------- auth ---------- */
interface AuthValue {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (role: Role) => boolean;
}
const AuthCtx = createContext<AuthValue>(null as never);
const RANK: Record<Role, number> = { viewer: 0, editor: 1, admin: 2 };

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<User>("/auth/me"), retry: false, staleTime: 5 * 60_000 });
  const loginM = useMutation({
    mutationFn: (v: { email: string; password: string }) => api<User>("/auth/login", { method: "POST", body: v }),
    onSuccess: (u) => qc.setQueryData(["me"], u),
  });
  useEffect(() => {
    const onUnauth = () => qc.setQueryData(["me"], null);
    window.addEventListener("pulse:unauthorized", onUnauth);
    return () => window.removeEventListener("pulse:unauthorized", onUnauth);
  }, [qc]);
  const value = useMemo<AuthValue>(() => {
    const user = me.data ?? null;
    return {
      user,
      loading: me.isLoading,
      login: async (email, password) => { await loginM.mutateAsync({ email, password }); },
      logout: async () => { await api("/auth/logout", { method: "POST" }); qc.clear(); qc.setQueryData(["me"], null); },
      can: (role) => !!user && RANK[user.role] >= RANK[role],
    };
  }, [me.data, me.isLoading, loginM, qc]);
  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}
export const useAuth = () => useContext(AuthCtx);

/* ---------- текущий проект ---------- */
interface ProjectValue { projects: Project[]; project: Project | null; setProjectId: (id: number) => void; loading: boolean }
const ProjectCtx = createContext<ProjectValue>(null as never);

export function ProjectProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const q = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects"), enabled: !!user });
  const [id, setId] = useState<number | null>(() => Number(localStorage.getItem("pulse.project")) || null);
  const projects = q.data ?? [];
  const project = projects.find((p) => p.id === id) ?? projects[0] ?? null;
  const setProjectId = useCallback((n: number) => { localStorage.setItem("pulse.project", String(n)); setId(n); }, []);
  const value = useMemo(() => ({ projects, project, setProjectId, loading: q.isLoading }), [projects, project, setProjectId, q.isLoading]);
  return <ProjectCtx.Provider value={value}>{children}</ProjectCtx.Provider>;
}
export const useProject = () => useContext(ProjectCtx);

/* ---------- уведомления ---------- */
type ToastKind = "ok" | "bad" | "info";
interface ToastItem { id: number; text: string; kind: ToastKind }
const ToastCtx = createContext<(text: string, kind?: ToastKind) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const push = useCallback((text: string, kind: ToastKind = "ok") => {
    const id = ++seq.current;
    setItems((x) => [...x, { id, text, kind }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), kind === "bad" ? 7000 : 3500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 w-[min(360px,calc(100vw-2rem))]" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className="fadein px-3.5 py-2.5 text-sm bg-ink text-paper shadow-lg" style={{ borderRadius: 3, borderLeft: `4px solid var(--${t.kind === "bad" ? "bad" : t.kind === "ok" ? "ok" : "info"})` }}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
export const useToast = () => useContext(ToastCtx);

/* ---------- тема ---------- */
export function useTheme() {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  const toggle = useCallback(() => {
    const next = !document.documentElement.classList.contains("dark");
    document.documentElement.classList.toggle("dark", next);
    localStorage.setItem("pulse.theme", next ? "dark" : "light");
    setDark(next);
  }, []);
  return { dark, toggle };
}

/* ---------- живые события (SSE) ---------- */
const EVENT_KEYS: Record<string, string[]> = {
  generation: ["generation-queue", "dashboard", "drafts"],
  pipeline: ["dashboard", "clusters", "cluster", "sources"],
  draft: ["drafts", "draft", "dashboard", "clusters"],
  publication: ["publications", "dashboard", "drafts", "draft"],
  source: ["sources", "dashboard"],
};

export function useLiveEvents(enabled: boolean): boolean {
  const qc = useQueryClient();
  const [live, setLive] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    const es = new EventSource("/api/events", { withCredentials: true });
    const pending = new Set<string>();
    let timer: ReturnType<typeof setTimeout> | undefined;
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    es.onmessage = (e) => {
      let type = "";
      try { type = JSON.parse(e.data).type; } catch { return; }
      (EVENT_KEYS[type] ?? []).forEach((k) => pending.add(k));
      clearTimeout(timer);
      timer = setTimeout(() => {
        pending.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
        pending.clear();
      }, 250);
    };
    return () => { clearTimeout(timer); es.close(); setLive(false); };
  }, [enabled, qc]);
  return live;
}
