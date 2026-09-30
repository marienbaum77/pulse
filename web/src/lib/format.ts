const nf = new Intl.NumberFormat("ru-RU");
export const fmtNum = (n: number) => nf.format(n);

export function plural(n: number, one: string, few: string, many: string): string {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 0) return inFuture(-s);
  if (s < 45) return "только что";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} мин назад`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} ч назад`;
  const d = Math.round(h / 24);
  return `${d} ${plural(d, "день", "дня", "дней")} назад`;
}

function inFuture(s: number): string {
  const m = Math.round(s / 60);
  if (m < 60) return `через ${Math.max(m, 1)} мин`;
  const h = Math.round(m / 60);
  if (h < 24) return `через ${h} ч`;
  const d = Math.round(h / 24);
  return `через ${d} ${plural(d, "день", "дня", "дней")}`;
}

export const dateTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";

export const shortDay = (iso: string) => new Date(iso + "T00:00:00").toLocaleDateString("ru-RU", { day: "numeric", month: "short" });

export const pct = (x: number) => `${Math.round(x * 100)}%`;

/** Как на сервере (textutil.strip_citations): маркеры [1][2] в опубликованный пост не попадают. */
export function stripCitations(text: string, valid: Set<number> = new Set()): string {
  const out = text.replace(/([ \t]*)\[(\d+)\]/g, (m, ws: string, n: string, offset: number) => {
    const prev = offset > 0 ? text[offset - 1] : " ";
    const attached = !ws && /[\p{L}\p{N}_]/u.test(prev);
    return attached && !valid.has(Number(n)) ? m : "";
  });
  return out.replace(/[ \t]{2,}/g, " ").replace(/[ \t]+([.,;:!?…])/g, "$1").replace(/^[ \t]+|[ \t]+$/gm, "").trim();
}
