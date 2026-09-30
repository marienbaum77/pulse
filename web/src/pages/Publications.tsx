import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { ago, dateTime } from "../lib/format";
import { PUB_STATUS } from "../lib/labels";
import type { Publication } from "../lib/types";
import { NoProject } from "../components/NoProject";
import { Badge, Empty, ErrorNote, PageHeader, Spinner, Tabs } from "../components/ui";

const TABS = [
  { id: "all", label: "Все", match: () => true },
  { id: "attention", label: "Требуют решения", match: (p: Publication) => p.status === "unknown" || p.status === "failed" },
  { id: "queue", label: "В очереди", match: (p: Publication) => p.status === "pending" || p.status === "sending" },
  { id: "sent", label: "Отправлены", match: (p: Publication) => p.status === "sent" },
] as const;

export default function Publications() {
  const { project, loading } = useProject();
  const { can } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("all");
  const q = useQuery({
    queryKey: ["publications", project?.id],
    queryFn: () => api<Publication[]>("/publications", { params: { project_id: project!.id, limit: 200 } }),
    enabled: !!project,
  });
  const resolve = useMutation({
    mutationFn: (v: { id: number; action: "retry" | "mark_sent" | "cancel" }) => api(`/publications/${v.id}/resolve`, { method: "POST", body: { action: v.action } }),
    onSuccess: (_, v) => {
      toast(v.action === "retry" ? "Отправка повторена — публикация в очереди" : v.action === "mark_sent" ? "Отмечено как отправленное" : "Публикация отменена");
      qc.invalidateQueries({ queryKey: ["publications"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  if (loading) return <Spinner />;
  if (!project) return <NoProject />;
  const all = q.data ?? [];
  const def = TABS.find((t) => t.id === tab)!;
  const rows = all.filter(def.match);
  const hasUnknown = all.some((p) => p.status === "unknown");

  return (
    <div className="fadein">
      <PageHeader title="Публикации" sub="Что и куда отправлено. Каждая пара «черновик — канал» отправляется ровно один раз." />
      {hasUnknown && (
        <div className="mb-6 px-4 py-3.5 text-[14px] leading-relaxed max-w-[76ch]" style={{ border: "1px solid var(--warn)", borderRadius: 3 }}>
          <b>Исход неизвестен.</b> Запрос ушёл, но ответа мы не получили (таймаут или сбой сервера) — пост мог дойти до канала. Автоматический повтор мог бы создать дубль, поэтому решение за вами:
          откройте канал, и если поста там нет — повторите отправку, если есть — отметьте как отправленное.
        </div>
      )}
      <Tabs className="mb-1" tabs={TABS.map((t) => ({ id: t.id, label: t.label, count: all.filter(t.match).length }))} value={tab} onChange={setTab} />
      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data && rows.length === 0 && <Empty title="Здесь пока пусто" text="Публикации появляются после утверждения черновика." />}
      <ul>
        {rows.map((p) => (
          <li key={p.id} className="rule-b py-3.5 grid md:grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-2 items-center">
            <div className="min-w-0">
              <Link to={`/drafts/${p.draft_id}`} className="block font-serif text-[16.5px] font-medium leading-snug hover:underline truncate">{p.draft_title || "Без заголовка"}</Link>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[13px] text-muted mt-0.5">
                <span>{p.channel_name}</span>
                <Badge tone={PUB_STATUS[p.status].tone}>{PUB_STATUS[p.status].label}</Badge>
                <span>{p.status === "sent" ? dateTime(p.sent_at) : p.status === "pending" ? `запланировано ${ago(p.due_at)}` : `попыток: ${p.attempts}`}</span>
                {p.external_id && <span className="num">id {p.external_id}</span>}
              </div>
              {p.error && <p className="text-[13px] mt-1" style={{ color: p.status === "sent" ? "var(--muted)" : "var(--bad)" }}>{p.error}</p>}
            </div>
            {can("editor") && (p.status === "unknown" || p.status === "failed" || p.status === "pending") && (
              <div className="flex flex-wrap gap-2">
                {(p.status === "unknown" || p.status === "failed") && <button className="btn btn-sm" onClick={() => resolve.mutate({ id: p.id, action: "retry" })} disabled={resolve.isPending}>Повторить отправку</button>}
                {(p.status === "unknown" || p.status === "failed") && (
                  <button className="btn btn-sm" disabled={resolve.isPending} onClick={() => confirm("Пост действительно есть в канале?") && resolve.mutate({ id: p.id, action: "mark_sent" })}>Пост уже в канале</button>
                )}
                <button className="btn btn-sm btn-danger" onClick={() => resolve.mutate({ id: p.id, action: "cancel" })} disabled={resolve.isPending}>Отменить</button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
