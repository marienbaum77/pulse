import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useProject } from "../lib/context";
import { ago, fmtNum } from "../lib/format";
import { DRAFT_STATUS, PUB_STATUS, SLOT_KINDS } from "../lib/labels";
import type { ContentPlan, PlanSlot } from "../lib/types";
import { NoProject } from "../components/NoProject";
import { Badge, Empty, ErrorNote, PageHeader, Spinner, Tabs } from "../components/ui";

const RANGES = [
  { id: "3", label: "3 дня" },
  { id: "7", label: "Неделя" },
  { id: "14", label: "2 недели" },
] as const;
type Range = (typeof RANGES)[number]["id"];
const CATCH_UP_MS = 10 * 60 * 1000;

const time = (iso: string) => new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
const dayKey = (iso: string) => new Date(iso).toLocaleDateString("sv-SE");

function dayTitle(key: string, today: string, tomorrow: string): string {
  if (key === today) return "Сегодня";
  if (key === tomorrow) return "Завтра";
  return new Date(key + "T00:00:00").toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" });
}

export default function Plan() {
  const { project, loading } = useProject();
  const [range, setRange] = useState<Range>("7");
  const q = useQuery({
    queryKey: ["content-plan", project?.id, range],
    queryFn: () => api<ContentPlan>("/content-plan", { params: { project_id: project!.id, days: Number(range) } }),
    enabled: !!project,
    refetchInterval: 30000,
  });

  if (loading) return <Spinner />;
  if (!project) return <NoProject />;
  const plan = q.data;
  const now = plan ? new Date(plan.now) : new Date();
  const today = dayKey(now.toISOString());
  const tomorrow = dayKey(new Date(now.getTime() + 86400000).toISOString());
  const days = new Map<string, PlanSlot[]>();
  for (const slot of plan?.slots ?? []) {
    const key = dayKey(slot.at);
    days.set(key, [...(days.get(key) ?? []), slot]);
  }

  return (
    <div className="fadein max-w-[920px]">
      <PageHeader
        title="Контент-план"
        sub={<>Слоты публикаций на ближайшие дни и итоги за последние сутки. Слоты задаются на странице <Link to="/channels" className="underline underline-offset-4">«Каналы и расписание»</Link>: время, вид материала и когда готовить черновик.</>}
        actions={<Tabs tabs={RANGES.map((r) => ({ ...r }))} value={range} onChange={setRange} />}
      />
      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {plan && plan.slots.length === 0 && (
        <Empty
          title="Слотов пока нет"
          text={<>Добавьте расписание на странице <Link to="/channels" className="underline underline-offset-4">«Каналы и расписание»</Link>. Например: «видео в 10:00», «статья в 14:00» и «статья в 19:00» — это план «1 видео и 2 статьи в день».</>}
        />
      )}
      {[...days.entries()].map(([key, slots]) => (
        <section key={key} className="mb-8">
          <h2 className="text-lg font-semibold mb-1 first-letter:uppercase">{dayTitle(key, today, tomorrow)}</h2>
          <ul>{slots.map((s) => <SlotRow key={`${s.schedule_id}-${s.at}`} slot={s} now={now} />)}</ul>
        </section>
      ))}
      {plan && plan.backlog.length > 0 && (
        <section className="mt-10">
          <h2 className="text-lg font-semibold mb-1">Ждут проверки вне плана</h2>
          <p className="hint mb-2">Черновики, созданные вручную или без слота: после утверждения уходят сразу.</p>
          <ul>
            {plan.backlog.map((d) => (
              <li key={d.id} className="rule-b py-2.5 flex items-baseline gap-3">
                <Link to={`/drafts/${d.id}`} className="font-serif text-[16px] hover:underline underline-offset-4 min-w-0 truncate">{d.title || "Без названия"}</Link>
                <span className="text-[13px] text-muted shrink-0">{ago(d.created_at)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function SlotRow({ slot, now }: { slot: PlanSlot; now: Date }) {
  const past = new Date(slot.at) <= now;
  const what = slot.content_kinds.length ? slot.content_kinds.map((k) => SLOT_KINDS[k]).join(", ") : "любой материал";
  const format = slot.kind === "digest" ? `дайджест из ${slot.top_n}` : slot.top_n > 1 ? `${slot.top_n} поста` : "пост";
  return (
    <li className="rule-b py-3 grid grid-cols-[64px_minmax(0,1fr)] gap-x-4 items-baseline">
      <span className={`num text-[17px] font-medium ${past ? "text-muted" : ""}`}>{time(slot.at)}</span>
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-3">
          <span className="font-medium">{slot.schedule_name}</span>
          <span className="eyebrow">{what} · {format}</span>
        </div>
        {slot.drafts.length > 0 ? (
          <ul className="mt-1 space-y-1">
            {slot.drafts.map((d) => {
              const sent = d.publications.find((p) => p.status === "sent");
              const pub = sent ?? d.publications[0];
              const badge = d.status === "approved" && pub ? PUB_STATUS[pub.status] : DRAFT_STATUS[d.status];
              return (
                <li key={d.id} className="flex items-baseline gap-3 min-w-0">
                  <Link to={`/drafts/${d.id}`} className="font-serif text-[16px] hover:underline underline-offset-4 min-w-0 truncate">{d.title || "Без названия"}</Link>
                  <Badge tone={badge.tone}>{badge.label}</Badge>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-[13px] mt-0.5"><SlotStatus slot={slot} past={past} now={now} /></p>
        )}
      </div>
    </li>
  );
}

function SlotStatus({ slot, past, now }: { slot: PlanSlot; past: boolean; now: Date }) {
  if (slot.outcome === "empty") return <span className="text-muted">Нечего было публиковать: подходящих материалов не нашлось</span>;
  if (slot.outcome === "failed") return <span className="text-bad">Генерация не удалась — подробности в черновиках и журнале</span>;
  if (slot.outcome === "queued") return <span className="text-muted">Черновик готовится…</span>;
  // Планировщик проверяет расписания раз в несколько секунд и догоняет слот в течение 10 минут.
  if (past && now.getTime() - new Date(slot.at).getTime() < CATCH_UP_MS) return <span className="text-muted">Запускается…</span>;
  if (past) return <span className="text-muted">Не запускался: воркер был остановлен</span>;
  const prepare = new Date(slot.prepare_at) > now && slot.lead_minutes > 0 ? `черновик будет готов в ${time(slot.prepare_at)}` : "черновик готовится ко времени слота";
  return (
    <span className="text-muted">
      {prepare}.{" "}
      {slot.candidates > 0
        ? `Подходящих материалов сейчас: ${fmtNum(slot.candidates)}.`
        : <span className="text-warn">Сейчас подходящих материалов нет — слот может остаться пустым.</span>}
    </span>
  );
}
