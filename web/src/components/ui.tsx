import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import type { Tone } from "../lib/labels";

export function Field({ label, hint, children, className = "" }: { label: string; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={`block ${className}`}>
      <span className="label">{label}</span>
      {children}
      {hint && <span className="hint block mt-1">{hint}</span>}
    </label>
  );
}

const TONE_VAR: Record<Tone, string> = { ok: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)", info: "var(--info)", muted: "var(--muted)" };

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[12.5px] font-medium whitespace-nowrap" style={{ color: TONE_VAR[tone] }}>
      <span aria-hidden className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: TONE_VAR[tone] }} />
      {children}
    </span>
  );
}

export function PageHeader({ title, sub, actions }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3 mb-7">
      <div className="min-w-0">
        <h1 className="text-[28px] leading-tight font-semibold">{title}</h1>
        {sub && <p className="text-muted mt-1.5 max-w-[68ch]">{sub}</p>}
      </div>
      {actions && <div className="flex items-center gap-2 flex-wrap">{actions}</div>}
    </header>
  );
}

export function Empty({ title, text, action }: { title: string; text?: ReactNode; action?: ReactNode }) {
  return (
    <div className="py-12 px-2 max-w-[52ch]">
      <p className="font-serif text-xl font-semibold">{title}</p>
      {text && <p className="text-muted mt-2">{text}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Spinner({ label = "Загрузка" }: { label?: string }) {
  return <div className="py-10 text-muted text-sm" role="status">{label}…</div>;
}

export function ErrorNote({ error }: { error: unknown }) {
  return <p className="py-6 text-bad text-sm" role="alert">{error instanceof Error ? error.message : "Что-то пошло не так"}</p>;
}

export function Dialog({ open, onClose, title, children, footer, wide }: { open: boolean; onClose: () => void; title: string; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => { if (e.target === ref.current) onClose(); }}
      className="m-auto p-0 bg-surface text-ink backdrop:bg-black/50"
      style={{ borderRadius: 4, border: "1px solid var(--rule)", width: wide ? "min(720px, 94vw)" : "min(520px, 94vw)", maxHeight: "90vh" }}
    >
      {open && (
        <div className="flex flex-col max-h-[90vh]">
          <div className="flex items-center justify-between px-5 py-4 rule-b">
            <h2 className="text-lg font-semibold">{title}</h2>
            <button className="btn btn-ghost btn-sm" onClick={onClose} aria-label="Закрыть"><X size={16} /></button>
          </div>
          <div className="px-5 py-5 overflow-y-auto space-y-4">{children}</div>
          {footer && <div className="px-5 py-3.5 rule-t flex justify-end gap-2 bg-sunken/40">{footer}</div>}
        </div>
      )}
    </dialog>
  );
}

export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  // в портале, чтобы панель всегда считалась от окна браузера и занимала его высоту целиком, что бы ни лежало в странице
  return createPortal(
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <aside className="slidein absolute right-0 top-0 bottom-0 w-[min(560px,100vw)] bg-surface overflow-y-auto" style={{ borderLeft: "1px solid var(--rule)" }} role="dialog" aria-label="Подробности">
        <div className="sticky top-0 bg-surface z-10 flex items-start justify-between gap-4 px-6 py-4 rule-b">
          <h2 className="text-lg font-semibold leading-snug">{title}</h2>
          <button className="btn btn-ghost btn-sm shrink-0" onClick={onClose} aria-label="Закрыть"><X size={16} /></button>
        </div>
        <div className="px-6 py-5">{children}</div>
      </aside>
    </div>,
    document.body,
  );
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="inline-flex items-center gap-2.5 cursor-pointer select-none text-sm">
      <button
        type="button" role="switch" aria-checked={checked} onClick={() => onChange(!checked)}
        className="relative w-9 h-5 shrink-0 transition-colors"
        style={{ borderRadius: 3, background: checked ? "var(--brand)" : "var(--rule)" }}
      >
        <span className="absolute top-0.5 w-4 h-4 transition-all" style={{ borderRadius: 2, left: checked ? 18 : 2, background: checked ? "var(--brand-ink)" : "var(--surface)" }} />
      </button>
      {label}
    </label>
  );
}


/** Вкладки. Подчёркивание рисуется inset-тенью внутри кнопки, поэтому содержимое не выходит за границы и ползунка нет. */
export function Tabs<T extends string>({ tabs, value, onChange, className = "" }: { tabs: { id: T; label: string; count?: number }[]; value: T; onChange: (id: T) => void; className?: string }) {
  return (
    <div role="tablist" className={`flex gap-1 rule-b overflow-x-auto overflow-y-hidden [scrollbar-width:none] [&::-webkit-scrollbar]:hidden ${className}`}>
      {tabs.map((t) => {
        const active = t.id === value;
        return (
          <button
            key={t.id} role="tab" aria-selected={active} onClick={() => onChange(t.id)}
            className="px-3.5 h-10 text-[14px] whitespace-nowrap shrink-0"
            style={{ boxShadow: active ? "inset 0 -2px 0 var(--brand)" : "none", fontWeight: active ? 600 : 400, color: active ? "var(--ink)" : "var(--muted)" }}
          >
            {t.label}{t.count !== undefined && <> <span className="num text-muted text-[12.5px]">{t.count}</span></>}
          </button>
        );
      })}
    </div>
  );
}
