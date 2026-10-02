import { SCORE_HINTS, SCORE_LABELS } from "../lib/labels";
import { fmtNum, plural, shortDay } from "../lib/format";
import type { ScorePart } from "../lib/types";

/** Столбики по дням: вид «сколько материалов вышло». Сегодняшний день выделен маркером. */
export function DailyBars({ data }: { data: { date: string; items: number }[] }) {
  const W = 640, H = 168, top = 20, bottom = 24;
  const max = Math.max(1, ...data.map((d) => d.items));
  const slot = W / Math.max(data.length, 1);
  const bw = Math.min(46, slot * 0.6);
  const labelEvery = Math.ceil(data.length / 7);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto" role="img" aria-label="Материалов по дням">
      <line x1="0" x2={W} y1={H - bottom} y2={H - bottom} stroke="var(--rule)" />
      {data.map((d, i) => {
        const h = (d.items / max) * (H - top - bottom);
        const x = i * slot + (slot - bw) / 2;
        const today = i === data.length - 1;
        return (
          <g key={d.date}>
            <rect x={x} y={H - bottom - h} width={bw} height={Math.max(h, d.items ? 1 : 0)} fill={today ? "var(--mark)" : "var(--brand)"} opacity={today ? 1 : 0.85} rx="1.5">
              <title>{`${shortDay(d.date)}: ${fmtNum(d.items)} ${plural(d.items, "материал", "материала", "материалов")}`}</title>
            </rect>
            {(data.length <= 14 || d.items === max) && d.items > 0 && (
              <text x={x + bw / 2} y={H - bottom - h - 5} textAnchor="middle" fontSize="11" fill="var(--muted)" className="num">{d.items}</text>
            )}
            {(i % labelEvery === 0 || today) && (
              <text x={x + bw / 2} y={H - 7} textAnchor="middle" fontSize="11" fill="var(--muted)">{shortDay(d.date)}</text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

/** Когда приходили материалы сюжета внутри окна кластеризации: одна точка — один материал. */
export function ArrivalStrip({ arrivals, windowHours, width = 150 }: { arrivals: string[] | null; windowHours: number; width?: number }) {
  const now = Date.now(), span = windowHours * 3600_000, h = 18;
  const pts = (arrivals ?? []).map((a) => (new Date(a).getTime() - (now - span)) / span).filter((p) => p >= -0.02);
  return (
    <svg width={width} height={h} viewBox={`0 0 ${width} ${h}`} role="img" aria-label={`${pts.length} материалов за ${windowHours} ч`}>
      <line x1="2" x2={width - 2} y1={h / 2} y2={h / 2} stroke="var(--rule)" />
      {pts.map((p, i) => (
        <circle key={i} cx={2 + Math.min(Math.max(p, 0), 1) * (width - 4)} cy={h / 2} r="2.6" fill="var(--brand)" opacity={0.35 + 0.65 * Math.min(Math.max(p, 0), 1)} />
      ))}
    </svg>
  );
}

/** Вес сюжета: жёлтая «маркерная» полоса; число — NWS ×100. */
export function Score({ value, width = 112 }: { value: number | null | undefined; width?: number }) {
  const v = value ?? 0;
  return (
    <div className="marker-track shrink-0" style={{ width }} title={value == null ? "Вес ещё не рассчитан" : `NWS ${v.toFixed(3)}`}>
      <div className="marker-fill" style={{ width: `${Math.max(0, Math.min(1, v)) * 100}%` }} />
      <span className="marker-num num">{value == null ? "—" : Math.round(v * 100)}</span>
    </div>
  );
}

/** Разложение NWS: полоса — значение признака (0–1); для близости темы число справа — само значение. */
export function Breakdown({ parts }: { parts: Record<string, ScorePart> }) {
  return (
    <div className="space-y-2.5">
      {Object.entries(parts).map(([key, p]) => {
        const neg = p.contribution < 0;
        const topicFit = key === "topic_fit";
        return (
          <div key={key} className="grid grid-cols-[150px_1fr_84px] items-center gap-3 text-[13px]" title={SCORE_HINTS[key]}>
            <span className={neg ? "text-bad" : ""}>{SCORE_LABELS[key] ?? key}</span>
            <div className="h-2 bg-sunken" style={{ borderRadius: 1 }}>
              {!p.neutral && <div className="h-full" style={{ width: `${Math.min(1, p.value) * 100}%`, background: neg ? "var(--bad)" : "var(--brand)", opacity: neg ? 0.8 : 0.75, borderRadius: 1 }} />}
            </div>
            <span className={`num text-right ${neg ? "text-bad" : p.neutral ? "text-muted" : ""}`} title={p.neutral ? "Не измерено: без настоящей модели эмбеддингов или без темы проекта значение нейтральное и одинаковое у всех сюжетов" : topicFit ? `Косинусное сходство заголовков, не вероятность. Вклад в общий вес: +${(p.contribution * 100).toFixed(1)} пункта` : undefined}>
              {p.neutral ? "н/д" : topicFit ? p.value.toFixed(3) : `${p.contribution > 0 ? "+" : ""}${(p.contribution * 100).toFixed(1)}`}
            </span>
          </div>
        );
      })}
      <p className="hint pt-1">Для близости к теме справа указано косинусное сходство заголовков (0–1), не вероятность; наведите, чтобы увидеть вклад в общий вес. У остальных признаков справа указан вклад в пунктах.</p>
    </div>
  );
}
