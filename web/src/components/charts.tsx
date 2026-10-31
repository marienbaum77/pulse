import { ago, dateTime, fmtNum, plural, shortDay } from "../lib/format";
import type { ScorePart } from "../lib/types";

/** Столбики по дням в монохромной стилистике Nothing UI: черные/серые бары с акцентом Nothing Red на текущем дне. */
export function DailyBars({ data }: { data: { date: string; items: number }[] }) {
  const W = 640, H = 168, top = 20, bottom = 24;
  const max = Math.max(1, ...data.map((d) => d.items));
  const slot = W / Math.max(data.length, 1);
  const bw = Math.min(46, slot * 0.6);
  const labelEvery = Math.ceil(data.length / 7);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto select-none" role="img" aria-label="Материалов по дням">
      <line x1="0" x2={W} y1={H - bottom} y2={H - bottom} stroke="var(--rule)" strokeDasharray="2 3" />
      {data.map((d, i) => {
        const h = (d.items / max) * (H - top - bottom);
        const x = i * slot + (slot - bw) / 2;
        const today = i === data.length - 1;
        return (
          <g key={d.date} className="group">
            <rect
              x={x}
              y={H - bottom - h}
              width={bw}
              height={Math.max(h, d.items ? 2 : 0)}
              fill={today ? "var(--accent)" : "var(--ink)"}
              opacity={today ? 1 : 0.35}
              rx="2"
            >
              <title>{`${shortDay(d.date)}: ${fmtNum(d.items)} ${plural(d.items, "материал", "материала", "материалов")}`}</title>
            </rect>
            {(data.length <= 14 || d.items === max) && d.items > 0 && (
              <text
                x={x + bw / 2}
                y={H - bottom - h - 5}
                textAnchor="middle"
                fontSize="10"
                fill={today ? "var(--accent)" : "var(--muted)"}
                className="font-mono font-medium"
              >
                {d.items}
              </text>
            )}
            {(i % labelEvery === 0 || today) && (
              <text
                x={x + bw / 2}
                y={H - 7}
                textAnchor="middle"
                fontSize="10"
                fill={today ? "var(--ink)" : "var(--muted)"}
                className="font-mono uppercase tracking-wider"
              >
                {shortDay(d.date)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

/** Когда приходили материалы сюжета внутри окна кластеризации в точечной (dot-matrix) стилистике Nothing UI.
 * Левее — старые материалы (начало окна), правее — свежие (сейчас). */
export function ArrivalStrip({ arrivals, windowHours, width = 150 }: { arrivals: string[] | null; windowHours: number; width?: number }) {
  const now = Date.now(), span = windowHours * 3600_000, h = 18;
  const list = arrivals ?? [];
  const pts = list.map((a) => ({
    time: a,
    pos: (new Date(a).getTime() - (now - span)) / span,
  })).filter((item) => item.pos >= -0.02);

  const tooltip = `Хронология публикаций за ${windowHours} ч:\nЛевее — ${windowHours} ч назад, правее — сейчас.\nВсего материалов: ${pts.length}`;

  return (
    <div className="inline-flex flex-col items-end" title={tooltip}>
      <svg width={width} height={h} viewBox={`0 0 ${width} ${h}`} role="img" aria-label={tooltip} className="overflow-visible select-none">
        {/* Пунктирная точечная шкала времени */}
        <line x1="3" x2={width - 3} y1={h / 2} y2={h / 2} stroke="var(--rule)" strokeDasharray="1 3" strokeWidth="1.5" />
        {/* Маркер текущего времени */}
        <circle cx={width - 3} cy={h / 2} r="1.5" fill="var(--muted)" opacity={0.6} />
        {pts.map((pt, i) => {
          const isLatest = i === pts.length - 1;
          const cx = 3 + Math.min(Math.max(pt.pos, 0), 1) * (width - 6);
          return (
            <circle
              key={i}
              cx={cx}
              cy={h / 2}
              r={isLatest ? 2.6 : 2}
              fill={isLatest ? "var(--accent)" : "var(--ink)"}
              opacity={isLatest ? 1 : 0.45 + 0.5 * Math.min(Math.max(pt.pos, 0), 1)}
            >
              <title>{dateTime(pt.time)} ({ago(pt.time)})</title>
            </circle>
          );
        })}
      </svg>
    </div>
  );
}

/**
 * @deprecated Вес сюжета (NWS) упразднён в редизайне Nothing UI.
 * Сохранён как заглушка null для безопасной компиляции страниц до завершения M2.
 */
export function Score(_props: { value?: number | null | undefined; width?: number }): null {
  return null;
}

/**
 * @deprecated Разложение NWS упразднено в редизайне Nothing UI.
 * Сохранено как заглушка null для безопасной компиляции страниц до завершения M2.
 */
export function Breakdown(_props: { parts?: Record<string, ScorePart> }): null {
  return null;
}
