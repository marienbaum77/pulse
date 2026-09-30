import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileUp, Pencil, Plus, RefreshCw, Trash2, Upload } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { ago, fmtNum } from "../lib/format";
import type { Source } from "../lib/types";
import { NoProject } from "../components/NoProject";
import { Badge, Dialog, Empty, ErrorNote, Field, PageHeader, Spinner, Toggle } from "../components/ui";

interface Form { id?: number; name: string; url: string; authority: number; poll_minutes: number; enabled: boolean }
const BLANK: Form = { name: "", url: "", authority: 0.5, poll_minutes: 30, enabled: true };
const POLL = [[15, "каждые 15 минут"], [30, "каждые 30 минут"], [60, "раз в час"], [180, "раз в 3 часа"], [360, "раз в 6 часов"], [1440, "раз в сутки"]] as const;

export default function Sources() {
  const { project, loading } = useProject();
  const { can } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const [form, setForm] = useState<Form | null>(null);
  const [preview, setPreview] = useState<{ count: number; sample: string[] } | string | null>(null);
  const opmlRef = useRef<HTMLInputElement>(null);
  const [opmlBusy, setOpmlBusy] = useState(false);
  const q = useQuery({ queryKey: ["sources", project?.id], queryFn: () => api<Source[]>("/sources", { params: { project_id: project!.id } }), enabled: !!project });
  const refresh = () => qc.invalidateQueries({ queryKey: ["sources"] });

  const save = useMutation({
    mutationFn: (f: Form) => {
      const body = { project_id: project!.id, type: "rss", name: f.name, url: f.url, authority: f.authority, poll_minutes: f.poll_minutes, enabled: f.enabled };
      return f.id ? api(`/sources/${f.id}`, { method: "PUT", body }) : api("/sources", { method: "POST", body });
    },
    onSuccess: () => { toast("Источник сохранён, первый сбор запущен"); setForm(null); setPreview(null); refresh(); },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api(`/sources/${id}`, { method: "DELETE" }),
    onSuccess: () => { toast("Источник удалён"); refresh(); },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const fetchNow = useMutation({
    mutationFn: (id: number) => api(`/sources/${id}/fetch`, { method: "POST" }),
    onSuccess: () => toast("Сбор поставлен в очередь", "info"),
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const check = useMutation({
    mutationFn: (url: string) => api<{ count: number; sample: string[] }>("/sources/preview", { method: "POST", body: { url } }),
    onSuccess: setPreview,
    onError: (e: Error) => setPreview(e.message),
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

  return (
    <div className="fadein">
      <PageHeader
        title="Источники"
        sub="Откуда Pulse берёт материалы. Авторитетность влияет на вес сюжета: чем она выше, тем сильнее вклад источника."
        actions={can("editor") && (
          <div className="flex flex-wrap gap-2">
            <input ref={opmlRef} type="file" accept=".opml,.xml,application/xml,text/xml" hidden onChange={(e) => e.target.files?.[0] && onOpml(e.target.files[0])} />
            <button className="btn" disabled={opmlBusy} onClick={() => opmlRef.current?.click()}><FileUp size={16} />{opmlBusy ? "Импорт…" : "Импортировать OPML"}</button>
            <button className="btn btn-primary" onClick={() => { setForm(BLANK); setPreview(null); }}><Plus size={16} />Добавить RSS-ленту</button>
          </div>
        )}
      />
      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data && rows.length === 0 && <Empty title="Источников пока нет" text="Добавьте RSS/Atom-ленту или импортируйте список подписок из OPML (Feedly, Inoreader и др.)." />}
      <ul>
        {rows.map((s) => {
          const tone = s.last_error ? "bad" : s.last_ok_at ? "ok" : "muted";
          return (
            <li key={s.id} className="rule-b py-3.5 grid md:grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-2 items-center">
              <div className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-x-3">
                  <span className="font-serif text-[17px] font-medium">{s.name}</span>
                  {s.type === "manual" && <span className="eyebrow">импорт</span>}
                  {!s.enabled && s.type === "rss" && <span className="eyebrow">отключён</span>}
                </div>
                {s.url && <p className="text-[13px] text-muted truncate">{s.url}</p>}
                <div className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[13px] mt-1">
                  {s.type === "rss" && <Badge tone={tone}>{s.last_error ? "Ошибка" : s.last_ok_at ? `Обновлён ${ago(s.last_ok_at)}` : "Ещё не опрашивался"}</Badge>}
                  <span className="text-muted num">{fmtNum(s.item_count)} материалов</span>
                  <span className="text-muted num">авторитетность {s.authority.toFixed(2)}</span>
                  {s.type === "rss" && <span className="text-muted">{POLL.find((p) => p[0] === s.poll_minutes)?.[1] ?? `каждые ${s.poll_minutes} мин`}</span>}
                </div>
                {s.last_error && <p className="text-[13px] text-bad mt-1 break-words">{s.last_error}</p>}
              </div>
              {can("editor") && (
                <div className="flex gap-1.5">
                  {s.type === "rss" && <button className="btn btn-sm" onClick={() => fetchNow.mutate(s.id)} title="Опросить сейчас"><RefreshCw size={14} />Опросить</button>}
                  {s.type === "rss" && <button className="btn btn-sm btn-ghost" aria-label="Изменить" onClick={() => { setForm({ id: s.id, name: s.name, url: s.url, authority: s.authority, poll_minutes: s.poll_minutes, enabled: s.enabled }); setPreview(null); }}><Pencil size={15} /></button>}
                  <button className="btn btn-sm btn-ghost btn-danger" aria-label="Удалить" onClick={() => confirm(`Удалить источник «${s.name}»? Собранные материалы останутся.`) && remove.mutate(s.id)}><Trash2 size={15} /></button>
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {can("editor") && <ImportBox projectId={project.id} />}

      <Dialog
        open={!!form} onClose={() => setForm(null)} title={form?.id ? "Изменить источник" : "Новый RSS-источник"}
        footer={<><button className="btn" onClick={() => setForm(null)}>Отмена</button><button className="btn btn-primary" disabled={!form?.name || !form?.url || save.isPending} onClick={() => form && save.mutate(form)}>{save.isPending ? "Сохраняем…" : "Сохранить"}</button></>}
      >
        {form && (
          <>
            <Field label="Название"><input className="field" value={form.name} onChange={(e) => patch({ name: e.target.value })} placeholder="Например, Хабр: новости" autoFocus /></Field>
            <Field label="Адрес ленты">
              <div className="flex gap-2">
                <input className="field" value={form.url} onChange={(e) => { patch({ url: e.target.value }); setPreview(null); }} placeholder="https://example.com/rss" inputMode="url" />
                <button className="btn shrink-0" disabled={!form.url || check.isPending} onClick={() => check.mutate(form.url)}>{check.isPending ? "Читаем…" : "Проверить"}</button>
              </div>
              {typeof preview === "string" && <span className="block mt-1.5 text-[13px] text-bad">{preview}</span>}
              {preview && typeof preview !== "string" && (
                <span className="block mt-2 text-[13px]"><b>Лента читается:</b> {preview.count} записей. Например:
                  <span className="block text-muted mt-1">{preview.sample.map((t, i) => <span key={i} className="block truncate">— {t}</span>)}</span>
                </span>
              )}
            </Field>
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
