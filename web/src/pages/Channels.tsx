import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Play, Plus, Send, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { dateTime } from "../lib/format";
import { CHANNEL_TYPES } from "../lib/labels";
import type { Channel, Schedule } from "../lib/types";
import { NoProject } from "../components/NoProject";
import { Badge, Dialog, ErrorNote, Field, PageHeader, Spinner, Toggle } from "../components/ui";

type ChType = Channel["type"];
interface ChForm { id?: number; type: ChType; name: string; chat_id: string; token_env: string; url: string; secret: string; enabled: boolean; hasSecret?: boolean }
const BLANK_CH: ChForm = { type: "telegram", name: "", chat_id: "", token_env: "", url: "", secret: "", enabled: true };

interface ScForm { id?: number; name: string; cron: string; tz: string; kind: "post" | "digest"; top_n: number; enabled: boolean }
const BLANK_SC: ScForm = { name: "", cron: "0 9 * * *", tz: "Europe/Moscow", kind: "digest", top_n: 5, enabled: true };
const PRESETS = [
  ["0 9 * * *", "Каждый день в 09:00"],
  ["0 9 * * 1-5", "По будням в 09:00"],
  ["0 9,18 * * *", "Дважды в день: 09:00 и 18:00"],
  ["0 */3 * * *", "Каждые 3 часа"],
  ["0 * * * *", "Каждый час"],
] as const;

export default function Channels() {
  const { project, loading } = useProject();
  const { can } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const [ch, setCh] = useState<ChForm | null>(null);
  const [sc, setSc] = useState<ScForm | null>(null);
  const pid = project?.id;
  const channels = useQuery({ queryKey: ["channels", pid], queryFn: () => api<Channel[]>("/channels", { params: { project_id: pid } }), enabled: !!pid });
  const schedules = useQuery({ queryKey: ["schedules", pid], queryFn: () => api<Schedule[]>("/schedules", { params: { project_id: pid } }), enabled: !!pid });
  const fail = (e: Error) => toast(e.message, "bad");

  const saveCh = useMutation({
    mutationFn: (f: ChForm) => {
      const config: Record<string, string> = f.type === "telegram" ? { chat_id: f.chat_id, ...(f.token_env ? { token_env: f.token_env } : {}) } : f.type === "webhook" ? { url: f.url, ...(f.secret ? { secret: f.secret } : {}) } : {};
      const body = { project_id: pid, type: f.type, name: f.name, config, enabled: f.enabled };
      return f.id ? api(`/channels/${f.id}`, { method: "PUT", body }) : api("/channels", { method: "POST", body });
    },
    onSuccess: () => { toast("Канал сохранён"); setCh(null); qc.invalidateQueries({ queryKey: ["channels"] }); },
    onError: fail,
  });
  const delCh = useMutation({
    mutationFn: (id: number) => api(`/channels/${id}`, { method: "DELETE" }),
    onSuccess: () => { toast("Канал удалён"); qc.invalidateQueries({ queryKey: ["channels"] }); },
    onError: fail,
  });
  const testCh = useMutation({
    mutationFn: (id: number) => api<{ ok: boolean; message: string }>(`/channels/${id}/test`, { method: "POST" }),
    onSuccess: (r) => toast(r.message, r.ok ? "ok" : "bad"),
    onError: fail,
  });
  const saveSc = useMutation({
    mutationFn: (f: ScForm) => {
      const body = { project_id: pid, name: f.name, cron: f.cron, tz: f.tz, kind: f.kind, top_n: f.top_n, enabled: f.enabled };
      return f.id ? api(`/schedules/${f.id}`, { method: "PUT", body }) : api("/schedules", { method: "POST", body });
    },
    onSuccess: () => { toast("Расписание сохранено"); setSc(null); qc.invalidateQueries({ queryKey: ["schedules"] }); qc.invalidateQueries({ queryKey: ["dashboard"] }); },
    onError: fail,
  });
  const delSc = useMutation({
    mutationFn: (id: number) => api(`/schedules/${id}`, { method: "DELETE" }),
    onSuccess: () => { toast("Расписание удалено"); qc.invalidateQueries({ queryKey: ["schedules"] }); },
    onError: fail,
  });
  const runSc = useMutation({
    mutationFn: (id: number) => api(`/schedules/${id}/run`, { method: "POST" }),
    onSuccess: () => {
      toast("Запуск поставлен в очередь постов", "info");
      qc.invalidateQueries({ queryKey: ["generation-queue"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: fail,
  });

  if (loading) return <Spinner />;
  if (!project) return <NoProject />;
  const patchCh = (p: Partial<ChForm>) => setCh((f) => (f ? { ...f, ...p } : f));
  const patchSc = (p: Partial<ScForm>) => setSc((f) => (f ? { ...f, ...p } : f));
  const chValid = !!ch && !!ch.name && (ch.type !== "telegram" || !!ch.chat_id) && (ch.type !== "webhook" || !!ch.url);

  return (
    <div className="fadein">
      <PageHeader title="Каналы и расписание" sub="Куда отправлять утверждённые посты и когда автоматически собирать дайджест." />

      <section className="mb-14">
        <div className="flex items-center justify-between mb-1">
          <h2 className="text-xl font-semibold">Каналы публикации</h2>
          {can("editor") && <button className="btn btn-primary btn-sm" onClick={() => setCh(BLANK_CH)}><Plus size={15} />Добавить канал</button>}
        </div>
        <p className="hint mb-2 max-w-[70ch]">«Проверить доступ» ничего не публикует: для Telegram запрашивается только информация о боте и его правах в канале.</p>
        {channels.isLoading && <Spinner />}
        {channels.error && <ErrorNote error={channels.error} />}
        {channels.data?.length === 0 && <p className="text-muted py-5">Каналов нет — утвердить черновик будет некуда. Начните с «Консоли»: она просто пишет пост в журнал воркера.</p>}
        <ul>
          {channels.data?.map((c) => (
            <li key={c.id} className="rule-b py-3.5 grid md:grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-2 items-center">
              <div className="min-w-0">
                <span className="font-serif text-[17px] font-medium">{c.name}</span>
                <div className="flex flex-wrap items-center gap-x-4 text-[13px] text-muted">
                  <span>{CHANNEL_TYPES[c.type]}</span>
                  {c.type === "telegram" && <span className="num">{c.config.chat_id}</span>}
                  {c.type === "webhook" && <span className="truncate max-w-[320px]">{c.config.url}</span>}
                  <Badge tone={c.enabled ? "ok" : "muted"}>{c.enabled ? "Включён" : "Выключен"}</Badge>
                </div>
              </div>
              {can("editor") && (
                <div className="flex gap-1.5">
                  <button className="btn btn-sm" onClick={() => testCh.mutate(c.id)} disabled={testCh.isPending}><Send size={14} />Проверить доступ</button>
                  <button className="btn btn-sm btn-ghost" aria-label="Изменить" onClick={() => setCh({ id: c.id, type: c.type, name: c.name, chat_id: c.config.chat_id ?? "", token_env: c.config.token_env ?? "", url: c.config.url ?? "", secret: "", enabled: c.enabled, hasSecret: c.has_secret })}><Pencil size={15} /></button>
                  <button className="btn btn-sm btn-ghost btn-danger" aria-label="Удалить" onClick={() => confirm(`Удалить канал «${c.name}»?`) && delCh.mutate(c.id)}><Trash2 size={15} /></button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <div className="flex items-center justify-between mb-1">
          <h2 className="text-xl font-semibold">Расписание</h2>
          {can("editor") && <button className="btn btn-primary btn-sm" onClick={() => setSc(BLANK_SC)}><Plus size={15} />Добавить запуск</button>}
        </div>
        <p className="hint mb-2 max-w-[70ch]">Каждое срабатывание запускает ровно это расписание: дайджест из выбранного числа сюжетов или указанное число отдельных постов. Режим публикации проекта определяет, останутся ли они на проверке или будут отправлены автоматически.</p>
        {schedules.isLoading && <Spinner />}
        {schedules.data?.length === 0 && <p className="text-muted py-5">Расписаний нет — черновики создаются только вручную.</p>}
        <ul>
          {schedules.data?.map((s) => (
            <li key={s.id} className="rule-b py-3.5 grid md:grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-2 items-center">
              <div className="min-w-0">
                <span className="font-serif text-[17px] font-medium">{s.name}</span>
                <div className="flex flex-wrap items-center gap-x-4 text-[13px] text-muted">
                  <span>{s.kind === "digest" ? `дайджест из ${s.top_n} сюжетов` : `${s.top_n} отдельных постов`}</span>
                  <code className="text-[12.5px]">{s.cron}</code><span>{s.tz}</span>
                  {s.enabled ? <span>следующий запуск: {dateTime(s.next_fire)}</span> : <Badge tone="muted">Выключено</Badge>}
                </div>
              </div>
              {can("editor") && (
                <div className="flex gap-1.5">
                  <button className="btn btn-sm" onClick={() => runSc.mutate(s.id)} disabled={runSc.isPending}><Play size={14} />Запустить сейчас</button>
                  <button className="btn btn-sm btn-ghost" aria-label="Изменить" onClick={() => setSc({ id: s.id, name: s.name, cron: s.cron, tz: s.tz, kind: s.kind, top_n: s.top_n, enabled: s.enabled })}><Pencil size={15} /></button>
                  <button className="btn btn-sm btn-ghost btn-danger" aria-label="Удалить" onClick={() => confirm(`Удалить расписание «${s.name}»?`) && delSc.mutate(s.id)}><Trash2 size={15} /></button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </section>

      <Dialog
        open={!!ch} onClose={() => setCh(null)} title={ch?.id ? "Изменить канал" : "Новый канал"}
        footer={<><button className="btn" onClick={() => setCh(null)}>Отмена</button><button className="btn btn-primary" disabled={!chValid || saveCh.isPending} onClick={() => ch && saveCh.mutate(ch)}>Сохранить</button></>}
      >
        {ch && (
          <>
            <Field label="Тип">
              <select className="field" value={ch.type} disabled={!!ch.id} onChange={(e) => patchCh({ type: e.target.value as ChType })}>
                {Object.entries(CHANNEL_TYPES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </Field>
            <Field label="Название"><input className="field" value={ch.name} onChange={(e) => patchCh({ name: e.target.value })} placeholder="Например, Основной канал" autoFocus /></Field>
            {ch.type === "telegram" && (
              <>
                <Field label="Чат или канал" hint="@имя_канала или числовой id (-100…). Бот должен быть добавлен в канал как администратор с правом публикации.">
                  <input className="field" value={ch.chat_id} onChange={(e) => patchCh({ chat_id: e.target.value })} placeholder="@my_channel" />
                </Field>
                <Field label="Переменная окружения с токеном" hint="Необязательно. По умолчанию TELEGRAM_BOT_TOKEN. Сам токен в интерфейсе не хранится и не показывается.">
                  <input className="field" value={ch.token_env} onChange={(e) => patchCh({ token_env: e.target.value.toUpperCase() })} placeholder="TELEGRAM_BOT_TOKEN" />
                </Field>
              </>
            )}
            {ch.type === "webhook" && (
              <>
                <Field label="Адрес" hint="Pulse отправит POST с JSON. Заголовок Idempotency-Key позволяет получателю отбрасывать повторы.">
                  <input className="field" value={ch.url} onChange={(e) => patchCh({ url: e.target.value })} placeholder="https://example.com/hook" inputMode="url" />
                </Field>
                <Field label="Секрет для подписи" hint={ch.hasSecret ? "Секрет задан. Оставьте пустым, чтобы не менять. Подпись — HMAC-SHA256 в заголовке X-Pulse-Signature." : "Необязательно. Подпись — HMAC-SHA256 в заголовке X-Pulse-Signature."}>
                  <input className="field" type="password" autoComplete="off" value={ch.secret} onChange={(e) => patchCh({ secret: e.target.value })} />
                </Field>
              </>
            )}
            {ch.type === "console" && <p className="hint">Пост будет записан в журнал воркера. Удобно, чтобы проверить весь путь до настройки Telegram.</p>}
            <Toggle checked={ch.enabled} onChange={(v) => patchCh({ enabled: v })} label="Канал включён" />
          </>
        )}
      </Dialog>

      <Dialog
        open={!!sc} onClose={() => setSc(null)} title={sc?.id ? "Изменить расписание" : "Новое расписание"}
        footer={<><button className="btn" onClick={() => setSc(null)}>Отмена</button><button className="btn btn-primary" disabled={!sc?.name || !sc?.cron || saveSc.isPending} onClick={() => sc && saveSc.mutate(sc)}>Сохранить</button></>}
      >
        {sc && (
          <>
            <Field label="Название"><input className="field" value={sc.name} onChange={(e) => patchSc({ name: e.target.value })} placeholder="Утренний дайджест" autoFocus /></Field>
            <Field label="Когда">
              <select className="field mb-2" value={PRESETS.some((p) => p[0] === sc.cron) ? sc.cron : ""} onChange={(e) => e.target.value && patchSc({ cron: e.target.value })}>
                {PRESETS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                <option value="">Своё выражение…</option>
              </select>
              <input className="field num" value={sc.cron} onChange={(e) => patchSc({ cron: e.target.value })} aria-label="Cron-выражение" />
              <span className="hint block mt-1">Формат cron из 5 полей: минута час день месяц день_недели.</span>
            </Field>
            <div className="grid sm:grid-cols-3 gap-4">
              <Field label="Часовой пояс"><input className="field" value={sc.tz} onChange={(e) => patchSc({ tz: e.target.value })} /></Field>
              <Field label="Формат">
                <select className="field" value={sc.kind} onChange={(e) => patchSc({ kind: e.target.value as ScForm["kind"] })}><option value="digest">Один дайджест</option><option value="post">Отдельные посты</option></select>
              </Field>
              <Field label="Сколько сюжетов"><input className="field num" type="number" min={1} max={10} value={sc.top_n} onChange={(e) => patchSc({ top_n: Math.max(1, Math.min(10, Number(e.target.value) || 1)) })} /></Field>
            </div>
            <Toggle checked={sc.enabled} onChange={(v) => patchSc({ enabled: v })} label="Расписание включено" />
          </>
        )}
      </Dialog>
    </div>
  );
}
