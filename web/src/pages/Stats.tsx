import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CornerDownRight, ExternalLink, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { ago, dateTime, fmtNum } from "../lib/format";
import { NoProject } from "../components/NoProject";
import { Badge, Empty, ErrorNote, PageHeader, Spinner } from "../components/ui";

interface StatPost {
  id: number; draft_id: number; title: string; channel_name: string; url: string | null; sent_at: string;
  views: number | null; reactions: number | null; stats_at: string | null; relative: number | null; sources: string[]; reply_to_draft_id: number | null;
}
interface StatSource { id: number; name: string; type: string; authority: number; performance: number | null; performance_posts: number; effective_authority: number }
interface StatsData { posts: StatPost[]; sources: StatSource[]; collected_at: string | null; settled_hours: number }

const median = (xs: number[]) => {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
};

/** Во сколько раз пост набрал больше медианы канала: ×1.5 — заметно лучше обычного, ×0.6 — хуже. */
function Relative({ value }: { value: number | null }) {
  if (value == null) return <span className="text-muted" title="Пост моложе двух суток или просмотров ещё нет: сравнивать рано">—</span>;
  const tone = value >= 1.25 ? "ok" : value <= 0.75 ? "warn" : "muted";
  return <Badge tone={tone}>×{value.toFixed(value >= 10 ? 0 : 1)}</Badge>;
}

export default function Stats() {
  const { project, loading } = useProject();
  const { can } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["stats", project?.id],
    queryFn: () => api<StatsData>("/stats", { params: { project_id: project!.id } }),
    enabled: !!project,
  });
  const refresh = useMutation({
    mutationFn: () => api<{ queued: number }>("/stats/refresh", { method: "POST", params: { project_id: project!.id } }),
    onSuccess: (r) => {
      toast(r.queued ? "Сбор просмотров запущен — обновите страницу через минуту" : "У проекта нет включённых Telegram-каналов");
      setTimeout(() => qc.invalidateQueries({ queryKey: ["stats"] }), 20000);
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  if (loading) return <Spinner />;
  if (!project) return <NoProject />;
  const d = q.data;
  const counted = d?.posts.filter((p) => p.views != null) ?? [];
  const settled = d?.posts.filter((p) => p.relative != null) ?? [];

  return (
    <div className="fadein">
      <PageHeader
        title="Статистика"
        sub={<>Просмотры и реакции постов за 30 дней из веб-превью публичных Telegram-каналов — без бота и без языковой модели. Собираются раз в три часа. Посты сравниваются с медианой своего канала, когда им больше {d?.settled_hours ?? 48} часов: раньше просмотры ещё растут.</>}
        actions={can("editor") && <button className="btn" onClick={() => refresh.mutate()} disabled={refresh.isPending}><RefreshCw size={15} />Обновить просмотры</button>}
      />
      {q.isLoading && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {d && d.posts.length === 0 && <Empty title="Опубликованных постов пока нет" text="Статистика появится, когда посты выйдут в публичный Telegram-канал." />}
      {d && d.posts.length > 0 && (
        <>
          <dl className="grid grid-cols-2 sm:grid-cols-4 gap-x-8 gap-y-4 mb-8">
            <div><dt className="label mb-0">Постов</dt><dd className="num text-2xl font-semibold">{d.posts.length}</dd></div>
            <div><dt className="label mb-0">Медиана просмотров</dt><dd className="num text-2xl font-semibold">{counted.length ? fmtNum(Math.round(median(counted.map((p) => p.views!)))) : "—"}</dd></div>
            <div><dt className="label mb-0">Реакций всего</dt><dd className="num text-2xl font-semibold">{fmtNum(counted.reduce((s, p) => s + (p.reactions ?? 0), 0))}</dd></div>
            <div><dt className="label mb-0">Обновлено</dt><dd className="text-[15px] mt-1.5">{d.collected_at ? ago(d.collected_at) : "ещё не собиралось"}</dd></div>
          </dl>

          <section className="mb-10">
            <h2 className="text-xl font-semibold mb-1">Вес источников</h2>
            <p className="hint mb-2 max-w-[76ch]">
              Медиана того, как заходят посты источника, относительно медианы канала (от ×0.5 до ×1.5, нужно хотя бы 3 поста старше двух суток).
              Этот множитель применяется к авторитетности при оценке сюжетов; значение, заданное вами на странице «Источники», не меняется.
            </p>
            {d.sources.length === 0 ? (
              <p className="text-muted py-3">Пока мало данных: {settled.length} {settled.length === 1 ? "пост старше двух суток" : "постов старше двух суток"}.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-[14px]">
                  <thead><tr className="rule-b text-left text-muted text-[12.5px]"><th className="py-2 pr-4 font-medium">Источник</th><th className="py-2 pr-4 font-medium">Постов</th><th className="py-2 pr-4 font-medium">Как заходят</th><th className="py-2 font-medium">Авторитетность</th></tr></thead>
                  <tbody>
                    {d.sources.map((s) => (
                      <tr key={s.id} className="rule-b">
                        <td className="py-2.5 pr-4">{s.name}</td>
                        <td className="py-2.5 pr-4 num">{s.performance_posts}</td>
                        <td className="py-2.5 pr-4">{s.performance == null ? <span className="text-muted" title="Меньше трёх постов старше двух суток">мало данных</span> : <Relative value={s.performance} />}</td>
                        <td className="py-2.5 num">{s.authority.toFixed(2)}{s.performance != null && <span className="text-muted"> → {s.effective_authority.toFixed(2)}</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section>
            <h2 className="text-xl font-semibold mb-1">Посты</h2>
            <ul>
              {d.posts.map((p) => (
                <li key={p.id} className="rule-b py-3 grid md:grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-1.5 items-center">
                  <div className="min-w-0">
                    <Link to={`/drafts/${p.draft_id}`} className="block font-serif text-[16px] font-medium leading-snug hover:underline truncate">
                      {p.reply_to_draft_id && <CornerDownRight size={14} className="inline mr-1 -mt-0.5 text-muted" aria-label="Ответ на прежний пост" />}
                      {p.title || "Без заголовка"}
                    </Link>
                    <div className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[13px] text-muted mt-0.5">
                      <span>{p.channel_name}</span>
                      <span>{dateTime(p.sent_at)}</span>
                      {p.sources.length > 0 && <span className="truncate max-w-[420px]">{p.sources.join(", ")}</span>}
                      {p.url && <a href={p.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:underline"><ExternalLink size={12} />в канале</a>}
                    </div>
                  </div>
                  <div className="flex items-center gap-5 text-[14px]">
                    <span className="num" title="Просмотры">{p.views == null ? "—" : fmtNum(p.views)} <span className="text-muted text-[12.5px]">просм.</span></span>
                    <span className="num" title="Реакции">{p.reactions == null ? "—" : fmtNum(p.reactions)} <span className="text-muted text-[12.5px]">реакц.</span></span>
                    <Relative value={p.relative} />
                  </div>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}
