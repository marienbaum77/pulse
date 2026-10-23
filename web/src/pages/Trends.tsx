import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, RotateCcw, Search, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { ago, dateTime, plural } from "../lib/format";
import { CLUSTER_STATE, CONTENT_KINDS, DRAFT_STATUS } from "../lib/labels";
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
  const [search, setSearch] = useState("");
  const [sortBy, setSortBy] = useState<"score" | "interest" | "date" | "items" | "sources" | "topic_fit">("score");
  const [minScore, setMinScore] = useState<string>("all");
  const [interestFilter, setInterestFilter] = useState<string>("all");
  const [onlyWithImages, setOnlyWithImages] = useState(false);

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

  // Базовые предикаты видимости
  // Подборочному материалу (видео, статья) второй источник не нужен — он не «одиночка», а полноценный кандидат.
  const isSingle = (c: Cluster) => c.mode !== "curated" && c.item_count < project.min_items;
  const visible = (c: Cluster) => singles || !isSingle(c);
  const belowTopicThreshold = (c: Cluster) => {
    const fit = c.score_breakdown?.topic_fit;
    return project.topic_threshold > 0 && c.state !== "published" && !!fit && !fit.neutral && fit.value < project.topic_threshold;
  };
  const topicVisible = (c: Cluster) => showOffTopic || !belowTopicThreshold(c);

  // Фильтры пользователя
  const searchFilter = (c: Cluster) => {
    if (!search.trim()) return true;
    return (c.title || "").toLowerCase().includes(search.trim().toLowerCase());
  };

  const scoreFilter = (c: Cluster) => {
    if (minScore === "all") return true;
    const target = Number(minScore) / 100;
    return c.score != null && c.score >= target;
  };

  const interestCheck = (c: Cluster) => {
    if (interestFilter === "all") return true;
    if (interestFilter === "rated") return c.interest != null;
    if (interestFilter === "high") return c.interest != null && c.interest >= 6;
    if (interestFilter === "low") return c.interest != null && c.interest < 3;
    return true;
  };

  const imageFilter = (c: Cluster) => {
    if (!onlyWithImages) return true;
    return c.image_item_id != null;
  };

  // Сортировка
  const sortFn = (a: Cluster, b: Cluster) => {
    if (sortBy === "interest") {
      const aVal = a.interest ?? -1;
      const bVal = b.interest ?? -1;
      if (bVal !== aVal) return bVal - aVal;
      return (b.score ?? 0) - (a.score ?? 0);
    }
    if (sortBy === "date") {
      return new Date(b.last_seen).getTime() - new Date(a.last_seen).getTime();
    }
    if (sortBy === "items") {
      return b.item_count - a.item_count;
    }
    if (sortBy === "sources") {
      return b.source_count - a.source_count;
    }
    if (sortBy === "topic_fit") {
      const aFit = a.score_breakdown?.topic_fit?.value ?? 0;
      const bFit = b.score_breakdown?.topic_fit?.value ?? 0;
      return bFit - aFit;
    }
    // По умолчанию: по весу NWS
    return (b.score ?? 0) - (a.score ?? 0);
  };

  const isFiltered = search.trim() !== "" || minScore !== "all" || interestFilter !== "all" || onlyWithImages || sortBy !== "score";
  const resetFilters = () => {
    setSearch("");
    setMinScore("all");
    setInterestFilter("all");
    setOnlyWithImages(false);
    setSortBy("score");
  };

  const allClusters = q.data ?? [];
  const baseTabRows = allClusters.filter(tabDef.match).filter(visible).filter(topicVisible);
  const rows = [...baseTabRows.filter(searchFilter).filter(scoreFilter).filter(interestCheck).filter(imageFilter)].sort(sortFn);

  const counts = Object.fromEntries(
    TABS.map((t) => [
      t.id,
      allClusters
        .filter(t.match)
        .filter(visible)
        .filter(topicVisible)
        .filter(searchFilter)
        .filter(scoreFilter)
        .filter(interestCheck)
        .filter(imageFilter).length,
    ])
  );
  const hiddenSingles = allClusters.filter((c) => TABS[0].match(c) && isSingle(c)).length;
  const hiddenOffTopic = allClusters.filter(tabDef.match).filter(visible).filter(belowTopicThreshold).length;

  return (
    <div className="fadein">
      <PageHeader
        title="Сюжеты"
        sub={`Материалы, объединённые по смыслу за последние ${project.window_hours} ч. Чем выше вес, тем сильнее сюжет подходит для публикации; нажмите на строку, чтобы увидеть из чего он сложился. Статус «Ждёт решения» означает, что система ничего не делает сама — нужно нажать «Создать пост» или «Исключить», либо дождаться планового запуска расписания.`}
        actions={allClusters.length > 0 && <button className="btn btn-danger" disabled={clearAll.isPending} title="Опубликованные сюжеты и сюжеты с черновиками сохранятся" onClick={() => window.confirm("Удалить все неопубликованные сюжеты без связанных черновиков? Опубликованные сюжеты и сюжеты с черновиками сохранятся. Материалы останутся в базе, но не будут повторно собраны в сюжеты.") && clearAll.mutate()}><Trash2 size={15} />{clearAll.isPending ? "Очищаем…" : "Очистить все"}</button>}
      />
      <Tabs className="mb-2" tabs={TABS.map((t) => ({ id: t.id, label: t.label, count: counts[t.id] }))} value={tab} onChange={setTab} />

      {/* Панель фильтров и сортировки */}
      <div className="bg-sunken/40 p-3 mb-4 rounded border border-rule/70 flex flex-col gap-2.5">
        <div className="flex flex-wrap items-center gap-3">
          {/* Поиск */}
          <div className="relative flex-1 min-w-[200px]">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
            <input
              type="text"
              className="field pl-8 h-8 text-[13px] w-full"
              placeholder="Поиск по названию сюжета..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>

          {/* Сортировка */}
          <div className="flex items-center gap-1.5 text-[13px] text-muted">
            <span className="shrink-0">Сортировка:</span>
            <select
              className="field h-8 px-2 py-0 text-[13px] w-auto bg-surface"
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value as any)}
            >
              <option value="score">Вес (NWS)</option>
              <option value="interest">Интересность (LLM)</option>
              <option value="date">Свежие сначала</option>
              <option value="items">Больше материалов</option>
              <option value="sources">Больше источников</option>
              <option value="topic_fit">Близость к теме</option>
            </select>
          </div>

          {/* Фильтр по NWS */}
          <div className="flex items-center gap-1.5 text-[13px] text-muted">
            <span className="shrink-0">Мин. вес:</span>
            <select
              className="field h-8 px-2 py-0 text-[13px] w-auto bg-surface"
              value={minScore}
              onChange={(e) => setMinScore(e.target.value)}
            >
              <option value="all">Все</option>
              <option value="30">≥ 30</option>
              <option value="50">≥ 50</option>
              <option value="70">≥ 70</option>
            </select>
          </div>

          {/* Фильтр по интересности */}
          <div className="flex items-center gap-1.5 text-[13px] text-muted">
            <span className="shrink-0">Интерес:</span>
            <select
              className="field h-8 px-2 py-0 text-[13px] w-auto bg-surface"
              value={interestFilter}
              onChange={(e) => setInterestFilter(e.target.value)}
            >
              <option value="all">Все</option>
              <option value="high">Высокий (≥ 6)</option>
              <option value="rated">С оценкой</option>
              <option value="low">Низкий (&lt; 3)</option>
            </select>
          </div>

          {isFiltered && (
            <button
              onClick={resetFilters}
              className="btn btn-ghost btn-sm text-[12px] h-8 px-2 text-muted hover:text-ink inline-flex items-center gap-1"
              title="Сбросить все фильтры"
            >
              <RotateCcw size={12} />
              Сбросить
            </button>
          )}
        </div>

        {/* Дополнительные тумблеры */}
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 text-[12.5px] text-muted pt-1 border-t border-rule/40">
          <label className="inline-flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              className="accent-[var(--brand)]"
              checked={onlyWithImages}
              onChange={(e) => setOnlyWithImages(e.target.checked)}
            />
            Только с картинкой
          </label>

          {(hiddenSingles > 0 || singles) && (
            <label className="inline-flex items-center gap-1.5 cursor-pointer select-none">
              <input
                type="checkbox"
                className="accent-[var(--brand)]"
                checked={singles}
                onChange={(e) => setSingles(e.target.checked)}
              />
              Показывать одиночные заметки{!singles && <span className="num">({hiddenSingles})</span>}
            </label>
          )}

          {(hiddenOffTopic > 0 || showOffTopic) && (
            <label className="inline-flex items-center gap-1.5 cursor-pointer select-none">
              <input
                type="checkbox"
                className="accent-[var(--brand)]"
                checked={showOffTopic}
                onChange={(e) => setShowOffTopic(e.target.checked)}
              />
              Показывать ниже порога темы{!showOffTopic && <span className="num">({hiddenOffTopic})</span>}
            </label>
          )}
        </div>
      </div>

      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data && rows.length === 0 && (
        <Empty
          title={isFiltered ? "Ничего не найдено" : tab === "open" ? "Новых сюжетов нет" : tab === "published" ? "Публикаций пока не было" : "Здесь пусто"}
          text={isFiltered ? "Попробуйте изменить параметры фильтрации или поиска." : hiddenOffTopic > 0 && !showOffTopic ? "Есть сюжеты ниже порога близости к теме. Включите «Показывать ниже порога темы», чтобы увидеть их." : tab === "open" ? <>Сюжет появляется, когда несколько материалов описывают одно событие. Подключите источники на странице <Link className="underline" to="/sources">«Источники»</Link> или импортируйте готовый набор.</> : tab === "published" ? "Сюжеты, по которым уже вышел пост, попадают сюда как история публикаций." : undefined}
          action={isFiltered ? <button className="btn btn-sm" onClick={resetFilters}><RotateCcw size={13} />Сбросить фильтры</button> : undefined}
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
              <div className="flex flex-col gap-1 items-start">
                <Score value={c.score} />
                {c.interest != null && (
                  <span
                    className={`text-[11px] font-medium px-1.5 py-0.5 rounded leading-none ${
                      c.interest >= 6
                        ? "bg-ok/15 text-ok"
                        : c.interest >= 3
                        ? "bg-warn/15 text-warn"
                        : "bg-bad/15 text-bad"
                    }`}
                    title={c.interest_reason ? `Оценка редактора (LLM): ${c.interest}/10\n${c.interest_reason}` : `Интересность: ${c.interest}/10`}
                  >
                    ★ {c.interest.toFixed(1)}
                  </span>
                )}
              </div>
              <span className="min-w-0 flex items-start gap-3.5">
                {c.image_item_id && <Thumb src={`/items/${c.image_item_id}/image`} className="w-[88px] h-[58px] shrink-0 hidden sm:block" />}
                <span className="min-w-0 flex-1">
                  <span className="block font-serif text-[17px] font-medium leading-snug">{c.title || "Без названия"}</span>
                  <span className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[13px] text-muted mt-0.5">
                    {c.mode === "curated" && <span className="eyebrow">{CONTENT_KINDS[c.kind ?? "article"]}</span>}
                    <span>{c.item_count} {plural(c.item_count, "материал", "материала", "материалов")}</span>
                    <span>{c.source_count} {plural(c.source_count, "источник", "источника", "источников")}</span>
                    <span>{ago(c.last_seen)}</span>
                    <Badge tone={CLUSTER_STATE[c.state].tone}>{CLUSTER_STATE[c.state].label}</Badge>
                  </span>
                </span>
              </span>
              <div className="hidden md:flex flex-col items-end gap-1 justify-self-end">
                <ArrivalStrip arrivals={c.arrivals} windowHours={project.window_hours} />
                <span className="text-[10px] text-muted/70 tracking-tight" title={`Хронология публикаций за ${project.window_hours} ч: левее — раньше, правее — свежие`}>
                  хронология ({c.arrivals?.length ?? 0})
                </span>
              </div>
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
              {c.state !== "drafted" && c.state !== "excluded" && <button className="btn btn-primary" onClick={() => act.mutate("generate")} disabled={act.isPending || belowTopicThreshold} title={belowTopicThreshold ? `Близость заголовка к теме ${topicFit.value.toFixed(2)} ниже порога ${topicThreshold.toFixed(2)}` : undefined}>Создать пост</button>}
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
