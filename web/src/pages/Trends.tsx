import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { ago, dateTime, plural } from "../lib/format";
import { CLUSTER_STATE, DRAFT_STATUS } from "../lib/labels";
import type { Cluster, ClusterDetail } from "../lib/types";
import { ArrivalStrip, Breakdown, Score } from "../components/charts";
import { Thumb } from "../components/Thumb";
import { NoProject } from "../components/NoProject";
import { Badge, Drawer, Empty, ErrorNote, PageHeader, Spinner, Tabs } from "../components/ui";

const TABS = [
  { id: "open", label: "Новые", match: (c: Cluster) => c.state === "open" },
  { id: "published", label: "Опубликованные", match: (c: Cluster) => c.state === "published" },
  { id: "other", label: "Устаревшие и исключённые", match: (c: Cluster) => c.state === "closed" || c.state === "excluded" },
] as const;

export default function Trends() {
  const { project, loading } = useProject();
  const [params, setParams] = useSearchParams();
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("open");
  const [singles, setSingles] = useState(false);
  const [showOffTopic, setShowOffTopic] = useState(false);
  const q = useQuery({
    queryKey: ["clusters", project?.id],
    queryFn: () => api<Cluster[]>("/clusters", { params: { project_id: project!.id, limit: 150 } }),
    enabled: !!project,
  });
  const toast = useToast();
  const qc = useQueryClient();
  const clearAll = useMutation({
    mutationFn: () => api<{ deleted: number; ignored_items: number }>("/clusters", { method: "DELETE", params: { project_id: project!.id } }),
    onSuccess: ({ deleted, ignored_items }) => {
      toast(`Удалено сюжетов: ${deleted}; материалы оставлены в базе: ${ignored_items}`);
      qc.invalidateQueries({ queryKey: ["clusters"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  if (loading) return <Spinner />;
  if (!project) return <NoProject />;
  const openId = Number(params.get("open")) || null;
  const tabDef = TABS.find((t) => t.id === tab)!;
  // одиночные заметки (меньше min_items материалов) — не сюжеты; по умолчанию скрыты, чтобы не засорять список
  const isSingle = (c: Cluster) => c.item_count < project.min_items;
  const visible = (c: Cluster) => singles || !isSingle(c);
  const belowTopicThreshold = (c: Cluster) => {
    const fit = c.score_breakdown?.topic_fit;
    return project.topic_threshold > 0 && c.state !== "published" && !!fit && !fit.neutral && fit.value < project.topic_threshold;
  };
  const topicVisible = (c: Cluster) => showOffTopic || !belowTopicThreshold(c);
  const rows = (q.data ?? []).filter(tabDef.match).filter(visible).filter(topicVisible);
  const counts = Object.fromEntries(TABS.map((t) => [t.id, (q.data ?? []).filter(t.match).filter(visible).filter(topicVisible).length]));
  const hiddenSingles = (q.data ?? []).filter((c) => TABS[0].match(c) && isSingle(c)).length;
  const hiddenOffTopic = (q.data ?? []).filter(tabDef.match).filter(visible).filter(belowTopicThreshold).length;

  return (
    <div className="fadein">
      <PageHeader
        title="Сюжеты"
        sub={`Материалы, объединённые по смыслу за последние ${project.window_hours} ч. Чем выше вес, тем сильнее сюжет подходит для публикации; нажмите на строку, чтобы увидеть из чего он сложился. Статус «Ждёт решения» означает, что система ничего не делает сама — нужно нажать «Создать пост» или «Исключить», либо дождаться планового запуска расписания.`}
        actions={(q.data?.length ?? 0) > 0 && <button className="btn btn-danger" disabled={clearAll.isPending} title="Опубликованные сюжеты и сюжеты с черновиками сохранятся" onClick={() => window.confirm("Удалить все неопубликованные сюжеты без связанных черновиков? Опубликованные сюжеты и сюжеты с черновиками сохранятся. Материалы останутся в базе, но не будут повторно собраны в сюжеты.") && clearAll.mutate()}><Trash2 size={15} />{clearAll.isPending ? "Очищаем…" : "Очистить все"}</button>}
      />
      <Tabs className="mb-2" tabs={TABS.map((t) => ({ id: t.id, label: t.label, count: counts[t.id] }))} value={tab} onChange={setTab} />

      {(hiddenSingles > 0 || singles || hiddenOffTopic > 0 || showOffTopic) && (
        <div className="py-2.5 flex flex-wrap gap-x-6 gap-y-2 text-[13px] text-muted">
          {(hiddenSingles > 0 || singles) && (
          <label className="inline-flex items-center gap-2 cursor-pointer select-none">
            <input type="checkbox" className="accent-[var(--brand)]" checked={singles} onChange={(e) => setSingles(e.target.checked)} />
            Показывать одиночные заметки{!singles && <span className="num">({hiddenSingles})</span>}
            <span className="hidden sm:inline">— материалы, о которых пока написал один источник</span>
          </label>
          )}
          {(hiddenOffTopic > 0 || showOffTopic) && (
            <label className="inline-flex items-center gap-2 cursor-pointer select-none">
              <input type="checkbox" className="accent-[var(--brand)]" checked={showOffTopic} onChange={(e) => setShowOffTopic(e.target.checked)} />
              Показывать ниже порога темы{!showOffTopic && <span className="num">({hiddenOffTopic})</span>}
            </label>
          )}
        </div>
      )}
      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data && rows.length === 0 && (
        <Empty
          title={tab === "open" ? "Новых сюжетов нет" : tab === "published" ? "Публикаций пока не было" : "Здесь пусто"}
          text={hiddenOffTopic > 0 && !showOffTopic ? "Есть сюжеты ниже порога близости к теме. Включите «Показывать ниже порога темы», чтобы увидеть их." : tab === "open" ? <>Сюжет появляется, когда несколько материалов описывают одно событие. Подключите источники на странице <Link className="underline" to="/sources">«Источники»</Link> или импортируйте готовый набор.</> : tab === "published" ? "Сюжеты, по которым уже вышел пост, попадают сюда как история публикаций." : undefined}
        />
      )}
      <ul>
        {rows.map((c) => (
          <li key={c.id} className="rule-b">
            <button
              onClick={() => setParams({ open: String(c.id) })}
              className="w-full text-left grid grid-cols-1 sm:grid-cols-[112px_1fr] md:grid-cols-[112px_1fr_160px] gap-x-5 gap-y-2 items-center py-3.5 hover:bg-sunken/60 -mx-2 px-2"
              style={{ borderRadius: 3 }}
            >
              <Score value={c.score} />
              <span className="min-w-0 flex items-start gap-3.5">
                {c.image_item_id && <Thumb src={`/items/${c.image_item_id}/image`} className="w-[88px] h-[58px] shrink-0 hidden sm:block" />}
                <span className="min-w-0 flex-1">
                <span className="block font-serif text-[17px] font-medium leading-snug">{c.title || "Без названия"}</span>
                <span className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[13px] text-muted mt-0.5">
                  <span>{c.item_count} {plural(c.item_count, "материал", "материала", "материалов")}</span>
                  <span>{c.source_count} {plural(c.source_count, "источник", "источника", "источников")}</span>
                  <span>{ago(c.last_seen)}</span>
                  <Badge tone={CLUSTER_STATE[c.state].tone}>{CLUSTER_STATE[c.state].label}</Badge>
                </span>
                </span>
              </span>
              <span className="hidden md:block justify-self-end"><ArrivalStrip arrivals={c.arrivals} windowHours={project.window_hours} /></span>
            </button>
          </li>
        ))}
      </ul>
      <ClusterDrawer id={openId} topicThreshold={project.topic_threshold} onClose={() => setParams({})} />
    </div>
  );
}

function ClusterDrawer({ id, topicThreshold, onClose }: { id: number | null; topicThreshold: number; onClose: () => void }) {
  const { can } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["cluster", id], queryFn: () => api<ClusterDetail>(`/clusters/${id}`), enabled: id != null });
  const act = useMutation({
    mutationFn: (a: "generate" | "exclude" | "reopen") => api(`/clusters/${id}/${a}`, { method: "POST" }),
    onSuccess: (_, a) => {
      toast(a === "generate" ? "Задача поставлена в очередь — черновик появится в разделе «Черновики»" : a === "exclude" ? "Сюжет исключён" : "Сюжет возвращён в работу", a === "generate" ? "info" : "ok");
      qc.invalidateQueries({ queryKey: ["clusters"] });
      qc.invalidateQueries({ queryKey: ["cluster", id] });
      if (a === "generate") {
        qc.invalidateQueries({ queryKey: ["generation-queue"] });
        qc.invalidateQueries({ queryKey: ["dashboard"] });
        onClose();
      }
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const c = q.data;
  const topicFit = c?.score_breakdown?.topic_fit;
  const belowTopicThreshold = topicThreshold > 0 && !!topicFit && !topicFit.neutral && topicFit.value < topicThreshold;
  return (
    <Drawer open={id != null} onClose={onClose} title={c?.title ?? "Сюжет"}>
      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {c && (
        <div className="space-y-7">
          {c.image_item_id && <Thumb src={`/items/${c.image_item_id}/image`} className="w-full max-h-64" />}
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
            <Score value={c.score} width={140} />
            <Badge tone={CLUSTER_STATE[c.state].tone}>{CLUSTER_STATE[c.state].label}</Badge>
            <span className="text-[13px] text-muted">{dateTime(c.first_seen)} — {dateTime(c.last_seen)}</span>
          </div>
          {can("editor") && c.state !== "published" && (
            <div className="flex flex-wrap gap-2">
              {c.state !== "drafted" && c.state !== "excluded" && <button className="btn btn-primary" onClick={() => act.mutate("generate")} disabled={act.isPending || belowTopicThreshold} title={belowTopicThreshold ? `Близость к теме ${topicFit.value.toFixed(2)} ниже порога ${topicThreshold.toFixed(2)}` : undefined}>Создать пост</button>}
              {c.state === "excluded"
                ? <button className="btn" onClick={() => act.mutate("reopen")} disabled={act.isPending}>Вернуть в работу</button>
                : c.state !== "drafted" && <button className="btn" onClick={() => act.mutate("exclude")} disabled={act.isPending}>Исключить</button>}
              {belowTopicThreshold && <span className="hint basis-full">Ниже порога близости к теме: {topicFit.value.toFixed(2)} из {topicThreshold.toFixed(2)}. Сюжет можно посмотреть, но нельзя отправить в генерацию.</span>}
            </div>
          )}
          {c.state === "published" && (
            <p className="hint">Пост по этому сюжету уже опубликован — карточка оставлена как история. Если материалов заметно прибавится, система сама предложит пост-обновление по расписанию или в полностью автоматическом режиме.</p>
          )}
          {c.interest != null && (
            <p className="hint">Интересность по мнению модели: <b>{c.interest.toFixed(1)} из 10</b>{c.interest_reason ? ` — ${c.interest_reason}` : ""}. Оценка приблизительная: при низкой пост не публикуется сам, а ждёт вашего решения.</p>
          )}
          {c.score_breakdown && (
            <section>
              <h3 className="text-base font-semibold mb-3">Из чего сложился вес</h3>
              <Breakdown parts={c.score_breakdown} />
            </section>
          )}
          {c.drafts.length > 0 && (
            <section>
              <h3 className="text-base font-semibold mb-2">Черновики по сюжету</h3>
              <ul>
                {c.drafts.map((d) => (
                  <li key={d.id} className="rule-b py-2 flex items-center justify-between gap-3">
                    <Link className="hover:underline font-serif truncate" to={`/drafts/${d.id}`}>{d.title || "Без заголовка"}</Link>
                    <Badge tone={DRAFT_STATUS[d.status].tone}>{DRAFT_STATUS[d.status].label}</Badge>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <section>
            <h3 className="text-base font-semibold mb-2">Материалы ({c.items.length})</h3>
            <ul>
              {c.items.map((i) => (
                <li key={i.id} className="rule-b py-2.5">
                  <div className="flex items-start justify-between gap-3">
                    {i.has_image && <Thumb src={`/items/${i.id}/image`} className="w-[72px] h-[48px] shrink-0" />}
                    <span className="leading-snug flex-1">{i.title}</span>
                    {i.url && <a href={i.url} target="_blank" rel="noreferrer noopener" className="text-muted hover:text-ink shrink-0 mt-0.5" aria-label="Открыть источник"><ExternalLink size={15} /></a>}
                  </div>
                  <span className="text-[13px] text-muted">{i.source_name ?? "Источник удалён"}, {dateTime(i.published_at)}</span>
                </li>
              ))}
            </ul>
          </section>
        </div>
      )}
    </Drawer>
  );
}
