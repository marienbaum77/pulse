import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Sparkles, X } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useToast } from "../lib/context";
import { ago, plural } from "../lib/format";
import type { DiscoveryState, SourceSuggestion, SuggestionType } from "../lib/types";
import { ErrorNote } from "./ui";

const TYPE_LABELS: Record<SuggestionType, string> = { youtube: "YouTube", site: "Сайт", reddit: "Reddit", telegram: "Telegram" };

/** Автоподбор источников по теме проекта: запуск поиска и решения редактора по найденным кандидатам. */
export function DiscoveryPanel({ projectId }: { projectId: number }) {
  const { can } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["discovery", projectId],
    queryFn: () => api<DiscoveryState>("/discovery", { params: { project_id: projectId } }),
    // Пока идёт подбор, опрашиваем чаще: события придут, но страница может быть открыта без живого канала.
    refetchInterval: (query) => (["queued", "running"].includes(query.state.data?.run?.status ?? "") ? 3000 : false),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["discovery"] });
  const start = useMutation({
    mutationFn: () => api<{ queued: boolean }>("/discovery", { method: "POST", body: { project_id: projectId } }),
    onSuccess: ({ queued }) => { toast(queued ? "Подбор запущен — это займёт минуту-две" : "Подбор уже идёт", "info"); refresh(); },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const accept = useMutation({
    mutationFn: (s: SourceSuggestion) => api(`/discovery/suggestions/${s.id}/accept`, { method: "POST", body: {} }),
    onSuccess: (_r, s) => { toast(`«${s.name}» добавлен, первый сбор запущен`); refresh(); qc.invalidateQueries({ queryKey: ["sources"] }); },
    onError: (e: Error) => { toast(e.message, "bad"); refresh(); },
  });
  const dismiss = useMutation({
    mutationFn: (s: SourceSuggestion) => api(`/discovery/suggestions/${s.id}/dismiss`, { method: "POST" }),
    onSuccess: refresh,
    onError: (e: Error) => toast(e.message, "bad"),
  });

  const run = q.data?.run;
  const busy = run?.status === "queued" || run?.status === "running";
  const suggestions = q.data?.suggestions ?? [];

  return (
    <section className="mb-10 max-w-[860px]">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-1">
        <h2 className="text-lg font-semibold">Подбор источников по теме</h2>
        {can("editor") && (
          <button className="btn btn-sm" disabled={busy || start.isPending} onClick={() => start.mutate()}>
            <Sparkles size={14} />{busy ? "Ищем…" : suggestions.length ? "Подобрать ещё" : "Подобрать источники"}
          </button>
        )}
      </div>
      <p className="hint mb-2 max-w-[72ch]">
        Pulse составит поисковые запросы по теме и аспектам проекта, найдёт статьи и видео и предложит площадки, которые встречаются
        в выдаче чаще всего: YouTube-каналы, сайты, сабреддиты и Telegram-каналы. Добавляете только то, что подходит; скрытые предложения не вернутся.
      </p>
      {q.error && <ErrorNote error={q.error} />}
      {run && (
        <p className="text-[13px] text-muted mb-2">
          {run.status === "queued" && "Подбор в очереди…"}
          {run.status === "running" && "Идёт подбор: запросы, поиск, проверка сайтов…"}
          {run.status === "done" && <>Последний подбор {ago(run.finished_at)}: {run.found ?? 0} предложений по запросам «{(run.queries ?? []).join("», «")}».</>}
          {run.status === "failed" && <span className="text-bad">Подбор не удался: {run.error}</span>}
        </p>
      )}
      {suggestions.length > 0 && (
        <ul>
          {suggestions.map((s) => (
            <li key={s.id} className="rule-b py-3 grid md:grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-2 items-center">
              <div className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-x-3">
                  <span className="font-serif text-[17px] font-medium">{s.name}</span>
                  <span className="eyebrow">{TYPE_LABELS[s.type]}</span>
                  <span className="text-[13px] text-muted num">найден по {s.hits} {plural(s.hits, "запросу", "запросам", "запросам")}</span>
                </div>
                {s.reason && <p className="text-[13px] mt-0.5">{s.reason}</p>}
                {s.samples.length > 0 && (
                  <p className="text-[13px] text-muted mt-0.5">{s.samples.map((t, i) => <span key={i} className="block truncate">— {t}</span>)}</p>
                )}
              </div>
              {can("editor") && (
                <div className="flex gap-1.5">
                  <button className="btn btn-sm btn-primary" disabled={accept.isPending} onClick={() => accept.mutate(s)}><Check size={14} />Добавить</button>
                  <button className="btn btn-sm btn-ghost" aria-label="Скрыть" title="Скрыть и больше не предлагать" onClick={() => dismiss.mutate(s)}><X size={15} /></button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {run?.status === "done" && suggestions.length === 0 && (
        <p className="text-[13px] text-muted">Новых предложений нет: подходящие площадки уже подключены или скрыты.</p>
      )}
    </section>
  );
}
