import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useToast } from "../lib/context";
import { ago, dateTime } from "../lib/format";
import type { AuditEntry, Health, Role, SystemConfig, User } from "../lib/types";
import { Badge, Dialog, ErrorNote, Field, PageHeader, Spinner, Tabs } from "../components/ui";

const TABS = [["users", "Пользователи"], ["health", "Состояние"], ["audit", "Журнал действий"]] as const;
const ROLES: Record<Role, string> = { admin: "Администратор", editor: "Редактор", viewer: "Наблюдатель" };
const ROLE_HINT: Record<Role, string> = { admin: "всё, включая пользователей и журнал", editor: "источники, каналы, черновики, публикация", viewer: "только просмотр" };

export default function Admin() {
  const { can, user } = useAuth();
  const [tab, setTab] = useState<(typeof TABS)[number][0]>("users");
  if (!can("admin")) return <Navigate to="/" replace />;
  return (
    <div className="fadein">
      <PageHeader title="Система" sub="Пользователи и роли, состояние сервисов и журнал изменений." />
      <Tabs className="mb-6" tabs={TABS.map(([id, label]) => ({ id, label }))} value={tab} onChange={setTab} />
      {tab === "users" && <Users me={user!.id} />}
      {tab === "health" && <HealthPanel />}
      {tab === "audit" && <Audit />}
    </div>
  );
}

function Users({ me }: { me: number }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/users") });
  const [add, setAdd] = useState<{ email: string; password: string; role: Role } | null>(null);
  const [pw, setPw] = useState<{ id: number; email: string; password: string } | null>(null);
  const done = () => qc.invalidateQueries({ queryKey: ["users"] });
  const fail = (e: Error) => toast(e.message, "bad");
  const create = useMutation({ mutationFn: () => api("/users", { method: "POST", body: add }), onSuccess: () => { toast("Пользователь создан"); setAdd(null); done(); }, onError: fail });
  const role = useMutation({ mutationFn: (v: { id: number; role: Role }) => api(`/users/${v.id}`, { method: "PATCH", body: { role: v.role } }), onSuccess: () => { toast("Роль изменена"); done(); }, onError: fail });
  const reset = useMutation({ mutationFn: () => api(`/users/${pw!.id}`, { method: "PATCH", body: { password: pw!.password } }), onSuccess: () => { toast("Пароль изменён"); setPw(null); }, onError: fail });
  const del = useMutation({ mutationFn: (id: number) => api(`/users/${id}`, { method: "DELETE" }), onSuccess: () => { toast("Пользователь удалён"); done(); }, onError: fail });

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <p className="hint max-w-[64ch]">Роли: {Object.entries(ROLE_HINT).map(([k, v]) => `${ROLES[k as Role].toLowerCase()} — ${v}`).join("; ")}.</p>
        <button className="btn btn-primary btn-sm shrink-0" onClick={() => setAdd({ email: "", password: "", role: "editor" })}><Plus size={15} />Добавить</button>
      </div>
      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      <ul>
        {q.data?.map((u) => (
          <li key={u.id} className="rule-b py-3 grid sm:grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-2 items-center">
            <div className="min-w-0"><span className="font-medium">{u.email}</span>{u.id === me && <span className="eyebrow ml-2">это вы</span>}<span className="block text-[13px] text-muted">с {dateTime(u.created_at)}</span></div>
            <div className="flex items-center gap-2">
              <select className="field !w-auto h-8 text-[13px]" value={u.role} onChange={(e) => role.mutate({ id: u.id, role: e.target.value as Role })} aria-label={`Роль ${u.email}`}>
                {Object.entries(ROLES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
              <button className="btn btn-sm btn-ghost" aria-label="Сменить пароль" onClick={() => setPw({ id: u.id, email: u.email, password: "" })}><KeyRound size={15} /></button>
              <button className="btn btn-sm btn-ghost btn-danger" aria-label="Удалить" disabled={u.id === me} onClick={() => confirm(`Удалить пользователя ${u.email}?`) && del.mutate(u.id)}><Trash2 size={15} /></button>
            </div>
          </li>
        ))}
      </ul>
      <Dialog open={!!add} onClose={() => setAdd(null)} title="Новый пользователь"
        footer={<><button className="btn" onClick={() => setAdd(null)}>Отмена</button><button className="btn btn-primary" disabled={!add?.email || (add?.password.length ?? 0) < 8 || create.isPending} onClick={() => create.mutate()}>Создать</button></>}>
        {add && (<>
          <Field label="Почта"><input className="field" type="email" value={add.email} onChange={(e) => setAdd({ ...add, email: e.target.value })} autoFocus /></Field>
          <Field label="Пароль" hint="Не короче 8 символов. Передайте его пользователю безопасным способом."><input className="field" type="password" autoComplete="new-password" value={add.password} onChange={(e) => setAdd({ ...add, password: e.target.value })} /></Field>
          <Field label="Роль"><select className="field" value={add.role} onChange={(e) => setAdd({ ...add, role: e.target.value as Role })}>{Object.entries(ROLES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
        </>)}
      </Dialog>
      <Dialog open={!!pw} onClose={() => setPw(null)} title={`Новый пароль для ${pw?.email ?? ""}`}
        footer={<><button className="btn" onClick={() => setPw(null)}>Отмена</button><button className="btn btn-primary" disabled={(pw?.password.length ?? 0) < 8 || reset.isPending} onClick={() => reset.mutate()}>Сменить</button></>}>
        {pw && <Field label="Пароль" hint="Не короче 8 символов."><input className="field" type="password" autoComplete="new-password" value={pw.password} onChange={(e) => setPw({ ...pw, password: e.target.value })} autoFocus /></Field>}
      </Dialog>
    </section>
  );
}

function HealthPanel() {
  const toast = useToast();
  const qc = useQueryClient();
  const [deep, setDeep] = useState(false);
  const [chatModel, setChatModel] = useState("");
  const [embedModel, setEmbedModel] = useState("");
  const [pullName, setPullName] = useState("");
  const h = useQuery({ queryKey: ["health", deep], queryFn: () => api<Health>("/system/health", { params: { deep } }), refetchInterval: 15_000 });
  const c = useQuery({ queryKey: ["sysconfig"], queryFn: () => api<SystemConfig>("/system/config") });
  const models = useQuery({
    queryKey: ["sysmodels"],
    queryFn: () => api<{ models: { id: string; kind: "embed" | "chat" }[]; hint?: string }>("/system/models"),
    enabled: c.data?.llm_provider === "openai",
  });
  const seen = h.data?.worker_seen_seconds;

  const saveModels = useMutation({
    mutationFn: () => api("/system/config", { method: "PATCH", body: { llm_model: chatModel || undefined, embed_model: embedModel || undefined } }),
    onSuccess: () => { toast("Модели сохранены — воркер подхватит их на следующей задаче"); qc.invalidateQueries({ queryKey: ["sysconfig"] }); qc.invalidateQueries({ queryKey: ["health"] }); },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const pull = useMutation({
    mutationFn: () => api("/system/models/pull", { method: "POST", body: { name: pullName.trim() } }),
    onSuccess: () => { toast(`Модель «${pullName.trim()}» скачана`, "info"); setPullName(""); qc.invalidateQueries({ queryKey: ["sysmodels"] }); },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  useEffect(() => {
    if (!c.data) return;
    setChatModel((v) => v || c.data.llm_model);
    setEmbedModel((v) => v || c.data.embed_model);
  }, [c.data]);

  return (
    <div className="space-y-10">
      <div className="grid lg:grid-cols-2 gap-x-14 gap-y-10">
        <section>
          <h2 className="text-lg font-semibold mb-2">Сервисы</h2>
          {h.isLoading && <Spinner />}
          {h.data && (
            <dl className="text-[14px]">
              <Line k="База данных"><Badge tone={h.data.db ? "ok" : "bad"}>{h.data.db ? "доступна" : "недоступна"}</Badge></Line>
              <Line k="Воркер">{seen == null ? <Badge tone="bad">не запускался</Badge> : seen < 90 ? <Badge tone="ok">работает, отклик {seen} с назад</Badge> : <Badge tone="bad">молчит {Math.round(seen / 60)} мин</Badge>}</Line>
              <Line k="Провайдер моделей"><span>{h.data.provider === "stub" ? "stub (без языковой модели)" : "OpenAI-совместимый"}</span></Line>
              {h.data.model && (
                <Line k="Проверка модели">
                  {h.data.model.ok ? <span className="text-right"><Badge tone="ok">отвечает</Badge><span className="block text-[12.5px] text-muted num">эмбеддинги: {h.data.model.embed_dim ?? "—"} измерений</span></span> : <span className="text-bad text-right">{h.data.model.error}</span>}
                </Line>
              )}
            </dl>
          )}
          <button className="btn mt-4" onClick={() => setDeep(true)} disabled={h.isFetching}>Проверить связь с моделью</button>
        </section>
        <section>
          <h2 className="text-lg font-semibold mb-2">Конфигурация</h2>
          {c.isLoading && <Spinner />}
          {c.error && <ErrorNote error={c.error} />}
          {c.data && (
            <dl className="text-[14px]">
              <Line k="Провайдер">{c.data.llm_provider}</Line>
              {c.data.llm_base_url && <Line k="Адрес API"><span className="break-all">{c.data.llm_base_url}</span></Line>}
              <Line k="Модель генерации">{c.data.llm_model}</Line>
              <Line k="Модель эмбеддингов">{c.data.embed_model}</Line>
              <Line k="Токен Telegram">{c.data.telegram_token_configured ? <Badge tone="ok">задан</Badge> : <span className="text-muted">не задан в окружении</span>}</Line>
              <Line k="Внутренние адреса в источниках">{c.data.allow_private_urls ? <Badge tone="warn">разрешены</Badge> : <span className="text-muted">запрещены</span>}</Line>
              <Line k="Хранение материалов">{c.data.retention_days} дн.</Line>
              <Line k="Параллельных задач">{c.data.worker_concurrency}</Line>
            </dl>
          )}
          <p className="hint mt-3">Адрес API и провайдер задаются в .env. Модели ниже можно менять без перезапуска.</p>
        </section>
      </div>

      {c.data?.llm_provider === "openai" && (
        <ModelPicker
          models={models.data?.models ?? []}
          modelsHint={models.data?.hint}
          modelsLoading={models.isLoading}
          chatModel={chatModel}
          embedModel={embedModel}
          pullName={pullName}
          setChatModel={setChatModel}
          setEmbedModel={setEmbedModel}
          setPullName={setPullName}
          onSave={() => saveModels.mutate()}
          savePending={saveModels.isPending}
          onPull={() => pull.mutate()}
          pullPending={pull.isPending}
          onRefreshModels={() => qc.invalidateQueries({ queryKey: ["sysmodels"] })}
        />
      )}
    </div>
  );
}

function ModelPicker({
  models, modelsHint, modelsLoading, chatModel, embedModel, pullName,
  setChatModel, setEmbedModel, setPullName, onSave, savePending, onPull, pullPending, onRefreshModels,
}: {
  models: { id: string; kind: "embed" | "chat" }[];
  modelsHint?: string;
  modelsLoading: boolean;
  chatModel: string; embedModel: string; pullName: string;
  setChatModel: (v: string) => void; setEmbedModel: (v: string) => void; setPullName: (v: string) => void;
  onSave: () => void; savePending: boolean; onPull: () => void; pullPending: boolean; onRefreshModels: () => void;
}) {
  const chatIds = models.filter((m) => m.kind === "chat").map((m) => m.id);
  const embedIds = models.filter((m) => m.kind === "embed").map((m) => m.id);
  const invalidSelection = models.length > 0 && (!chatIds.includes(chatModel) || !embedIds.includes(embedModel));
  const MEMORY: Record<string, string> = {
    "bge-m3": "~1,2 ГБ",
    "qwen2.5:3b-instruct": "~2 ГБ",
    "qwen2.5:7b-instruct": "~5–8 ГБ",
    "llama3.1:8b": "~5–8 ГБ",
  };

  return (
    <section className="max-w-[720px]">
      <h2 className="text-lg font-semibold mb-1">Модели</h2>
      <p className="hint mb-4">Выберите скачанную модель или укажите имя и нажмите «Скачать» (Ollama). Смена применяется сразу, без правки .env.</p>
      <div className="grid sm:grid-cols-2 gap-5 mb-4">
        <Field label="Модель генерации" hint={MEMORY[chatModel] ? `Ориентир по памяти: ${MEMORY[chatModel]}` : "Пишет текст постов; доступны только чат-модели"}>
          {models.length > 0 ? (
            <select className="field" value={chatModel} onChange={(e) => setChatModel(e.target.value)}>
              {!chatIds.includes(chatModel) && chatModel && <option value={chatModel} disabled>{chatModel} (не чат-модель)</option>}
              {chatIds.length === 0 && <option value="" disabled>Нет чат-моделей</option>}
              {chatIds.map((id) => <option key={id} value={id}>{id}{MEMORY[id] ? ` (${MEMORY[id]})` : ""}</option>)}
            </select>
          ) : (
            <input className="field" value={chatModel} onChange={(e) => setChatModel(e.target.value)} placeholder="qwen2.5:7b-instruct" />
          )}
        </Field>
        <Field label="Модель эмбеддингов" hint={MEMORY[embedModel] ? `Ориентир по памяти: ${MEMORY[embedModel]}` : "Нужна для сюжетов; доступны только embedding-модели"}>
          {models.length > 0 ? (
            <select className="field" value={embedModel} onChange={(e) => setEmbedModel(e.target.value)}>
              {!embedIds.includes(embedModel) && embedModel && <option value={embedModel} disabled>{embedModel} (не embedding-модель)</option>}
              {embedIds.length === 0 && <option value="" disabled>Нет embedding-моделей</option>}
              {embedIds.map((id) => <option key={id} value={id}>{id}{MEMORY[id] ? ` (${MEMORY[id]})` : ""}</option>)}
            </select>
          ) : (
            <input className="field" value={embedModel} onChange={(e) => setEmbedModel(e.target.value)} placeholder="bge-m3" />
          )}
        </Field>
      </div>
      <div className="flex flex-wrap gap-2 mb-6">
        <button className="btn btn-primary" disabled={savePending || (!chatModel && !embedModel) || invalidSelection} onClick={onSave}>{savePending ? "Сохраняем…" : "Применить модели"}</button>
        <button className="btn" disabled={modelsLoading} onClick={onRefreshModels}>{modelsLoading ? "Обновляем…" : "Обновить список"}</button>
      </div>
      {modelsHint && <p className="hint mb-3">{modelsHint}</p>}
      <Field label="Скачать модель в Ollama" hint="Например bge-m3 или qwen2.5:3b-instruct. Займёт время и место на диске.">
        <div className="flex gap-2">
          <input className="field" value={pullName} onChange={(e) => setPullName(e.target.value)} placeholder="qwen2.5:3b-instruct" />
          <button className="btn shrink-0" disabled={!pullName.trim() || pullPending} onClick={onPull}>{pullPending ? "Скачиваем…" : "Скачать"}</button>
        </div>
      </Field>
    </section>
  );
}

function Line({ k, children }: { k: string; children: React.ReactNode }) {
  return <div className="rule-b py-2.5 flex items-baseline justify-between gap-4"><dt className="text-muted">{k}</dt><dd className="text-right">{children}</dd></div>;
}

function Audit() {
  const q = useQuery({ queryKey: ["audit"], queryFn: () => api<AuditEntry[]>("/audit", { params: { limit: 200 } }) });
  return (
    <section>
      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data?.length === 0 && <p className="text-muted">Записей пока нет.</p>}
      <ul>
        {q.data?.map((a) => (
          <li key={a.id} className="rule-b py-2.5 grid grid-cols-[150px_minmax(0,1fr)] sm:grid-cols-[150px_220px_minmax(0,1fr)] gap-x-5 gap-y-0.5 text-[13.5px]">
            <span className="num text-muted">{dateTime(a.created_at)}</span>
            <span className="truncate">{a.user_email ?? "система"}</span>
            <span className="col-span-2 sm:col-span-1"><b className="font-medium">{a.action}</b> <span className="text-muted">{a.entity}{a.entity_id ? ` №${a.entity_id}` : ""}</span> <span className="text-muted">({ago(a.created_at)})</span></span>
          </li>
        ))}
      </ul>
    </section>
  );
}
