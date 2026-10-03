import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Check, ChevronLeft, ChevronRight, ExternalLink, ImageOff, Info, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { ago, stripCitations } from "../lib/format";
import { DRAFT_STATUS, PUB_STATUS } from "../lib/labels";
import type { Draft, DraftChecks, DraftListItem, GenerationJob, Project } from "../lib/types";
import { NoProject } from "../components/NoProject";
import { Thumb } from "../components/Thumb";
import { Badge, Empty, ErrorNote, PageHeader, Spinner, Tabs } from "../components/ui";

const TABS = [
  { id: "generating", label: "Очередь", match: (d: DraftListItem) => d.status === "generating" },
  { id: "pending_review", label: "На проверке", match: (d: DraftListItem) => d.status === "pending_review" },
  { id: "approved", label: "Утверждены", match: (d: DraftListItem) => d.status === "approved" },
  { id: "other", label: "Отклонены и сбои", match: (d: DraftListItem) => d.status === "rejected" || d.status === "failed" },
] as const;

export default function Drafts() {
  const { id } = useParams();
  const nav = useNavigate();
  const { project, loading } = useProject();
  const toast = useToast();
  const qc = useQueryClient();
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("generating");
  const list = useQuery({
    queryKey: ["drafts", project?.id, "all"],
    queryFn: () => api<DraftListItem[]>("/drafts", { params: { project_id: project!.id, limit: 100 } }),
    enabled: !!project,
    refetchInterval: 5000,
  });
  const generationQueue = useQuery({
    queryKey: ["generation-queue", project?.id],
    queryFn: () => api<GenerationJob[]>("/generation-queue", { params: { project_id: project!.id } }),
    enabled: !!project,
    refetchInterval: 5000,
  });
  const clearAll = useMutation({
    mutationFn: () => api<{ deleted: number }>("/drafts", { method: "DELETE", params: { project_id: project!.id } }),
    onSuccess: ({ deleted }) => {
      toast(`Удалено черновиков: ${deleted}`);
      qc.invalidateQueries({ queryKey: ["drafts"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["clusters"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  if (loading) return <Spinner />;
  if (!project) return <NoProject />;

  const all = list.data ?? [];
  const clearable = all.some((d) => ["pending_review", "rejected", "failed"].includes(d.status));
  const pendingIds = all.filter((d) => d.status === "pending_review").map((d) => d.id);

  if (id) {
    const cur = Number(id);
    const pos = pendingIds.indexOf(cur);
    const go = (to: number | undefined) => nav(to ? `/drafts/${to}` : "/drafts");
    return (
      <div className="fadein">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
          <Link to="/drafts" className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink"><ArrowLeft size={15} />Все черновики</Link>
          {pos >= 0 && (
            <div className="flex items-center gap-1.5">
              <span className="text-[13px] text-muted num mr-1.5">{pos + 1} из {pendingIds.length} на проверке</span>
              <button className="btn btn-sm" disabled={pos === 0} onClick={() => go(pendingIds[pos - 1])} aria-label="Предыдущий черновик"><ChevronLeft size={15} /></button>
              <button className="btn btn-sm" disabled={pos === pendingIds.length - 1} onClick={() => go(pendingIds[pos + 1])} aria-label="Следующий черновик"><ChevronRight size={15} /></button>
            </div>
          )}
        </div>
        {/* После решения открываем следующий черновик на проверке; если очередь пуста, остаёмся на решённом и показываем статус отправки. */}
        <DraftEditor key={cur} id={cur} project={project} onDecided={() => go(pos >= 0 ? (pendingIds[pos + 1] ?? pendingIds[pos - 1] ?? cur) : cur)} />
      </div>
    );
  }

  const def = TABS.find((t) => t.id === tab)!;
  const rows = all.filter(def.match);
  const queued = generationQueue.data ?? [];
  const counts = Object.fromEntries(TABS.map((t) => [t.id, all.filter(t.match).length + (t.id === "generating" ? queued.length : 0)]));

  return (
    <div className="fadein">
      <PageHeader
        title="Посты"
        sub="Здесь виден весь путь поста: очередь генерации, проверка, публикация и ошибки. В автоматическом режиме система публикует прошедшие проверки посты сама."
        actions={clearable && <button className="btn btn-danger" disabled={clearAll.isPending} title="Утверждённые публикации и выполняющиеся генерации сохранятся" onClick={() => window.confirm("Удалить все черновики на проверке, отклонённые и сбои? Утверждённая история и выполняющиеся генерации останутся.") && clearAll.mutate()}><Trash2 size={15} />{clearAll.isPending ? "Очищаем…" : "Очистить все"}</button>}
      />
      <div className="max-w-[860px]">
        <Tabs className="mb-1" tabs={TABS.map((t) => ({ id: t.id, label: t.label, count: counts[t.id] }))} value={tab} onChange={setTab} />
        {list.isLoading && <Spinner />}
        {list.error && <ErrorNote error={list.error} />}
        {tab === "generating" && generationQueue.error && <ErrorNote error={generationQueue.error} />}
        {list.data && rows.length === 0 && (tab !== "generating" || queued.length === 0) && (
          <Empty title={tab === "pending_review" ? "Постов на проверке нет" : tab === "generating" ? "Очередь пуста" : "Здесь пусто"} text={tab === "pending_review" ? "Новые посты появятся после генерации. В автоматическом режиме прошедшие проверки посты публикуются без ожидания редактора." : tab === "generating" ? "Посты появятся здесь сразу после постановки в очередь или старта генерации. Черновики, уже ожидающие решения редактора, находятся во вкладке «На проверке»." : undefined} />
        )}
        <ul>
          {tab === "generating" && queued.map((job) => (
            <li key={`job-${job.id}`} className="rule-b">
              <div className="block py-3.5 -mx-2 px-2">
                <span className="block font-serif text-[17px] font-medium leading-snug">{job.title}</span>
                <span className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[13px] text-muted mt-0.5">
                  <span>{job.kind === "digest" ? "Дайджест" : "Пост"}</span>
                  <span>{ago(job.created_at)}</span>
                  <Badge tone="info">Ожидает генерации</Badge>
                </span>
              </div>
            </li>
          ))}
          {rows.map((d) => {
            const warn = (d.checks.unsupported_numbers?.length ?? 0) + (d.checks.invalid_citations?.length ?? 0) > 0;
            return (
              <li key={d.id} className="rule-b">
                <Link to={`/drafts/${d.id}`} className="flex items-start gap-3.5 py-3.5 hover:bg-sunken/60 -mx-2 px-2" style={{ borderRadius: 3 }}>
                  {d.has_image && <Thumb src={`/drafts/${d.id}/image?v=${encodeURIComponent(d.updated_at)}`} className="w-[88px] h-[58px] shrink-0 hidden sm:block" />}
                  <span className="min-w-0 flex-1">
                  <span className="block break-words font-serif text-[17px] font-medium leading-snug">{d.title || (d.status === "failed" ? "Не удалось написать" : "Подготовка поста")}</span>
                  <span className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[13px] text-muted mt-0.5">
                    <span>{d.kind === "digest" ? "Дайджест" : "Пост"}</span>
                    <span>{ago(d.created_at)}</span>
                    <Badge tone={DRAFT_STATUS[d.status].tone}>{DRAFT_STATUS[d.status].label}</Badge>
                    {warn && <span className="text-warn inline-flex items-center gap-1"><AlertTriangle size={12} />есть замечания</span>}
                  </span>
                  {d.status === "failed" && d.error && <span className="mt-1 block line-clamp-2 text-[12.5px] text-bad">{d.error}</span>}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

function DraftEditor({ id, project, onDecided }: { id: number; project: Project; onDecided: () => void }) {
  const { can } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["draft", id], queryFn: () => api<Draft>(`/drafts/${id}`) });
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const d = q.data;

  useEffect(() => {
    if (d) { setTitle(d.title); setBody(d.body); }
    // сбрасываем локальные правки только когда сервер отдал новую версию
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d?.id, d?.updated_at]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["draft", id] });
    qc.invalidateQueries({ queryKey: ["drafts"] });
    qc.invalidateQueries({ queryKey: ["clusters"] });
  };
  const fail = (e: Error) => toast(e.message, "bad");
  const dirty = !!d && (title !== d.title || body !== d.body);
  const editable = !!d && can("editor") && ["pending_review", "rejected", "failed"].includes(d.status);

  const save = useMutation({
    mutationFn: () => api(`/drafts/${id}`, { method: "PATCH", body: { title, body } }),
    onSuccess: () => { toast("Правки сохранены"); refresh(); },
    onError: fail,
  });
  const approve = useMutation({
    mutationFn: async () => {
      if (dirty) await api(`/drafts/${id}`, { method: "PATCH", body: { title, body } });
      return api<{ already: boolean; suppressed: number }>(`/drafts/${id}/approve`, { method: "POST" });
    },
    onSuccess: ({ suppressed }) => { toast(suppressed ? `Утверждено; точных дублей не отправлено: ${suppressed}` : "Утверждено. Публикация поставлена в очередь"); refresh(); qc.invalidateQueries({ queryKey: ["publications"] }); onDecided(); },
    onError: fail,
  });
  const reject = useMutation({
    mutationFn: () => api(`/drafts/${id}/reject`, { method: "POST" }),
    onSuccess: () => { toast("Черновик отклонён, сюжет исключён из автоматического отбора"); refresh(); onDecided(); },
    onError: fail,
  });
  const regen = useMutation({
    mutationFn: () => api(`/drafts/${id}/regenerate`, { method: "POST" }),
    onSuccess: () => { toast("Пишем заново — новый черновик появится в списке", "info"); refresh(); nav("/drafts"); },
    onError: fail,
  });
  const image = useMutation({
    mutationFn: (patch: { remove_image?: boolean; image_item_id?: number }) => api(`/drafts/${id}`, { method: "PATCH", body: patch }),
    onSuccess: () => { toast("Картинка обновлена"); refresh(); },
    onError: fail,
  });
  const busy = image.isPending || save.isPending || approve.isPending || reject.isPending || regen.isPending;

  if (q.isLoading) return <Spinner />;
  if (q.error) return <ErrorNote error={q.error} />;
  if (!d) return null;

  return (
    <article className="fadein">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mb-3">
        <Badge tone={DRAFT_STATUS[d.status].tone}>{DRAFT_STATUS[d.status].label}</Badge>
        <span className="text-[13px] text-muted">{d.kind === "digest" ? "Дайджест" : "Пост"}, {d.model === "extractive" ? "без языковой модели" : d.model}, {ago(d.created_at)}</span>
      </div>
      {d.error && <p className="text-bad text-sm mb-3" role="alert">{d.error}</p>}
      {d.status === "generating" && <p className="mb-4 text-sm text-muted" role="status">Пост по этому сюжету формируется. Как только генерация закончится, здесь появятся текст и результаты проверок.</p>}

      <div className="grid xl:grid-cols-[minmax(0,1fr)_320px] gap-x-12 gap-y-10">
        <div className="min-w-0">
          <TitleField value={title} onChange={setTitle} readOnly={!editable} />
          {d.image_url && (
            <figure className="mb-3">
              <Thumb key={`${d.id}-${d.updated_at}`} src={`/drafts/${d.id}/image?v=${encodeURIComponent(d.updated_at)}`} className="w-full max-h-80" />
              {editable && (
                <figcaption className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5">
                  <button className="btn btn-sm" onClick={() => image.mutate({ remove_image: true })} disabled={busy}><ImageOff size={14} />Убрать картинку</button>
                  <span className="hint">Пост выйдет без иллюстрации — так лучше, если картинка не по теме или низкого качества.</span>
                </figcaption>
              )}
            </figure>
          )}
          <AutoTextarea value={body} onChange={setBody} readOnly={!editable} />
          <div className="flex flex-wrap items-center justify-between gap-x-4 mt-1.5 text-[12.5px] text-muted">
            <span className="num">{body.length} симв.{body.length > 3800 && <span className="text-warn ml-2">Telegram принимает до 4096 символов</span>}</span>
            <span>Номера [1] — только для сверки с источниками справа; при публикации они убираются</span>
          </div>
          <details className="mt-4">
            <summary className="cursor-pointer text-sm font-medium">Как увидят подписчики</summary>
            <div className="mt-2 px-4 py-3.5 prose-draft whitespace-pre-wrap break-words" style={{ background: "var(--sunken)", borderRadius: 3, fontSize: 16 }}>
              {d.image_url && <Thumb key={`pv-${d.id}-${d.updated_at}`} src={`/drafts/${d.id}/image?v=${encodeURIComponent(d.updated_at)}`} className="w-full max-h-72 mb-3" />}
              {title && <p className="font-semibold mb-3">{title}</p>}
              {stripCitations(body, new Set(d.citations.map((c) => c.n)))}
            </div>
          </details>

          {can("editor") && (
            <div className="flex flex-wrap items-center gap-2 mt-6">
              {d.status === "pending_review" && <button className="btn btn-primary h-10" onClick={() => approve.mutate()} disabled={busy}><Check size={16} />Утвердить и отправить</button>}
              {editable && <button className="btn" onClick={() => save.mutate()} disabled={busy || !dirty}>Сохранить правки</button>}
              {(d.status === "pending_review" || d.status === "failed") && <button className="btn" onClick={() => regen.mutate()} disabled={busy}>Написать заново</button>}
              {(d.status === "pending_review" || d.status === "failed") && <button className="btn btn-danger" onClick={() => reject.mutate()} disabled={busy}>Отклонить</button>}
            </div>
          )}
          {d.status === "approved" && d.publications.length > 0 && (
            <section className="mt-8">
              <h3 className="text-base font-semibold mb-2">Отправка</h3>
              <ul>
                {d.publications.map((p) => (
                  <li key={p.id} className="rule-b py-2 flex items-center justify-between gap-3 text-sm">
                    <span>{p.channel_name}</span>
                    <span className="flex items-center gap-3">
                      {p.error && <span className="text-[12.5px] text-muted truncate max-w-[220px]" title={p.error}>{p.error}</span>}
                      <Badge tone={PUB_STATUS[p.status].tone}>{PUB_STATUS[p.status].label}</Badge>
                    </span>
                  </li>
                ))}
              </ul>
              <Link to="/publications" className="text-sm text-muted hover:text-ink inline-block mt-2">Все публикации →</Link>
            </section>
          )}
        </div>

        <aside className="space-y-8 min-w-0 xl:sticky xl:top-6 xl:self-start xl:max-h-[calc(100vh-3rem)] xl:overflow-y-auto xl:pr-1">
          <section>
            <h3 className="text-base font-semibold mb-2.5">Проверки</h3>
            <Checks checks={d.checks} maxLength={project.max_length} kind={d.kind} />
          </section>
          <section>
            <h3 className="text-base font-semibold mb-1">Источники</h3>
            {d.citations.length === 0 && <p className="text-sm text-muted">Нет данных об источниках.</p>}
            <ol>
              {d.citations.map((c) => (
                <li key={c.n} className="rule-b py-2.5 text-[13.5px]">
                  <div className="flex items-start gap-2.5">
                    <span className="num font-semibold shrink-0 w-6" style={{ background: "var(--sunken)", textAlign: "center", borderRadius: 2 }}>{c.n}</span>
                    <div className="min-w-0 flex-1">
                      <p className="font-medium leading-snug">{c.source}</p>
                      <p className="text-muted leading-snug mt-0.5">{c.title}</p>
                      {c.image_url && (
                        <div className="mt-1.5 flex items-end gap-2.5">
                          <Thumb src={`/items/${c.item_id}/image`} className="w-[96px] h-[64px]" />
                          {editable && c.image_url !== d.image_url && (
                            <button className="text-[12.5px] text-muted hover:text-ink" onClick={() => image.mutate({ image_item_id: c.item_id })} disabled={busy}>
                              {d.image_url ? "Взять эту картинку" : "Добавить в пост"}
                            </button>
                          )}
                        </div>
                      )}
                      <details className="mt-1">
                        <summary className="text-[12.5px] text-muted cursor-pointer">Что видела модель</summary>
                        <p className="mt-1 text-[12.5px] leading-relaxed">{c.excerpt}</p>
                      </details>
                    </div>
                    {c.url && <a href={c.url} target="_blank" rel="noreferrer noopener" className="text-muted hover:text-ink mt-0.5" aria-label={`Открыть источник ${c.n}`}><ExternalLink size={14} /></a>}
                  </div>
                </li>
              ))}
            </ol>
          </section>
        </aside>
      </div>
    </article>
  );
}

function Checks({ checks, maxLength, kind }: { checks: DraftChecks; maxLength: number; kind: string }) {
  const lines: { tone: "ok" | "warn" | "bad" | "info"; text: string }[] = [];
  if (checks.automatic_review) {
    const r = checks.automatic_review;
    if (r.overridden) {
      lines.push({ tone: "warn", text: `Опубликовано в режиме автопубликации, несмотря на замечания: ${r.reasons.join("; ")}.` });
    } else {
      lines.push(r.passed
        ? r.auto_publish_enabled
          ? { tone: "ok", text: "Автопроверка пройдена; пост поставлен в очередь публикации." }
          : { tone: "ok", text: "Автопроверка пройдена; черновик ждёт вашего решения." }
        : { tone: "warn", text: `Автопроверка нашла замечания, пост остался на проверке редактора: ${r.reasons.join("; ")}.` });
    }
  }
  if (checks.mode === "extractive") {
    lines.push({ tone: "info", text: "Текст собран из предложений источников без языковой модели: числа и факты дословно из материалов. Стиль сухой — при необходимости поправьте вручную." });
  } else {
    const un = checks.unsupported_numbers ?? [];
    lines.push(un.length ? { tone: "warn", text: `Числа, которых нет в источниках: ${un.join(", ")}. Проверьте вручную.` } : { tone: "ok", text: "Все числа из текста встречаются в источниках." });
    const inv = checks.invalid_citations ?? [];
    if (inv.length) lines.push({ tone: "bad", text: `Ссылки на несуществующие источники: ${inv.map((n) => `[${n}]`).join(" ")}.` });
    if (checks.citation_coverage != null) lines.push({ tone: checks.citation_coverage < 0.5 ? "warn" : "ok", text: `Со ссылкой на источник: ${Math.round(checks.citation_coverage * 100)}% предложений.` });
  }
  if (checks.fallback) lines.push({ tone: "warn", text: checks.fallback });
  if (kind === "post" && (checks.length ?? 0) > maxLength * 1.15) lines.push({ tone: "warn", text: `Длиннее заданного лимита проекта (${maxLength} симв.).` });
  if (checks.edited) lines.push({ tone: "info", text: "Текст правился вручную." });
  const icon = { ok: <Check size={15} style={{ color: "var(--ok)" }} />, warn: <AlertTriangle size={15} style={{ color: "var(--warn)" }} />, bad: <AlertTriangle size={15} style={{ color: "var(--bad)" }} />, info: <Info size={15} style={{ color: "var(--info)" }} /> };
  return (
    <ul className="space-y-2.5">
      {lines.map((l, i) => <li key={i} className="flex gap-2 text-[13.5px] leading-snug"><span className="mt-0.5 shrink-0">{icon[l.tone]}</span><span>{l.text}</span></li>)}
      <li className="hint pt-1">Проверки эвристические: они ловят типичные ошибки, но не заменяют чтение источников.</li>
    </ul>
  );
}

/** Заголовок переносится на несколько строк и растёт по высоте, а не обрезается по ширине колонки. Перевод строки в нём запрещён. */
function TitleField({ value, onChange, readOnly }: { value: string; onChange: (v: string) => void; readOnly: boolean }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [value]);
  return (
    <textarea
      ref={ref} rows={1}
      className="block w-full resize-none overflow-hidden break-words bg-transparent font-serif text-[26px] font-semibold leading-tight py-1 -mx-1 px-1 mb-3"
      style={{ border: "1px solid transparent", borderRadius: 3, overflowWrap: "anywhere" }}
      value={value} onChange={(e) => onChange(e.target.value.replace(/\s*\n\s*/g, " "))} onKeyDown={(e) => e.key === "Enter" && e.preventDefault()}
      readOnly={readOnly} placeholder="Заголовок" aria-label="Заголовок"
      onFocus={(e) => (e.currentTarget.style.borderColor = "var(--rule)")} onBlur={(e) => (e.currentTarget.style.borderColor = "transparent")}
    />
  );
}

/** Поле растёт вместе с текстом: длинный дайджест не приходится прокручивать внутри маленького окошка. */
function AutoTextarea({ value, onChange, readOnly }: { value: string; onChange: (v: string) => void; readOnly: boolean }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.max(el.scrollHeight + 2, 320)}px`;
  }, [value]);
  return (
    <textarea
      ref={ref} className="field prose-draft w-full" style={{ minHeight: 320, padding: "14px 16px", resize: "vertical", overflow: "hidden" }}
      value={value} onChange={(e) => onChange(e.target.value)} readOnly={readOnly} aria-label="Текст поста"
    />
  );
}
