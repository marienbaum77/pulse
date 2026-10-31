import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { BarChart3, CalendarClock, CalendarDays, FileText, LayoutDashboard, LogOut, Menu, Moon, Rss, Send, Settings2, SlidersHorizontal, Sun, TrendingUp } from "lucide-react";
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
    { to: "/plan", label: "Контент-план", icon: CalendarDays },
    { to: "/publications", label: "Публикации", icon: Send },
    { to: "/stats", label: "Статистика", icon: BarChart3 },
    { to: "/sources", label: "Источники", icon: Rss },
    { to: "/channels", label: "Каналы и расписание", icon: CalendarClock },
    { to: "/project", label: "Настройки проекта", icon: SlidersHorizontal },
    ...(can("admin") ? [{ to: "/admin", label: "Система", icon: Settings2 }] : []),
  ];

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[236px_1fr]">
      {/* Mobile Topbar */}
      <div className="lg:hidden flex items-center justify-between px-4 h-13 border-b border-rule bg-surface/90 backdrop-blur-md sticky top-0 z-30">
        <button
          className="inline-flex items-center justify-center w-8 h-8 rounded-full border border-rule/60 bg-surface hover:bg-sunken text-muted hover:text-ink cursor-pointer transition-colors"
          onClick={() => setOpen(true)}
          aria-label="Меню"
        >
          <Menu size={17} />
        </button>
        <div className="flex items-center gap-2">
          <div className="relative flex items-center justify-center w-6 h-6 rounded-md bg-sunken border border-rule/80 overflow-hidden">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="text-ink">
              <path d="M2 13h5l3-7 4 14 3-7h5" />
            </svg>
            <span className="absolute top-0.5 right-0.5 w-1 h-1 rounded-full" style={{ background: "var(--accent)" }} />
          </div>
          <span className="font-sans font-bold text-[16px] tracking-tight text-ink">Pulse</span>
        </div>
        <span className="w-8" />
      </div>

      {open && <div className="fixed inset-0 bg-black/60 backdrop-blur-xs z-40 lg:hidden" onClick={() => setOpen(false)} />}

      {/* Desktop & Mobile Drawer Sidebar */}
      <aside
        className={`fixed lg:sticky top-0 left-0 z-50 h-screen w-[236px] flex flex-col bg-surface border-r border-rule transition-transform lg:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}
      >
        <div className="px-5 pt-5 pb-4">
          <div className="flex items-center gap-2.5">
            {/* Nothing Pulse Glyph */}
            <div className="relative flex items-center justify-center w-8 h-8 rounded-lg bg-sunken border border-rule/80 overflow-hidden shrink-0 shadow-2xs">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="text-ink">
                <path d="M2 13h5l3-7 4 14 3-7h5" />
              </svg>
              <span className="absolute top-1 right-1 w-1.5 h-1.5 rounded-full animate-pulse" style={{ background: "var(--accent)" }} />
            </div>
            <span className="font-sans text-[20px] font-bold tracking-tight text-ink">Pulse</span>
          </div>
          {projects.length > 0 && (
            <select
              className="field mt-4 rounded-xl text-[13px] font-sans" aria-label="Проект" value={project?.id ?? ""}
              onChange={(e) => setProjectId(Number(e.target.value))}
            >
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          )}
        </div>

        <nav className="flex-1 overflow-y-auto px-3 pb-3 space-y-0.5">
          {items.map((it) => (
            <NavLink
              key={it.to} to={it.to} end={it.end}
              className={({ isActive }) =>
                `group flex items-center gap-2.5 px-3.5 h-10 text-[14px] rounded-full transition-colors duration-100 select-none focus:outline-none ${
                  isActive
                    ? "bg-sunken text-ink font-medium border border-rule/70"
                    : "text-muted hover:text-ink hover:bg-sunken/40 border border-transparent"
                }`
              }
            >
              {({ isActive }) => (
                <>
                  <it.icon size={17} strokeWidth={isActive ? 2.1 : 1.8} className={isActive ? "text-ink" : "text-muted group-hover:text-ink transition-colors"} />
                  <span className="flex-1 truncate">{it.label}</span>
                  {isActive && !("badge" in it && Boolean(it.badge)) && (
                    <span
                      className="w-1.5 h-1.5 rounded-full shrink-0"
                      style={{ background: "var(--accent)" }}
                      aria-hidden="true"
                    />
                  )}
                  {"badge" in it && Boolean(it.badge) ? (
                    <span
                      className="inline-flex items-center justify-center px-1.5 min-w-[20px] h-[18px] text-[10px] font-mono font-bold tracking-wider rounded-full text-white shrink-0"
                      style={{ background: "var(--accent)" }}
                    >
                      {it.badge}
                    </span>
                  ) : null}
                </>
              )}
            </NavLink>
          ))}
        </nav>

        {/* Sidebar Footer */}
        <div className="px-4 py-3.5 border-t border-rule text-[13px] space-y-2.5">
          {/* Pulsing SSE Indicator */}
          <div className="flex items-center gap-2.5 text-muted text-[12px]" title="События приходят с сервера сразу, без обновления страницы">
            <span className="relative flex h-2 w-2 shrink-0">
              {live && (
                <span
                  className="animate-ping absolute inline-flex h-full w-full rounded-full opacity-60"
                  style={{ background: "var(--ok)" }}
                />
              )}
              <span
                className="relative inline-flex rounded-full h-2 w-2"
                style={{ background: live ? "var(--ok)" : "var(--muted)" }}
              />
            </span>
            <span className="truncate">{live ? "В реальном времени" : "Нет живого соединения"}</span>
          </div>

          <div className="flex items-center justify-between gap-2 pt-1 border-t border-rule/40">
            <span className="truncate text-muted text-xs font-mono" title={user?.email}>{user?.email}</span>
            <span className="flex items-center gap-1 shrink-0">
              <button
                className="inline-flex items-center justify-center w-7 h-7 rounded-full border border-rule/60 bg-surface hover:bg-sunken text-muted hover:text-ink transition-colors cursor-pointer"
                onClick={toggle}
                aria-label={dark ? "Светлая тема" : "Тёмная тема"}
                title={dark ? "Светлая тема" : "Тёмная тема"}
              >
                {dark ? <Sun size={14} /> : <Moon size={14} />}
              </button>
              <button
                className="inline-flex items-center justify-center w-7 h-7 rounded-full border border-rule/60 bg-surface hover:bg-sunken text-muted hover:text-ink transition-colors cursor-pointer"
                onClick={() => logout()}
                aria-label="Выйти"
                title="Выйти"
              >
                <LogOut size={14} />
              </button>
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
