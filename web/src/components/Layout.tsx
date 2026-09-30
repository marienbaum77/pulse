import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CalendarClock, FileText, LayoutDashboard, LogOut, Menu, Moon, Rss, Send, Settings2, SlidersHorizontal, Sun, TrendingUp } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useLiveEvents, useProject, useTheme } from "../lib/context";
import type { DraftListItem } from "../lib/types";

export function Layout() {
  const { user, logout, can } = useAuth();
  const { projects, project, setProjectId } = useProject();
  const live = useLiveEvents(!!user);
  const { dark, toggle } = useTheme();
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  useEffect(() => setOpen(false), [loc.pathname]);

  const pending = useQuery({
    queryKey: ["drafts", project?.id, "pending_review"],
    queryFn: () => api<DraftListItem[]>("/drafts", { params: { project_id: project!.id, status: "pending_review", limit: 50 } }),
    enabled: !!project,
  });
  const nPending = pending.data?.length ?? 0;

  const items = [
    { to: "/", label: "Обзор", icon: LayoutDashboard, end: true },
    { to: "/trends", label: "Сюжеты", icon: TrendingUp },
    { to: "/drafts", label: "Черновики", icon: FileText, badge: nPending },
    { to: "/publications", label: "Публикации", icon: Send },
    { to: "/sources", label: "Источники", icon: Rss },
    { to: "/channels", label: "Каналы и расписание", icon: CalendarClock },
    { to: "/project", label: "Настройки проекта", icon: SlidersHorizontal },
    ...(can("admin") ? [{ to: "/admin", label: "Система", icon: Settings2 }] : []),
  ];

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[236px_1fr]">
      <div className="lg:hidden flex items-center justify-between px-4 h-12 rule-b bg-surface sticky top-0 z-30">
        <button className="btn btn-ghost btn-sm" onClick={() => setOpen(true)} aria-label="Меню"><Menu size={18} /></button>
        <span className="font-serif font-semibold">Pulse</span>
        <span className="w-8" />
      </div>
      {open && <div className="fixed inset-0 bg-black/40 z-40 lg:hidden" onClick={() => setOpen(false)} />}
      <aside
        className={`fixed lg:sticky top-0 left-0 z-50 h-screen w-[236px] flex flex-col bg-surface transition-transform lg:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}
        style={{ borderRight: "1px solid var(--rule)" }}
      >
        <div className="px-5 pt-5 pb-4">
          <div className="flex items-center gap-2.5">
            <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden>
              <rect width="32" height="32" rx="5" fill="var(--brand)" />
              <path d="M4 17h6l3-8 5 15 3-7h7" fill="none" stroke="var(--mark)" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className="font-serif text-[21px] font-semibold tracking-tight">Pulse</span>
          </div>
          {projects.length > 0 && (
            <select
              className="field mt-4" aria-label="Проект" value={project?.id ?? ""}
              onChange={(e) => setProjectId(Number(e.target.value))}
            >
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          )}
        </div>
        <nav className="flex-1 overflow-y-auto px-2.5 pb-3">
          {items.map((it) => (
            <NavLink
              key={it.to} to={it.to} end={it.end}
              className={({ isActive }) => `flex items-center gap-3 px-3 h-9 text-[14px] mb-0.5 ${isActive ? "font-semibold" : "text-muted hover:text-ink"}`}
              style={({ isActive }) => ({ borderRadius: 3, background: isActive ? "var(--sunken)" : undefined, color: isActive ? "var(--ink)" : undefined })}
            >
              <it.icon size={17} strokeWidth={1.9} />
              <span className="flex-1 truncate">{it.label}</span>
              {"badge" in it && it.badge ? (
                <span className="num text-[12px] font-semibold px-1.5 min-w-5 text-center" style={{ background: "var(--mark)", color: "var(--mark-ink)", borderRadius: 2 }}>{it.badge}</span>
              ) : null}
            </NavLink>
          ))}
        </nav>
        <div className="px-4 py-3.5 rule-t text-[13px] space-y-2.5">
          <div className="flex items-center gap-2 text-muted" title="События приходят с сервера сразу, без обновления страницы">
            <span className="inline-block w-2 h-2 rounded-full" style={{ background: live ? "var(--ok)" : "var(--muted)" }} />
            {live ? "Обновляется в реальном времени" : "Нет живого соединения"}
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="truncate text-muted" title={user?.email}>{user?.email}</span>
            <span className="flex shrink-0">
              <button className="btn btn-ghost btn-sm" onClick={toggle} aria-label={dark ? "Светлая тема" : "Тёмная тема"}>{dark ? <Sun size={15} /> : <Moon size={15} />}</button>
              <button className="btn btn-ghost btn-sm" onClick={() => logout()} aria-label="Выйти"><LogOut size={15} /></button>
            </span>
          </div>
        </div>
      </aside>
      <main className="min-w-0 px-5 sm:px-8 lg:px-10 py-8 max-w-[1240px] w-full">
        <Outlet />
      </main>
    </div>
  );
}
