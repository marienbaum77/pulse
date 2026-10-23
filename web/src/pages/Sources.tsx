import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileUp, Globe, MessagesSquare, Newspaper, Pencil, Plus, RefreshCw, Rss, Search, Send, Trash2, Upload, Video } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { ago, fmtNum } from "../lib/format";
import { CONTENT_KINDS } from "../lib/labels";
import type { ConnectorPreview, ConnectorType, ContentMode, Source } from "../lib/types";
import { DiscoveryPanel } from "../components/Discovery";
import { NoProject } from "../components/NoProject";
import { Badge, Dialog, Empty, ErrorNote, Field, PageHeader, Spinner, Toggle } from "../components/ui";

type Kind = "rss" | "news_search" | ConnectorType;
interface Options { time_range: string; category: string; sort: string; period: string; mode: "" | ContentMode }
interface Form { id?: number; kind: Kind; name: string; url: string; value: string; options: Options; authority: number; poll_minutes: number; enabled: boolean }
const BLANK_OPTIONS: Options = { time_range: "", category: "general", sort: "top", period: "week", mode: "" };
const POLL = [[15, "каждые 15 минут"], [30, "каждые 30 минут"], [60, "раз в час"], [180, "раз в 3 часа"], [360, "раз в 6 часов"], [720, "раз в 12 часов"], [1440, "раз в сутки"]] as const;
const TIME_RANGES = [["", "за всё время"], ["day", "за сутки"], ["week", "за неделю"], ["month", "за месяц"], ["year", "за год"]] as const;

/** Что добавляет каждый тип и с какой частотой его разумно опрашивать: поиск и Reddit не любят частых запросов. */
const KINDS: Record<Kind, { label: string; icon: LucideIcon; poll: number; field: string; placeholder: string; hint: string }> = {
  rss: { label: "RSS-лента", icon: Rss, poll: 30, field: "Адрес ленты", placeholder: "https://example.com/rss", hint: "" },
  news_search: {
    label: "Google Новости", icon: Newspaper, poll: 30, field: "Что искать?", placeholder: '"Python разработка" -змея',
    hint: "Запрос регулярно опрашивается как RSS-лента Google News. Уточняйте тему и используйте операторы, например site:habr.com.",
  },
  youtube: {
    label: "YouTube", icon: Video, poll: 180, field: "Канал, плейлист или поисковый запрос", placeholder: "https://www.youtube.com/@channel или «уроки Python для начинающих»",
    hint: "Ссылка на канал (youtube.com/@name) или плейлист (?list=…) читается через RSS YouTube. Текст запроса ищет видео через SearXNG. Расшифровка видео подгружается из субтитров.",
  },
  web_search: {
    label: "Веб-поиск", icon: Search, poll: 360, field: "Поисковый запрос", placeholder: "гайд по настройке PostgreSQL",
    hint: "Ищет статьи на любых сайтах через SearXNG; полный текст страниц дозагружается сам. Конкретный запрос работает лучше общего.",
  },
  site: {
    label: "Сайт без RSS", icon: Globe, poll: 360, field: "Адрес раздела или блога", placeholder: "https://example.com/blog",
    hint: "Если у сайта есть RSS, Pulse найдёт её сам. Иначе берёт ссылки на статьи со страницы — укажите страницу со списком материалов, а не главную.",
  },
  reddit: {
    label: "Reddit", icon: MessagesSquare, poll: 180, field: "Сабреддит", placeholder: "r/MachineLearning",
    hint: "Публичная лента сабреддита. Если пост — ссылка на статью или видео, Pulse загрузит их текст. Число апвоутов Reddit без авторизации не отдаёт, поэтому лучше брать «лучшее за неделю».",
  },
  telegram: {
    label: "Telegram-канал", icon: Send, poll: 60, field: "Публичный канал", placeholder: "@channel или https://t.me/channel",
    hint: "Читает веб-превью t.me/s/канал: бот и вход в аккаунт не нужны, но канал должен быть публичным.",
  },
};
const CONNECTORS: ConnectorType[] = ["youtube", "web_search", "site", "reddit", "telegram"];

function blank(kind: Kind): Form {
  return { kind, name: "", url: "", value: "", options: BLANK_OPTIONS, authority: 0.5, poll_minutes: KINDS[kind].poll, enabled: true };
}

function sourceKind(s: Source): Kind {
  if (s.type === "rss") return s.url.includes("news.google.com/rss/search?") ? "news_search" : "rss";
  return s.type === "manual" ? "rss" : s.type;
}

function describe(s: Source): string {
  const c = s.config ?? {};
  switch (s.type) {
    case "youtube": return c.kind === "search" ? `Поиск видео: ${c.query}` : c.kind === "playlist" ? "Плейлист" : "Канал";
    case "web_search": return `Запрос: ${c.query}`;
    case "site": return c.feed_url ? `Найдена RSS-лента: ${c.feed_url}` : "Ссылки со страницы";
    case "reddit": return `${c.sort === "top" ? `лучшее за ${c.period === "day" ? "сутки" : c.period === "month" ? "месяц" : c.period === "year" ? "год" : "неделю"}` : c.sort === "new" ? "новое" : "популярное"}`;
    default: return "";
  }
}

export default function Sources() {
  const { project, loading } = useProject();
  const { can } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const [form, setForm] = useState<Form | null>(null);
  const [preview, setPreview] = useState<{ count: number; sample: string[] } | string | null>(null);
  const [searchPreview, setSearchPreview] = useState<{ count: number; new_count: number; sample: { title: string; publisher: string | null }[] } | string | null>(null);
  const [connectorPreview, setConnectorPreview] = useState<ConnectorPreview | string | null>(null);
  const opmlRef = useRef<HTMLInputElement>(null);
  const [opmlBusy, setOpmlBusy] = useState(false);
  const q = useQuery({ queryKey: ["sources", project?.id], queryFn: () => api<Source[]>("/sources", { params: { project_id: project!.id } }), enabled: !!project, refetchInterval: 5000 });
  const refresh = () => qc.invalidateQueries({ queryKey: ["sources"] });
  const resetPreviews = () => { setPreview(null); setSearchPreview(null); setConnectorPreview(null); };
  const open = (f: Form) => { setForm(f); resetPreviews(); };

  const connectorBody = (f: Form) => ({
    project_id: project!.id, type: f.kind, value: f.value.trim(),
    options: { ...f.options, time_range: f.options.time_range || null, mode: f.options.mode || null },
  });
  const save = useMutation({
    mutationFn: (f: Form) => {
      const common = { authority: f.authority, poll_minutes: f.poll_minutes, enabled: f.enabled };
      if (f.id) return api(`/sources/${f.id}`, { method: "PUT", body: { project_id: project!.id, type: "rss", name: f.name, url: f.url, ...common } });
      if (f.kind === "news_search") return api("/sources/search", { method: "POST", body: { project_id: project!.id, query: f.value, ...common } });
      if (f.kind === "rss") return api("/sources", { method: "POST", body: { project_id: project!.id, type: "rss", name: f.name, url: f.url, ...common } });
      return api("/sources/connector", { method: "POST", body: { ...connectorBody(f), name: f.name.trim() || null, ...common } });
    },
    onSuccess: () => { toast(form?.id ? "Источник сохранён" : "Источник добавлен, первый сбор запущен"); setForm(null); resetPreviews(); refresh(); },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api(`/sources/${id}`, { method: "DELETE" }),
    onSuccess: () => { toast("Источник удалён"); refresh(); },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const fetchNow = useMutation({
    mutationFn: (id: number) => api<{ queued: boolean }>(`/sources/${id}/fetch`, { method: "POST" }),
    onSuccess: ({ queued }) => {
      toast(queued ? "Сбор поставлен в очередь" : "Сбор уже выполняется", "info");
      refresh();
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const check = useMutation({
    mutationFn: (url: string) => api<{ count: number; sample: string[] }>("/sources/preview", { method: "POST", body: { url } }),
    onSuccess: setPreview,
    onError: (e: Error) => setPreview(e.message),
  });
  const checkSearch = useMutation({
    mutationFn: (query: string) => api<{ count: number; new_count: number; sample: { title: string; publisher: string | null }[] }>("/sources/search/preview", {
      method: "POST", body: { project_id: project!.id, query },
    }),
    onSuccess: setSearchPreview,
    onError: (e: Error) => setSearchPreview(e.message),
  });
  const checkConnector = useMutation({
    mutationFn: (f: Form) => api<ConnectorPreview>("/sources/connector/preview", { method: "POST", body: connectorBody(f) }),
    onSuccess: setConnectorPreview,
    onError: (e: Error) => setConnectorPreview(e.message),
  });

  async function onOpml(file: File) {
    if (!project) return;
    setOpmlBusy(true);
    try {
      const fd = new FormData();
      fd.append("project_id", String(project.id));
      fd.append("authority", "0.5");
      fd.append("poll_minutes", "30");
      fd.append("enabled", "true");
      fd.append("file", file);
      const r = await api<{ created: unknown[]; skipped: string[]; errors: unknown[] }>("/sources/opml", { method: "POST", body: fd });
      const n = r.created.length;
      const parts = [`Добавлено лент: ${n}`];
      if (r.skipped.length) parts.push(`уже были: ${r.skipped.length}`);
      if (r.errors.length) parts.push(`ошибок: ${r.errors.length}`);
      toast(parts.join(", "), n ? "info" : "bad");
      refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Не удалось импортировать OPML", "bad");
    } finally {
      setOpmlBusy(false);
      if (opmlRef.current) opmlRef.current.value = "";
    }
  }

  if (loading) return <Spinner />;
  if (!project) return <NoProject />;
  const rows = q.data ?? [];
  const patch = (p: Partial<Form>) => setForm((f) => (f ? { ...f, ...p } : f));
  const patchOptions = (p: Partial<Options>) => { setForm((f) => (f ? { ...f, options: { ...f.options, ...p } } : f)); setConnectorPreview(null); };
  const isConnector = (k: Kind): k is ConnectorType => (CONNECTORS as string[]).includes(k);
  const editingConnector = !!form?.id && isConnector(form.kind);
  const canSave = !form || save.isPending ? false
    : form.id ? !!form.name.trim()
    : form.kind === "rss" ? !!form.name && !!form.url
    : form.value.trim().length >= 2;

  return (
    <div className="fadein">
      <PageHeader
        title="Источники"
        sub="Откуда Pulse берёт материалы: RSS-ленты, YouTube, веб-поиск, сайты без RSS, Reddit и Telegram. Авторитетность влияет на вес: чем она выше, тем сильнее вклад источника."
        actions={can("editor") && (
          <div className="flex flex-wrap gap-2">
            <input ref={opmlRef} type="file" accept=".opml,.xml,application/xml,text/xml" hidden onChange={(e) => e.target.files?.[0] && onOpml(e.target.files[0])} />
            <button className="btn" disabled={opmlBusy} onClick={() => opmlRef.current?.click()}><FileUp size={16} />{opmlBusy ? "Импорт…" : "Импортировать OPML"}</button>
            <button className="btn btn-primary" onClick={() => open(blank("rss"))}><Plus size={16} />Добавить источник</button>
          </div>
        )}
      />
      <DiscoveryPanel projectId={project.id} />
      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data && rows.length === 0 && <Empty title="Источников пока нет" text="Добавьте RSS-ленту, YouTube-канал, поисковый запрос или Telegram-канал, либо импортируйте подписки из OPML (Feedly, Inoreader и др.)." />}
      <ul>
        {rows.map((s) => {
          const tone = s.last_error ? "bad" : s.last_ok_at ? "ok" : "muted";
          const polled = s.type !== "manual";
          const kind = sourceKind(s);
          const detail = describe(s);
          return (
            <li key={s.id} className="rule-b py-3.5 grid md:grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-2 items-center">
              <div className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-x-3">
                  <span className="font-serif text-[17px] font-medium">{s.name}</span>
                  {s.type === "manual" ? <span className="eyebrow">импорт</span> : <span className="eyebrow">{KINDS[kind].label}</span>}
                  {s.config?.mode && <span className="eyebrow">{s.config.mode === "curated" ? "подборка" : "новости"}</span>}
                  {!s.enabled && polled && <span className="eyebrow">отключён</span>}
                </div>
                {detail && <p className="text-[13px] text-muted truncate">{detail}</p>}
                {s.url && !detail.startsWith("Найдена") && <p className="text-[13px] text-muted truncate">{s.url}</p>}
                <div className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[13px] mt-1">
                  {polled && <Badge tone={tone}>{s.last_error ? "Ошибка" : s.last_ok_at ? `Обновлён ${ago(s.last_ok_at)}` : "Ещё не опрашивался"}</Badge>}
                  <span className="text-muted num">{fmtNum(s.item_count)} материалов</span>
                  {polled && s.last_ok_at && <span className="text-muted num">последняя выдача: {s.last_entry_count}, новых: {s.last_new_count}</span>}
                  <span className="text-muted num">авторитетность {s.authority.toFixed(2)}</span>
                  {polled && <span className="text-muted">{POLL.find((p) => p[0] === s.poll_minutes)?.[1] ?? `каждые ${s.poll_minutes} мин`}</span>}
                </div>
                {s.last_error && <p className="text-[13px] text-bad mt-1 break-words">{s.last_error}</p>}
              </div>
              {can("editor") && (
                <div className="flex gap-1.5">
                  {polled && <button className="btn btn-sm" onClick={() => fetchNow.mutate(s.id)} title="Опросить сейчас"><RefreshCw size={14} />Опросить</button>}
                  {polled && <button className="btn btn-sm btn-ghost" aria-label="Изменить" onClick={() => open({ ...blank(kind === "news_search" ? "rss" : kind), id: s.id, name: s.name, url: s.url, authority: s.authority, poll_minutes: s.poll_minutes, enabled: s.enabled })}><Pencil size={15} /></button>}
                  <button className="btn btn-sm btn-ghost btn-danger" aria-label="Удалить" onClick={() => confirm(`Удалить источник «${s.name}»? Собранные материалы останутся.`) && remove.mutate(s.id)}><Trash2 size={15} /></button>
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {can("editor") && <ImportBox projectId={project.id} />}

      <Dialog
        wide open={!!form} onClose={() => { setForm(null); resetPreviews(); }} title={form?.id ? "Изменить источник" : "Новый источник"}
        footer={<><button className="btn" onClick={() => setForm(null)}>Отмена</button><button className="btn btn-primary" disabled={!canSave} onClick={() => form && save.mutate(form)}>{save.isPending ? "Сохраняем…" : "Сохранить"}</button></>}
      >
        {form && (
          <>
            {!form.id && (
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5" role="radiogroup" aria-label="Тип источника">
                {(Object.keys(KINDS) as Kind[]).map((k) => {
                  const Icon = KINDS[k].icon;
                  const active = form.kind === k;
                  return (
                    <button
                      key={k} type="button" role="radio" aria-checked={active}
                      className={`btn btn-sm justify-start ${active ? "btn-primary" : ""}`}
                      onClick={() => { setForm({ ...blank(k), authority: form.authority, enabled: form.enabled }); resetPreviews(); }}
                    >
                      <Icon size={14} />{KINDS[k].label}
                    </button>
                  );
                })}
              </div>
            )}
            {KINDS[form.kind].hint && !form.id && <p className="text-[13px] text-muted">{KINDS[form.kind].hint}</p>}

            {form.id ? (
              <>
                <Field label="Название"><input className="field" value={form.name} onChange={(e) => patch({ name: e.target.value })} autoFocus /></Field>
                {!editingConnector && (
                  <Field label="Адрес ленты">
                    <input className="field" value={form.url} onChange={(e) => patch({ url: e.target.value })} inputMode="url" />
                  </Field>
                )}
                {editingConnector && <p className="text-[13px] text-muted">Канал, запрос или адрес коннектора не меняются: удалите источник и добавьте новый.</p>}
              </>
            ) : form.kind === "rss" ? (
              <>
                <Field label="Название"><input className="field" value={form.name} onChange={(e) => patch({ name: e.target.value })} placeholder="Например, Хабр: новости" autoFocus /></Field>
                <Field label={KINDS.rss.field}>
                  <div className="flex gap-2">
                    <input className="field" value={form.url} onChange={(e) => { patch({ url: e.target.value }); setPreview(null); }} placeholder={KINDS.rss.placeholder} inputMode="url" />
                    <button className="btn shrink-0" disabled={!form.url || check.isPending} onClick={() => check.mutate(form.url)}>{check.isPending ? "Читаем…" : "Проверить"}</button>
                  </div>
                  {typeof preview === "string" && <span className="block mt-1.5 text-[13px] text-bad">{preview}</span>}
                  {preview && typeof preview !== "string" && (
                    <span className="block mt-2 text-[13px]"><b>Лента читается:</b> {preview.count} записей. Например:
                      <span className="block text-muted mt-1">{preview.sample.map((t, i) => <span key={i} className="block truncate">— {t}</span>)}</span>
                    </span>
                  )}
                </Field>
              </>
            ) : form.kind === "news_search" ? (
              <Field label={KINDS.news_search.field}>
                <div className="flex gap-2">
                  <input className="field" value={form.value} maxLength={300} onChange={(e) => { patch({ value: e.target.value }); setSearchPreview(null); }} placeholder={KINDS.news_search.placeholder} autoFocus />
                  <button className="btn shrink-0" disabled={form.value.trim().length < 2 || checkSearch.isPending} onClick={() => checkSearch.mutate(form.value.trim())}>{checkSearch.isPending ? "Ищем…" : "Предпросмотр"}</button>
                </div>
                {typeof searchPreview === "string" && <span className="block mt-1.5 text-[13px] text-bad">{searchPreview}</span>}
                {searchPreview && typeof searchPreview !== "string" && (
                  <span className="block mt-2 text-[13px]"><b>Найдено: {searchPreview.count}, новых в окне проекта: {searchPreview.new_count}.</b>
                    <span className="block text-muted mt-1">{searchPreview.sample.map((item, i) => <span key={i} className="block truncate">— {item.title}{item.publisher ? ` · ${item.publisher}` : ""}</span>)}</span>
                  </span>
                )}
              </Field>
            ) : (
              <>
                <Field label={KINDS[form.kind].field}>
                  <div className="flex gap-2">
                    <input className="field" value={form.value} maxLength={500} onChange={(e) => { patch({ value: e.target.value }); setConnectorPreview(null); }} placeholder={KINDS[form.kind].placeholder} autoFocus />
                    <button className="btn shrink-0" disabled={form.value.trim().length < 2 || checkConnector.isPending} onClick={() => checkConnector.mutate(form)}>{checkConnector.isPending ? "Проверяем…" : "Предпросмотр"}</button>
                  </div>
                  {typeof connectorPreview === "string" && <span className="block mt-1.5 text-[13px] text-bad">{connectorPreview}</span>}
                  {connectorPreview && typeof connectorPreview !== "string" && <ConnectorSample p={connectorPreview} />}
                </Field>
                <ConnectorOptionsFields kind={form.kind} value={form.value} options={form.options} onChange={patchOptions} mixed={project.content_profile === "mixed"} />
                <Field label="Название" hint="Пусто — подставится автоматически (название канала, запрос или домен).">
                  <input className="field" value={form.name} maxLength={120} onChange={(e) => patch({ name: e.target.value })} placeholder={connectorPreview && typeof connectorPreview !== "string" ? connectorPreview.name : ""} />
                </Field>
              </>
            )}

            <div className="grid sm:grid-cols-2 gap-4">
              <Field label={`Авторитетность: ${form.authority.toFixed(2)}`} hint="0 — сомнительный, 1 — первоисточник">
                <input type="range" min={0} max={1} step={0.05} className="w-full accent-[var(--brand)]" value={form.authority} onChange={(e) => patch({ authority: Number(e.target.value) })} />
              </Field>
              <Field label="Как часто опрашивать">
                <select className="field" value={form.poll_minutes} onChange={(e) => patch({ poll_minutes: Number(e.target.value) })}>{POLL.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
              </Field>
            </div>
            <Toggle checked={form.enabled} onChange={(v) => patch({ enabled: v })} label="Источник включён" />
          </>
        )}
      </Dialog>
    </div>
  );
}

function ConnectorSample({ p }: { p: ConnectorPreview }) {
  return (
    <span className="block mt-2 text-[13px]">
      <b>{p.name}: найдено {p.count}, новых {p.new_count}.</b>{" "}
      <span className="text-muted">Материалы пойдут {p.mode === "curated" ? "в подборку (каждый — отдельный кандидат)" : "в новостные сюжеты"}.</span>
      {p.feed_url && <span className="block text-muted mt-1">На сайте нашлась RSS-лента — она и будет читаться: {p.feed_url}</span>}
      <span className="block text-muted mt-1">
        {p.sample.map((item, i) => (
          <span key={i} className="block truncate">— <span className="eyebrow">{CONTENT_KINDS[item.kind]}</span> {item.title}{item.publisher ? ` · ${item.publisher}` : ""}</span>
        ))}
      </span>
    </span>
  );
}

function ConnectorOptionsFields({ kind, value, options, onChange, mixed }: { kind: ConnectorType; value: string; options: Options; onChange: (p: Partial<Options>) => void; mixed: boolean }) {
  // Для YouTube период имеет смысл только у поискового запроса: лента канала всегда отдаёт последние видео.
  const youtubeSearch = kind === "youtube" && !/youtube\.com|youtu\.be|^@|^UC[\w-]{22}$/i.test(value.trim());
  const timeRange = (kind === "web_search" || youtubeSearch) && (
    <Field label="Период публикации">
      <select className="field" value={options.time_range} onChange={(e) => onChange({ time_range: e.target.value })}>{TIME_RANGES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
    </Field>
  );
  return (
    <>
      {(timeRange || kind === "web_search" || kind === "reddit") && (
        <div className="grid sm:grid-cols-2 gap-4">
          {kind === "web_search" && (
            <Field label="Категория поиска">
              <select className="field" value={options.category} onChange={(e) => onChange({ category: e.target.value })}>
                <option value="general">Все сайты</option>
                <option value="news">Новости</option>
                <option value="it">ИТ</option>
                <option value="science">Наука</option>
              </select>
            </Field>
          )}
          {timeRange}
          {kind === "reddit" && (
            <>
              <Field label="Сортировка">
                <select className="field" value={options.sort} onChange={(e) => onChange({ sort: e.target.value })}>
                  <option value="top">Лучшее</option>
                  <option value="hot">Популярное сейчас</option>
                  <option value="new">Новое</option>
                </select>
              </Field>
              {options.sort === "top" && (
                <Field label="Лучшее за">
                  <select className="field" value={options.period} onChange={(e) => onChange({ period: e.target.value })}>
                    <option value="day">сутки</option><option value="week">неделю</option><option value="month">месяц</option><option value="year">год</option>
                  </select>
                </Field>
              )}
            </>
          )}
        </div>
      )}
      {mixed && (
        <Field label="Как отбирать материалы" hint="Проект смешанный: по умолчанию Telegram даёт новостные сюжеты, остальные коннекторы — подборочный контент.">
          <select className="field" value={options.mode} onChange={(e) => onChange({ mode: e.target.value as Options["mode"] })}>
            <option value="">По умолчанию для типа</option>
            <option value="news">Как новости (сюжеты из нескольких источников)</option>
            <option value="curated">Как подборку (каждый материал — кандидат)</option>
          </select>
        </Field>
      )}
    </>
  );
}

function ImportBox({ projectId }: { projectId: number }) {
  const toast = useToast();
  const qc = useQueryClient();
  const ref = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);

  async function onFile(file: File) {
    setBusy(true);
    try {
      const raw = (await file.text()).trim();
      const items: unknown[] = raw.startsWith("[") ? JSON.parse(raw) : raw.split("\n").filter(Boolean).map((l) => JSON.parse(l));
      let inserted = 0;
      for (let i = 0; i < items.length; i += 500) {
        const r = await api<{ inserted: number }>("/items/import", { method: "POST", body: { project_id: projectId, items: items.slice(i, i + 500) } });
        inserted += r.inserted;
      }
      toast(`Импортировано ${inserted} из ${items.length}. Сюжеты появятся через несколько секунд`, "info");
      qc.invalidateQueries({ queryKey: ["sources"] });
    } catch (e) {
      toast(e instanceof Error ? `Не удалось импортировать: ${e.message}` : "Не удалось прочитать файл", "bad");
    } finally {
      setBusy(false);
      if (ref.current) ref.current.value = "";
    }
  }

  return (
    <section className="mt-12 max-w-[72ch]">
      <h2 className="text-lg font-semibold mb-1">Импорт материалов из файла</h2>
      <p className="text-muted text-[14px] mb-3">
        Для тестов и ручной загрузки. JSONL или JSON-массив, в каждой записи: <code className="text-[13px]">title</code>, <code className="text-[13px]">text</code>, необязательно <code className="text-[13px]">source</code>, <code className="text-[13px]">url</code>, <code className="text-[13px]">authority</code> и время публикации — <code className="text-[13px]">published_at</code> (ISO) или <code className="text-[13px]">hours_ago</code>.
      </p>
      <input ref={ref} type="file" accept=".jsonl,.json,application/json" hidden onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])} />
      <button className="btn" disabled={busy} onClick={() => ref.current?.click()}><Upload size={16} />{busy ? "Загружаем…" : "Выбрать файл"}</button>
    </section>
  );
}
