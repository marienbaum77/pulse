import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Check, ChevronLeft, ChevronRight, CornerDownRight, ExternalLink, ImageOff, Info, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { ago, stripCitations } from "../lib/format";
import { CHANNEL_TYPES, DRAFT_STATUS, PUB_STATUS } from "../lib/labels";
import type { Channel, Cluster, Draft, DraftChecks, DraftListItem, GenerationJob, Project } from "../lib/types";
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
  const { can } = useAuth();
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
  const clustersQ = useQuery({
    queryKey: ["clusters", project?.id],
    queryFn: () => api<Cluster[]>("/clusters", { params: { project_id: project!.id } }),
    enabled: !!project,
  });
  const quickApprove = useMutation({
    mutationFn: (draftId: number) => api(`/drafts/${draftId}/approve`, { method: "POST" }),
    onSuccess: () => {
      toast("Пост утверждён и отправлен в публикацию", "ok");
      qc.invalidateQueries({ queryKey: ["drafts"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["publications"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const clearAll = useMutation({
    mutationFn: () => api<{ deleted: number; cancelled_jobs?: number }>("/drafts", { method: "DELETE", params: { project_id: project!.id } }),
    onSuccess: ({ deleted, cancelled_jobs }) => {
      const msg = cancelled_jobs
        ? `Удалено черновиков: ${deleted}, отменено генераций: ${cancelled_jobs}`
        : `Удалено черновиков: ${deleted}`;
      toast(msg);
      qc.invalidateQueries({ queryKey: ["drafts"] });
      qc.invalidateQueries({ queryKey: ["generation-queue"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["clusters"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const cancelJob = useMutation({
    mutationFn: (jobId: number) =>
      api(`/generation-queue/${jobId}`, {
        method: "DELETE",
        params: { project_id: project!.id },
      }),
    onSuccess: () => {
      toast("Генерация отменена");
      qc.invalidateQueries({ queryKey: ["generation-queue"] });
      qc.invalidateQueries({ queryKey: ["drafts"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  if (loading) return <Spinner />;
  if (!project) return <NoProject />;

  const all = list.data ?? [];
  const queued = generationQueue.data ?? [];
  const clearable = all.some((d) => ["pending_review", "rejected", "failed"].includes(d.status)) || queued.length > 0;
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
  const clustersMap = new Map((clustersQ.data ?? []).map((c) => [c.id, c]));
  const getDraftInterest = (d: DraftListItem) => {
    if (!d.cluster_ids || d.cluster_ids.length === 0) return -1;
    const scores = d.cluster_ids.map((cid) => clustersMap.get(cid)?.interest ?? -1);
    return Math.max(...scores, -1);
  };
  const rows = [...all.filter(def.match)].sort((a, b) => {
    const intA = getDraftInterest(a);
    const intB = getDraftInterest(b);
    if (intB !== intA) return intB - intA;
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
  });
  const counts = Object.fromEntries(TABS.map((t) => [t.id, all.filter(t.match).length + (t.id === "generating" ? queued.length : 0)]));

  return (
    <div className="fadein">
      <PageHeader
        title="Посты"
        sub="Генерация, проверка и публикация постов. Сортируются по интересности сюжета."
        actions={clearable && <button className="btn btn-danger" disabled={clearAll.isPending} title="Утверждённые публикации и выполняющиеся генерации сохранятся" onClick={() => window.confirm("Удалить все черновики на проверке, отклонённые и сбои? Утверждённая история и выполняющиеся генерации останутся.") && clearAll.mutate()}><Trash2 size={15} />{clearAll.isPending ? "Очищаем…" : "Очистить все"}</button>}
      />
      <div className="max-w-[860px]">
        <Tabs className="mb-1" tabs={TABS.map((t) => ({ id: t.id, label: t.label, count: counts[t.id] }))} value={tab} onChange={setTab} />
        {list.isLoading && <Spinner />}
        {list.error && <ErrorNote error={list.error} />}
        {tab === "generating" && generationQueue.error && <ErrorNote error={generationQueue.error} />}
        {list.data && rows.length === 0 && (tab !== "generating" || queued.length === 0) && (
          <Empty title={tab === "pending_review" ? "Постов на проверке нет" : tab === "generating" ? "Очередь пуста" : "Здесь пусто"} text={tab === "pending_review" ? "Новые посты появятся после генерации. В автоматическом режиме прошедшие проверки посты публикуются без ожидания редактора." : tab === "generating" ? "Посты появятся здесь сразу после постановки в очередь или старта генерации." : undefined} />
        )}
        <ul>
          {tab === "generating" && queued.map((job) => (
            <li key={`job-${job.id}`} className="rule-b">
              <div className="flex items-center justify-between py-3.5 -mx-2 px-2 gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="inline-block w-2 h-2 rounded-full bg-accent animate-pulse shrink-0" />
                    <span className="font-sans text-[17.5px] font-medium leading-snug break-words">{job.title}</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[13.5px] text-muted mt-1">
                    <span>{job.kind === "digest" ? "Дайджест" : "Пост"}</span>
                    <span>{ago(job.created_at)}</span>
                    <Badge tone="info">Идёт обработка</Badge>
                  </div>
                </div>
                {can("editor") && (
                  <button
                    className="btn btn-secondary text-xs shrink-0"
                    disabled={cancelJob.isPending}
                    onClick={() => {
                      if (window.confirm("Отменить эту задачу генерации?")) {
                        cancelJob.mutate(job.id);
                      }
                    }}
                    title="Отменить задачу генерации"
                  >
                    Отменить
                  </button>
                )}
              </div>
            </li>
          ))}
          {rows.map((d) => {
            const warn = (d.checks.invalid_citations?.length ?? 0) > 0;
            const interest = getDraftInterest(d);
            return (
              <li key={d.id} className="rule-b">
                <div className="flex items-center justify-between py-3.5 hover:bg-sunken/60 -mx-2 px-2 rounded gap-4">
                  <Link to={`/drafts/${d.id}`} className="flex items-start gap-3.5 flex-1 min-w-0">
                    {d.has_image && <Thumb src={`/drafts/${d.id}/image?v=${encodeURIComponent(d.updated_at)}`} className="w-[88px] h-[58px] shrink-0 hidden sm:block" />}
                    <span className="min-w-0 flex-1">
                      <span className="block break-words font-sans text-[17.5px] font-medium leading-snug">{d.title || (d.status === "failed" ? "Не удалось написать" : "Подготовка поста")}</span>
                      <span className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[13.5px] text-muted mt-1">
                        <span>{d.kind === "digest" ? "Дайджест" : "Пост"}</span>
                        <span>{ago(d.created_at)}</span>
                        {interest >= 0 && (
                          <span className="font-mono text-[12.5px] font-semibold text-accent">★ {interest.toFixed(1)}</span>
                        )}
                        <Badge tone={DRAFT_STATUS[d.status].tone}>{DRAFT_STATUS[d.status].label}</Badge>
                        {warn && <span className="text-warn inline-flex items-center gap-1"><AlertTriangle size={12} />есть замечания</span>}
                      </span>
                      {d.status === "failed" && d.error && <span className="mt-1 block line-clamp-2 text-[12.5px] text-bad">{d.error}</span>}
                    </span>
                  </Link>
                  {can("editor") && d.status === "pending_review" && (
                    <button
                      type="button"
                      className="btn btn-sm btn-primary shrink-0 self-center"
                      disabled={quickApprove.isPending}
                      onClick={(e) => {
                        e.stopPropagation();
                        quickApprove.mutate(d.id);
                      }}
                      title="Быстро утвердить и отправить в канал"
                    >
                      ✓ Опубликовать
                    </button>
                  )}
                </div>
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
  const channelsQ = useQuery({
    queryKey: ["channels", project.id],
    queryFn: () => api<Channel[]>("/channels", { params: { project_id: project.id } }),
  });
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [selectedChannels, setSelectedChannels] = useState<number[] | null>(null);
  const d = q.data;

  const enabledChannels = (channelsQ.data ?? []).filter((c) => c.enabled);
  const activeChannelIds = selectedChannels ?? enabledChannels.map((c) => c.id);

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
      return api<{ already: boolean; suppressed: number }>(`/drafts/${id}/approve`, {
        method: "POST",
        body: { channel_ids: activeChannelIds },
      });
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
    mutationFn: (patch: { remove_image?: boolean; remove_video?: boolean; remove_reply?: boolean; remove_extra_images?: boolean; image_item_id?: number }) =>
      api(`/drafts/${id}`, { method: "PATCH", body: patch }),
    onSuccess: (_r, patch) => {
      toast(patch.remove_reply ? "Пост выйдет отдельным сообщением" : patch.remove_video ? "Ролик убран" : patch.remove_extra_images ? "Пост выйдет с одной картинкой" : "Картинка обновлена");
      refresh();
    },
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
          {d.reply_to_draft_id && d.checks.related && (
            <p className="flex flex-wrap items-center gap-x-3 gap-y-1 mb-3 text-[13.5px]">
              <CornerDownRight size={15} className="text-muted" aria-hidden />
              <span>Ответ на пост <Link to={`/drafts/${d.reply_to_draft_id}`} className="underline underline-offset-4">«{d.checks.related.title}»</Link> — {RELATED_REASONS[d.checks.related.reason]}</span>
              {editable && <button className="btn btn-sm" onClick={() => image.mutate({ remove_reply: true })} disabled={busy}>Не отвечать</button>}
            </p>
          )}
          {d.video_url && (
            <figure className="mb-3">
              {d.video_kind === "animation"
                ? <img key={`a-${d.id}-${d.updated_at}`} src={`/api/drafts/${d.id}/video`} alt="" loading="lazy" className="w-full max-h-80 object-contain bg-sunken" style={{ borderRadius: 3 }} />
                : <video key={`v-${d.id}-${d.updated_at}`} src={`/api/drafts/${d.id}/video`} controls muted preload="none" className="w-full max-h-80 bg-sunken" style={{ borderRadius: 3 }}
                    poster={d.image_url ? `/api/drafts/${d.id}/image?v=${encodeURIComponent(d.updated_at)}` : undefined} />}
              <figcaption className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5">
                {editable && <button className="btn btn-sm" onClick={() => image.mutate({ remove_video: true })} disabled={busy}><ImageOff size={14} />{d.video_kind === "animation" ? "Убрать GIF" : "Убрать видео"}</button>}
                <span className="hint">{d.video_kind === "animation" ? "GIF" : "Видео"} из первоисточника, README или статьи, как есть (до 50 МБ). Telegram получит его вместо картинки; ВКонтакте и Discord — картинку ниже.</span>
              </figcaption>
            </figure>
          )}
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
          {d.image_url && d.extra_images.length > 0 && (
            <figure className="mb-3">
              <div className="grid grid-cols-3 gap-2">
                {d.extra_images.map((_, n) => <Thumb key={`x-${d.id}-${n}-${d.updated_at}`} src={`/drafts/${d.id}/extra/${n}?v=${encodeURIComponent(d.updated_at)}`} className="w-full h-28" />)}
              </div>
              <figcaption className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5">
                {editable && <button className="btn btn-sm" onClick={() => image.mutate({ remove_extra_images: true })} disabled={busy}><ImageOff size={14} />Только обложка</button>}
                <span className="hint">Ещё иллюстрации из первоисточника, README или статьи: в Telegram пост уйдёт альбомом{d.video_url && d.video_kind !== "animation" ? " вместе с видео" : ""}, во ВКонтакте — несколькими фото.</span>
              </figcaption>
            </figure>
          )}
          <AutoTextarea value={body} onChange={setBody} readOnly={!editable} />
          <div className="flex flex-wrap items-center justify-between gap-x-4 mt-1.5 text-[12.5px] text-muted">
            <span className="num">{body.length} симв.{body.length > 3800 && <span className="text-warn ml-2">Telegram принимает до 4096 символов</span>}</span>
            <span>Номера [1] — только для сверки с источниками справа; при публикации они убираются. **Жирный** и [текст](https://…) становятся форматированием</span>
          </div>
          <details className="mt-4">
            <summary className="cursor-pointer text-sm font-medium">Как увидят подписчики</summary>
            <div className="mt-2 px-4 py-3.5 prose-draft whitespace-pre-wrap break-words" style={{ background: "var(--sunken)", borderRadius: 3, fontSize: 16 }}>
              {d.image_url && <Thumb key={`pv-${d.id}-${d.updated_at}`} src={`/drafts/${d.id}/image?v=${encodeURIComponent(d.updated_at)}`} className="w-full max-h-72 mb-3" />}
              {title && <p className="font-semibold mb-3">{title}</p>}
              {renderMarkup(stripCitations(body, new Set(d.citations.map((c) => c.n))))}
            </div>
          </details>

          {can("editor") && d.status === "pending_review" && enabledChannels.length > 0 && (
            <div className="mt-6 mb-2 p-3.5 bg-sunken/60 rounded border border-ink/5">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-semibold text-muted uppercase tracking-wider">Каналы публикации:</span>
                <span className="text-xs text-muted">Выбрано: {activeChannelIds.length} из {enabledChannels.length}</span>
              </div>
              <div className="flex flex-wrap gap-2">
                {enabledChannels.map((ch) => {
                  const isSelected = activeChannelIds.includes(ch.id);
                  return (
                    <button
                      key={ch.id}
                      type="button"
                      className={`btn btn-sm text-xs py-1 px-2.5 transition-colors ${
                        isSelected ? "btn-primary" : "btn-ghost text-muted border border-ink/10"
                      }`}
                      onClick={() => {
                        if (isSelected) {
                          setSelectedChannels(activeChannelIds.filter((cid) => cid !== ch.id));
                        } else {
                          setSelectedChannels([...activeChannelIds, ch.id]);
                        }
                      }}
                    >
                      {isSelected ? "✓ " : ""}{ch.name} <span className="opacity-70 text-[11px]">({CHANNEL_TYPES[ch.type]})</span>
                    </button>
                  );
                })}
              </div>
              {activeChannelIds.length === 0 && (
                <p className="text-bad text-xs mt-2">Выберите хотя бы один канал доставки для отправки</p>
              )}
            </div>
          )}

          {can("editor") && (
            <div className="flex flex-wrap items-center gap-2 mt-4">
              {d.status === "pending_review" && (
                <button
                  className="btn btn-primary h-10"
                  onClick={() => approve.mutate()}
                  disabled={busy || (enabledChannels.length > 0 && activeChannelIds.length === 0)}
                  title={enabledChannels.length > 0 && activeChannelIds.length === 0 ? "Выберите хотя бы один канал доставки" : undefined}
                >
                  <Check size={16} />Утвердить и отправить
                </button>
              )}
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

const RELATED_REASONS: Record<NonNullable<DraftChecks["related"]>["reason"], string> = {
  update: "обновление того же сюжета",
  link: "тот же репозиторий или страница",
  origin: "тот же первоисточник",
  similar: "близкая тема и общее имя в заголовке",
};

// Та же разметка, что понимает публикатор (backend/app/markup.py): [текст](https://адрес) и **жирный**.
const MARKUP = /\[([^[\]\n]{1,200})\]\((https?:\/\/[^\s()<>]+)\)|\*\*(?=\S)([^*\n]{1,300}?)(?<=\S)\*\*/g;

function renderMarkup(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(MARKUP)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    out.push(m[2]
      ? <a key={at} href={m[2]} target="_blank" rel="noreferrer" className="underline underline-offset-4">{m[1]}</a>
      : <b key={at}>{m[3]}</b>);
    last = at + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

function Checks({ checks, maxLength, kind }: { checks: DraftChecks; maxLength: number; kind: string }) {
  const lines: { tone: "ok" | "warn" | "bad" | "info"; text: string }[] = [];
  if (checks.automatic_review) {
    const r = checks.automatic_review;
    if (r.instant) lines.push({ tone: "info", text: "Пост создан сразу после оценки: интересность сюжета выше порога проекта." });
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
      className="block w-full resize-none overflow-hidden break-words bg-transparent font-sans text-[24px] font-semibold leading-tight py-1 -mx-1 px-1 mb-3"
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
