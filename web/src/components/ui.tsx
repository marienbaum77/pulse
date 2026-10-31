import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import type { Tone } from "../lib/labels";

export function Field({
  label,
  hint,
  children,
  className = "",
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`block ${className}`}>
      <span className="label block font-mono uppercase text-[11px] tracking-widest text-muted mb-1.5 select-none">
        {label}
      </span>
      {children}
      {hint && <span className="hint block mt-1.5 text-[12px] text-muted/80 leading-relaxed">{hint}</span>}
    </label>
  );
}

/** Числовое поле. Пока человек печатает, значение не обрезается по границам: иначе «6» на пути к 600 в поле
 *  с минимумом 200 сразу превращалось в 200. Значение в границах уходит в форму сразу, остальное приводится к границам при уходе из поля. */
export function NumberInput({
  value,
  onChange,
  min,
  max,
  step = 1,
  fallback,
  className = "",
  disabled,
}: {
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
  step?: number;
  fallback?: number;
  className?: string;
  disabled?: boolean;
}) {
  const [text, setText] = useState(String(value));
  const [focused, setFocused] = useState(false);
  useEffect(() => {
    if (!focused) setText(String(value));
  }, [value, focused]);

  const parse = (raw: string) => {
    const n = Number(raw.replace(",", "."));
    return raw.trim() === "" || !Number.isFinite(n) ? null : step < 1 ? n : Math.round(n);
  };

  return (
    <input
      className={`field num rounded-xl font-mono text-sm px-3 py-2 bg-surface border border-rule focus:border-ink transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${className}`}
      type="number"
      inputMode={step < 1 ? "decimal" : "numeric"}
      min={min}
      max={max}
      step={step}
      value={text}
      disabled={disabled}
      onFocus={() => setFocused(true)}
      onChange={(e) => {
        setText(e.target.value);
        const n = parse(e.target.value);
        if (n !== null && n >= min && n <= max) onChange(n);
      }}
      onBlur={(e) => {
        setFocused(false);
        const n = parse(e.target.value);
        const v = n === null ? (fallback ?? value) : Math.max(min, Math.min(max, n));
        onChange(v);
        setText(String(v));
      }}
    />
  );
}

const TONE_VAR: Record<Tone, string> = {
  ok: "var(--ok)",
  warn: "var(--warn)",
  bad: "var(--bad)",
  info: "var(--info)",
  muted: "var(--muted)",
};

export function Badge({
  tone,
  children,
  className = "",
}: {
  tone: Tone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border border-rule/80 px-2.5 py-0.5 text-[11px] font-mono tracking-wider uppercase bg-surface/60 text-ink whitespace-nowrap select-none ${className}`}
    >
      <span
        aria-hidden
        className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
        style={{
          backgroundColor: TONE_VAR[tone],
          boxShadow: tone === "muted" ? "none" : `0 0 5px ${TONE_VAR[tone]}`,
        }}
      />
      {children}
    </span>
  );
}

export function PageHeader({
  title,
  sub,
  actions,
  className = "",
}: {
  title: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header className={`flex flex-wrap items-end justify-between gap-x-6 gap-y-3 mb-8 ${className}`}>
      <div className="min-w-0">
        <h1 className="font-sans text-[30px] leading-tight font-bold tracking-tight text-ink">
          {title}
        </h1>
        {sub && <p className="text-muted text-[15px] mt-2 max-w-[72ch] leading-relaxed">{sub}</p>}
      </div>
      {actions && <div className="flex items-center gap-2.5 flex-wrap">{actions}</div>}
    </header>
  );
}

export function Empty({
  title,
  text,
  action,
  className = "",
}: {
  title: string;
  text?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`border border-dashed border-rule rounded-2xl bg-surface/40 p-10 text-center flex flex-col items-center justify-center my-4 ${className}`}
    >
      <div
        className="w-10 h-10 rounded-2xl border border-rule/70 bg-sunken/40 flex items-center justify-center mb-4"
        aria-hidden
      >
        <span className="inline-block w-2 h-2 rounded-full bg-accent shadow-[0_0_8px_var(--accent,#d71921)]" />
      </div>
      <h3 className="font-sans font-bold text-lg text-ink tracking-tight">{title}</h3>
      {text && <div className="text-muted text-sm mt-1.5 max-w-[46ch] leading-relaxed">{text}</div>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function Spinner({
  label = "Загрузка",
  className = "",
}: {
  label?: string;
  className?: string;
}) {
  return (
    <div
      className={`py-10 flex items-center justify-center gap-2.5 text-xs font-mono uppercase tracking-widest text-muted select-none ${className}`}
      role="status"
    >
      <span className="relative flex h-2 w-2 shrink-0" aria-hidden>
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-accent opacity-75" />
        <span className="relative inline-flex rounded-full h-2 w-2 bg-accent shadow-[0_0_6px_var(--accent,#d71921)]" />
      </span>
      <span>{label}…</span>
    </div>
  );
}

export function ErrorNote({
  error,
  className = "",
}: {
  error: unknown;
  className?: string;
}) {
  const message = error instanceof Error ? error.message : typeof error === "string" ? error : "Что-то пошло не так";
  return (
    <div
      role="alert"
      className={`my-4 px-4 py-3 rounded-xl border border-bad/30 bg-bad/5 text-bad text-xs font-mono flex items-center gap-2.5 ${className}`}
    >
      <span className="w-1.5 h-1.5 rounded-full bg-bad shrink-0 shadow-[0_0_6px_var(--bad,#ef4444)]" />
      <span>{message}</span>
    </div>
  );
}

export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  wide,
  className = "",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  className?: string;
}) {
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
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
      className={`m-auto p-0 bg-surface text-ink rounded-2xl border border-rule shadow-2xl overflow-hidden outline-none backdrop:backdrop-blur-md backdrop:bg-black/80 ${className}`}
      style={{
        width: wide ? "min(720px, 94vw)" : "min(520px, 94vw)",
        maxHeight: "90vh",
      }}
    >
      {open && (
        <div className="flex flex-col max-h-[90vh]">
          <div className="flex items-center justify-between px-6 py-4.5 border-b border-rule bg-surface">
            <h2 className="text-lg font-sans font-bold tracking-tight text-ink">{title}</h2>
            <button
              type="button"
              className="inline-flex items-center justify-center w-8 h-8 rounded-full border border-rule/70 hover:border-ink hover:bg-sunken text-muted hover:text-ink transition-colors cursor-pointer"
              onClick={onClose}
              aria-label="Закрыть"
            >
              <X size={15} />
            </button>
          </div>
          <div className="px-6 py-5 overflow-y-auto space-y-4">{children}</div>
          {footer && (
            <div className="px-6 py-4 border-t border-rule flex justify-end gap-2.5 bg-sunken/30">
              {footer}
            </div>
          )}
        </div>
      )}
    </dialog>
  );
}

export function Drawer({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end">
      <div
        className="fixed inset-0 bg-black/60 backdrop-blur-sm transition-opacity"
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        className="slidein relative w-[min(580px,100vw)] h-full bg-surface border-l border-rule shadow-2xl overflow-y-auto flex flex-col z-10"
        role="dialog"
        aria-label="Подробности"
      >
        <div className="sticky top-0 bg-surface/95 backdrop-blur-md z-10 flex items-start justify-between gap-4 px-6 py-4.5 border-b border-rule">
          <h2 className="text-lg font-sans font-bold leading-snug tracking-tight text-ink">
            {title}
          </h2>
          <button
            type="button"
            className="inline-flex items-center justify-center w-8 h-8 rounded-full border border-rule/70 hover:border-ink hover:bg-sunken text-muted hover:text-ink transition-colors shrink-0 cursor-pointer"
            onClick={onClose}
            aria-label="Закрыть"
          >
            <X size={15} />
          </button>
        </div>
        <div className="px-6 py-6 flex-1">{children}</div>
      </aside>
    </div>,
    document.body,
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  className = "",
  disabled = false,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  className?: string;
  disabled?: boolean;
}) {
  return (
    <label
      className={`inline-flex items-center gap-3 select-none text-sm ${
        disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"
      } ${className}`}
    >
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => !disabled && onChange(!checked)}
        className={`relative inline-flex items-center w-11 h-6 rounded-full transition-colors duration-200 shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink focus-visible:ring-offset-2 ${
          checked ? "bg-accent" : "bg-sunken border border-rule"
        }`}
      >
        <span
          aria-hidden
          className={`w-4 h-4 rounded-full bg-white shadow-sm transition-transform duration-200 ease-out transform ${
            checked ? "translate-x-6" : "translate-x-1"
          }`}
        />
      </button>
      <span className="text-sm font-medium text-ink">{label}</span>
    </label>
  );
}

/** Вкладки в стиле Nothing OS: сегментированные pill-кнопки с активным LED-индикатором и счетчиком */
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  className = "",
}: {
  tabs: { id: T; label: string; count?: number }[];
  value: T;
  onChange: (id: T) => void;
  className?: string;
}) {
  return (
    <div
      role="tablist"
      className={`inline-flex items-center gap-1 p-1 rounded-full bg-sunken/60 border border-rule/50 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden max-w-full select-none ${className}`}
    >
      {tabs.map((t) => {
        const active = t.id === value;
        return (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(t.id)}
            className={`px-4 py-1.5 rounded-full text-[13px] font-mono uppercase tracking-wider transition-colors duration-100 inline-flex items-center gap-2 shrink-0 cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-accent ${
              active
                ? "bg-surface text-ink font-semibold shadow-sm border border-rule/60"
                : "text-muted hover:text-ink hover:bg-surface/40 border border-transparent"
            }`}
          >
            {active && (
              <span
                aria-hidden
                className="w-1.5 h-1.5 rounded-full bg-accent shrink-0 shadow-[0_0_6px_var(--accent,#d71921)]"
              />
            )}
            <span>{t.label}</span>
            {t.count !== undefined && (
              <span
                className={`px-2 py-0.5 rounded-full text-[11px] font-mono tabular-nums leading-none ${
                  active
                    ? "bg-accent/15 text-accent font-bold"
                    : "bg-surface/80 text-muted"
                }`}
              >
                {t.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
