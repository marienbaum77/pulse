import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, ListPlus } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { ago, dateTime, fmtNum, plural } from "../lib/format";
import { PUB_STATUS } from "../lib/labels";
import type { Dashboard } from "../lib/types";
import { DailyBars, Score } from "../components/charts";
import { NoProject } from "../components/NoProject";
import { Badge, ErrorNote, PageHeader, Spinner } from "../components/ui";

const N = ({ children }: { children: React.ReactNode }) => <b className="num font-semibold">{children}</b>;

export default function Overview() {
  const { project, loading } = useProject();
  const { can } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const [days, setDays] = useState(7);
  const q = useQuery({
    queryKey: ["dashboard", project?.id, days],
    queryFn: () => api<Dashboard>("/dashboard", { params: { project_id: project!.id, days } }),
    enabled: !!project,
    refetchInterval: 60_000,
  });
  const gen = useMutation({
    mutationFn: (v: { kind: "post" | "digest"; top_n: number }) => api("/drafts/generate", { method: "POST", body: { project_id: project!.id, ...v } }),
    onSuccess: () => {
      toast("Задача поставлена в очередь постов", "info");
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["generation-queue"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  if (loading) return <Spinner />;
  if (!project) return <NoProject />;
  const d = q.data;

  return (
    <div className="fadein">
      <PageHeader
        title={project.name}
        sub={project.topic || "Тема проекта не задана — добавьте её в настройках, чтобы сюжеты оценивались по близости к теме."}
        actions={can("editor") && (
          <>
            <button className="btn" onClick={() => gen.mutate({ kind: "post", top_n: 1 })} disabled={gen.isPending}><FileText size={16} />Пост по главному сюжету</button>
            <button className="btn btn-primary" onClick={() => gen.mutate({ kind: "digest", top_n: 5 })} disabled={gen.isPending}><ListPlus size={16} />Собрать дайджест</button>
          </>
        )}
      />

      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {d && d.pipeline.rss_enabled === 0 && (
        <div className="mb-7 px-4 py-3.5 text-[14px] leading-relaxed max-w-[76ch]" style={{ border: "1px solid var(--info)", borderRadius: 3 }}>
          <b>Новые материалы не поступают: не подключён ни один RSS-источник.</b> Pulse сам ничего не ищет в интернете — он читает ленты, которые вы добавили.
          Добавьте ленту или импортируйте OPML на странице <Link to="/sources" className="underline underline-offset-4">«Источники»</Link>.
        </div>
      )}
      {d && (
        <>
          <section className="flex flex-wrap items-start justify-between gap-4 mb-9">
            <p className="font-serif text-[21px] leading-[1.45] max-w-[62ch]">
              За {days} {plural(days, "день", "дня", "дней")} вышло <N>{fmtNum(d.metrics.items)}</N> {plural(d.metrics.items, "материал", "материала", "материалов")}
              {d.metrics.items_prev > 0 && (() => {
                const diff = Math.round(((d.metrics.items - d.metrics.items_prev) / d.metrics.items_prev) * 100);
                return diff === 0 ? ", столько же, сколько в прошлый период" : <>, {Math.abs(diff)}% {diff > 0 ? "больше" : "меньше"}, чем в прошлый период</>;
              })()}
              . Сейчас в работе <N>{d.metrics.open_clusters}</N> {plural(d.metrics.open_clusters, "сюжет", "сюжета", "сюжетов")}
              {d.metrics.pending_drafts > 0 && <>, на проверке <Link to="/drafts" className="underline underline-offset-4 decoration-mark decoration-2"><N>{d.metrics.pending_drafts}</N> {plural(d.metrics.pending_drafts, "черновик", "черновика", "черновиков")}</Link></>}
              . Отправлено <N>{d.metrics.published}</N> {plural(d.metrics.published, "пост", "поста", "постов")}.
            </p>
            <div className="inline-flex" role="group" aria-label="Период" style={{ border: "1px solid var(--rule)", borderRadius: 3 }}>
              {[7, 14, 30].map((v) => (
                <button key={v} onClick={() => setDays(v)} aria-pressed={days === v} className="px-3 h-8 text-[13px] num" style={{ background: days === v ? "var(--brand)" : "transparent", color: days === v ? "var(--brand-ink)" : "var(--muted)" }}>{v} дн.</button>
              ))}
            </div>
          </section>

          <div className="grid xl:grid-cols-[minmax(0,1.65fr)_minmax(0,1fr)] gap-x-12 gap-y-10">
            <div className="space-y-10 min-w-0">
              <section>
                <h2 className="text-lg font-semibold mb-3">Поток материалов</h2>
                <DailyBars data={d.daily} />
              </section>
              <section>
                <div className="flex items-baseline justify-between mb-1">
                  <h2 className="text-lg font-semibold">Главные сюжеты</h2>
                  <Link to="/trends" className="text-sm text-muted hover:text-ink">Все сюжеты →</Link>
                </div>
                {d.top_clusters.length === 0 ? (
                  <p className="text-muted py-6">Сюжетов пока нет. Добавьте источники или импортируйте материалы на странице «Источники».</p>
                ) : (
                  <ul>
                    {d.top_clusters.map((c) => (
                      <li key={c.id} className="rule-b">
                        <Link to={`/trends?open=${c.id}`} className="grid grid-cols-1 sm:grid-cols-[112px_1fr] gap-x-4 gap-y-2 items-center py-3 hover:bg-sunken/60 -mx-2 px-2" style={{ borderRadius: 3 }}>
                          <Score value={c.score} />
                          <span className="min-w-0">
                            <span className="block font-serif text-[16.5px] font-medium leading-snug truncate">{c.title}</span>
                            <span className="block text-[13px] text-muted">{c.item_count} {plural(c.item_count, "материал", "материала", "материалов")} из {c.source_count} {plural(c.source_count, "источника", "источников", "источников")}, {ago(c.last_seen)}</span>
                          </span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </div>

            <div className="space-y-10 min-w-0">
              <section>
                <h2 className="text-lg font-semibold mb-2">Очередь постов</h2>
                {d.generation_queue.length === 0 && d.generating.length === 0 && d.review.length === 0 && d.queue.length === 0 && <p className="text-muted py-2">Очередь пуста. Плановые запуски и новые посты появятся здесь.</p>}
                <ul>
                  {d.generation_queue.map((j) => (
                    <li key={`j${j.id}`} className="rule-b">
                      <Link to="/drafts" className="block py-2.5 hover:bg-sunken/60 -mx-2 px-2" style={{ borderRadius: 3 }}>
                        <span className="block font-serif font-medium leading-snug">{j.title}</span>
                        <span className="text-[13px] text-muted">{j.kind === "digest" ? "дайджест" : "пост"} · ожидает генерации · {ago(j.created_at)}</span>
                      </Link>
                    </li>
                  ))}
                  {d.generating.map((r) => (
                    <li key={`g${r.id}`} className="rule-b">
                      <Link to={`/drafts/${r.id}`} className="block py-2.5 hover:bg-sunken/60 -mx-2 px-2" style={{ borderRadius: 3 }}>
                        <span className="block font-serif font-medium leading-snug">{r.title}</span>
                        <span className="text-[13px] text-muted">{r.kind === "digest" ? "дайджест" : "пост"} · формируется · {ago(r.created_at)}</span>
                      </Link>
                    </li>
                  ))}
                  {d.review.map((r) => (
                    <li key={`d${r.id}`} className="rule-b">
                      <Link to={`/drafts/${r.id}`} className="block py-2.5 hover:bg-sunken/60 -mx-2 px-2" style={{ borderRadius: 3 }}>
                        <span className="block font-serif font-medium leading-snug">{r.title || "Без заголовка"}</span>
                        <span className="text-[13px] text-muted">на проверке, {ago(r.created_at)}</span>
                      </Link>
                    </li>
                  ))}
                  {d.queue.map((p) => (
                    <li key={`p${p.id}`} className="rule-b">
                      <Link to="/publications" className="block py-2.5 hover:bg-sunken/60 -mx-2 px-2" style={{ borderRadius: 3 }}>
                        <span className="block font-serif font-medium leading-snug">{p.title || "Без заголовка"}</span>
                        <Badge tone={PUB_STATUS[p.status].tone}>{PUB_STATUS[p.status].label}</Badge>
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>

              {d.upcoming.length > 0 && (
                <section>
                  <h2 className="text-lg font-semibold mb-2">Ближайшие запуски</h2>
                  <ul>
                    {d.upcoming.map((u) => (
                      <li key={u.id} className="rule-b py-2.5 flex items-baseline justify-between gap-3">
                        <span>{u.name} <span className="text-muted text-[13px]">({u.kind === "digest" ? "дайджест" : "посты"})</span></span>
                        <span className="num text-[13px] text-muted whitespace-nowrap">{dateTime(u.at)}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              <section>
                <h2 className="text-lg font-semibold mb-2">Состояние конвейера</h2>
                <dl className="text-[14px]">
                  <Row k="Воркер">
                    {d.pipeline.worker_seen_seconds == null
                      ? <Badge tone="bad">не запускался</Badge>
                      : d.pipeline.worker_seen_seconds < 90 ? <Badge tone="ok">работает</Badge> : <Badge tone="bad">не отвечает {Math.round(d.pipeline.worker_seen_seconds / 60)} мин</Badge>}
                  </Row>
                  <Row k="Источники">
                    {d.pipeline.failing_sources > 0
                      ? <Badge tone="warn">{d.pipeline.failing_sources} с ошибкой</Badge>
                      : <span className="text-muted">{d.pipeline.last_ingest_ok ? `последний сбор ${ago(d.pipeline.last_ingest_ok)}` : "RSS ещё не опрашивался"}</span>}
                  </Row>
                  <Row k="Очередь задач"><span className="num">{d.pipeline.jobs_queued}</span>{d.pipeline.jobs_failed_24h > 0 && <span className="text-bad ml-2">сбоев за сутки: {d.pipeline.jobs_failed_24h}</span>}</Row>
                  <Row k="Модель">
                    {d.llm.provider === "stub"
                      ? <span className="text-muted">режим без моделей (stub)</span>
                      : <span className="text-right"><span className="block">{d.llm.chat_model}</span><span className="block text-[12.5px] text-muted num">{d.llm.calls} вызовов, {d.llm.avg_chat_ms ? `${(d.llm.avg_chat_ms / 1000).toFixed(1)} с на пост` : "постов ещё не было"}{d.llm.errors > 0 && <span className="text-bad">, ошибок: {d.llm.errors}</span>}</span></span>}
                  </Row>
                </dl>
              </section>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="rule-b py-2.5 flex items-baseline justify-between gap-4">
      <dt className="text-muted">{k}</dt>
      <dd className="text-right">{children}</dd>
    </div>
  );
}
