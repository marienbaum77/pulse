import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { SCORE_HINTS, SCORE_LABELS } from "../lib/labels";
import type { Project, ProjectInput } from "../lib/types";
import { Field, PageHeader, Spinner, Toggle } from "../components/ui";

const BLANK: ProjectInput = {
  name: "", topic: "", language: "ru", tone: "нейтральный, информативный", max_length: 900, prompt_template: "",
  generation_mode: "extractive", publish_mode: "review", window_hours: 48, sim_threshold: 0.72, topic_threshold: 0.4,
  weights: { coverage: 0.15, authority: 0.075, freshness: 0.1, velocity: 0.075, topic_fit: 0.6 },
  context_items: 6, context_sentences: 3, min_items: 2, auto_retry_unknown: false, show_sources: true, active: true,
};
const toInput = (p: Project): ProjectInput => { const { id: _id, prompt_version: _v, ...rest } = p; return rest; };

export default function ProjectSettings() {
  const { project, loading, setProjectId } = useProject();
  const { can } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const creating = params.get("new") === "1" || (!loading && !project);
  const [f, setF] = useState<ProjectInput>(BLANK);

  useEffect(() => {
    if (creating) setF(BLANK);
    else if (project) setF(toInput(project));
  }, [creating, project?.id, project?.prompt_version]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation({
    mutationFn: () => (creating ? api<Project>("/projects", { method: "POST", body: f }) : api<Project>(`/projects/${project!.id}`, { method: "PUT", body: f })),
    onSuccess: (p) => {
      toast(creating ? "Проект создан" : "Настройки сохранены");
      qc.invalidateQueries({ queryKey: ["projects"] });
      if (creating) { setProjectId(p.id); setParams({}); nav("/"); }
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const del = useMutation({
    mutationFn: () => api(`/projects/${project!.id}`, { method: "DELETE" }),
    onSuccess: () => { toast("Проект удалён"); qc.invalidateQueries(); nav("/"); },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  if (loading) return <Spinner />;
  const set = <K extends keyof ProjectInput>(k: K, v: ProjectInput[K]) => setF((x) => ({ ...x, [k]: v }));
  const setW = (k: string, v: number) => setF((x) => ({ ...x, weights: { ...x.weights, [k]: v } }));
  const ro = !can("editor");
  const num = (v: string, min: number, max: number, fallback: number) => Math.max(min, Math.min(max, Number(v) || fallback));

  return (
    <div className="fadein max-w-[860px]">
      <PageHeader
        title={creating ? "Новый проект" : "Настройки проекта"}
        sub={<>Тема, правила кластеризации и оценки сюжетов, стиль текста и режим публикации. Изменения действуют на новые материалы и следующие генерации.{!creating && project && <span className="block mt-1 text-[13px]">Номер проекта для скриптов: <b className="num">{project.id}</b></span>}</>}
        actions={!creating && can("editor") && <button className="btn" onClick={() => setParams({ new: "1" })}>Новый проект</button>}
      />
      <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }} className="space-y-12">
        <Section title="Основное">
          <Field label="Название"><input className="field" required value={f.name} onChange={(e) => set("name", e.target.value)} disabled={ro} /></Field>
          <Field label="Тема" hint="Одно-два предложения. Сюжеты, близкие по смыслу к теме, получают больший вес.">
            <textarea className="field" rows={3} value={f.topic} onChange={(e) => set("topic", e.target.value)} disabled={ro} placeholder="Например: базы данных, облачная инфраструктура, ИИ и ИТ-инциденты" />
          </Field>
        </Section>

        <Section title="Генерация текста">
          <div className="grid sm:grid-cols-2 gap-5">
            <Field label="Способ" hint={
              f.generation_mode === "llm" ? "Языковая модель пишет текст по источникам и переводит материалы на язык проекта. Нужен настроенный провайдер с поддержкой чата."
              : f.generation_mode === "auto" ? "Модель синтезирует материалы минимум из двух разных источников. При несовпадении языков она также вызывается для перевода; нужен провайдер с поддержкой чата."
              : "Пост собирается из ведущих предложений без модели. При несовпадении языков модель вызывается для перевода; без провайдера с поддержкой чата черновик останется на проверке."
            }>
              <select className="field" value={f.generation_mode} onChange={(e) => set("generation_mode", e.target.value as ProjectInput["generation_mode"])} disabled={ro}>
                <option value="extractive">Экстрактивный (без модели)</option>
                <option value="auto">Авто (модель только когда нужна)</option>
                <option value="llm">Языковая модель всегда</option>
              </select>
            </Field>
            <Field label="Максимальная длина, символов"><input className="field num" type="number" min={200} max={3500} value={f.max_length} onChange={(e) => set("max_length", num(e.target.value, 200, 3500, 900))} disabled={ro} /></Field>
            <Field label="Язык"><input className="field" value={f.language} onChange={(e) => set("language", e.target.value)} disabled={ro} /></Field>
            <Field label="Тон" hint="Стиль общения модели: нейтральный, деловой, разговорный и т.п. Учитывается при генерации моделью и обязательном переводе."><input className="field" value={f.tone} onChange={(e) => set("tone", e.target.value)} disabled={ro} /></Field>
          </div>
          {(f.generation_mode === "llm" || f.generation_mode === "auto") && (
            <>
              <Field label="Инструкция для модели" hint={<>Пусто — используется стандартная: краткий заголовок одним предложением, продолжение — в тексте, ссылки [1], [2] только в тексте; пост пишется на заданном языке. Можно подставлять <code>{"{language}"}</code>, <code>{"{tone}"}</code>, <code>{"{topic}"}</code>, <code>{"{max_length}"}</code>. Каждое изменение увеличивает версию промпта, она сохраняется в черновиках.</>}>
                <textarea className="field font-mono text-[13px]" rows={7} value={f.prompt_template} onChange={(e) => set("prompt_template", e.target.value)} disabled={ro} />
              </Field>
              <div className="grid sm:grid-cols-2 gap-5">
                <Field label="Материалов в контексте" hint="Сколько репрезентативных материалов сюжета видит модель."><input className="field num" type="number" min={1} max={15} value={f.context_items} onChange={(e) => set("context_items", num(e.target.value, 1, 15, 6))} disabled={ro} /></Field>
                <Field label="Предложений из материала" hint="Берутся ближайшие к смыслу сюжета — так контекст короче, а источник каждого факта известен."><input className="field num" type="number" min={1} max={8} value={f.context_sentences} onChange={(e) => set("context_sentences", num(e.target.value, 1, 8, 3))} disabled={ro} /></Field>
              </div>
            </>
          )}
        </Section>

        <Section title="Поиск сюжетов">
          <div className="grid sm:grid-cols-2 gap-5">
            <Field label="Окно, часов" hint="Материалы группируются в сюжет, если вышли в пределах окна.">
              <input className="field num" type="number" min={1} max={720} value={f.window_hours} onChange={(e) => set("window_hours", num(e.target.value, 1, 720, 48))} disabled={ro} />
            </Field>
            <Field label="Минимум материалов в сюжете" hint="По умолчанию 2: одиночные заметки не идут в автопостинг и дайджесты. Поставьте 1, если нужны и одиночные материалы — тогда имеет смысл поднять авторитетность источников, иначе в канал попадёт всё подряд.">
              <input className="field num" type="number" min={1} max={50} value={f.min_items} onChange={(e) => set("min_items", num(e.target.value, 1, 50, 2))} disabled={ro} />
            </Field>
          </div>
          <Field label={`Порог сходства: ${f.sim_threshold.toFixed(2)}`} hint="Выше — строже: сюжеты мельче и чище. Ниже — крупнее, но растёт риск склеить разные события. Значение подбирают на размеченных данных: scripts/eval_clustering.py.">
            <input type="range" min={0.3} max={0.95} step={0.01} className="w-full accent-[var(--brand)]" value={f.sim_threshold} onChange={(e) => set("sim_threshold", Number(e.target.value))} disabled={ro} />
          </Field>
          <Field label={`Минимальная близость к теме: ${f.topic_threshold.toFixed(2)}`} hint="Автоматический выбор постов и дайджестов пропустит только сюжеты не ниже этого значения. Поставьте 0, чтобы отключить фильтр; снижайте порог, если подходящих сюжетов мало.">
            <input type="range" min={0} max={0.8} step={0.01} className="w-full accent-[var(--brand)]" value={f.topic_threshold} onChange={(e) => set("topic_threshold", Number(e.target.value))} disabled={ro} />
          </Field>
        </Section>

        <Section title="Вес сюжета (NWS)">
          <div className="space-y-5">
            {Object.keys(SCORE_LABELS).map((k) => (
              <div key={k} className="grid sm:grid-cols-[220px_1fr_48px] items-center gap-x-5 gap-y-1">
                <div><span className="label mb-0">{SCORE_LABELS[k]}</span><span className="hint block">{SCORE_HINTS[k]}</span></div>
                <input type="range" min={0} max={1} step={0.05} className="w-full accent-[var(--brand)]" value={f.weights[k] ?? 0} onChange={(e) => setW(k, Number(e.target.value))} disabled={ro} aria-label={SCORE_LABELS[k]} />
                <span className="num text-right">{(f.weights[k] ?? 0).toFixed(2)}</span>
              </div>
            ))}
          </div>
        </Section>

        <Section title="Публикация">
          <Field
            label="Режим публикации"
            hint={
              f.publish_mode === "full_auto"
                ? "Публикуется без обычного редакторского просмотра. Пустой или слишком короткий текст, неполученная короткая статья и явное несоответствие языку останутся на проверке."
                : f.publish_mode === "auto"
                ? "Каждый пост сначала проходит автоматические проверки (заголовок, числа, ссылки, длина). Если замечаний нет — публикуется сразу; если есть — остаётся на проверке редактора с указанием причины."
                : "Каждый пост ждёт вашего решения, даже если автоматические проверки не нашли замечаний. Проверки только показывают подсказки, ничего не публикуют сами."
            }
          >
            <select className="field max-w-[360px]" value={f.publish_mode} onChange={(e) => set("publish_mode", e.target.value as ProjectInput["publish_mode"])} disabled={ro}>
              <option value="review">Ручное утверждение (всегда проверка редактором)</option>
              <option value="auto">Полуавтоматически (проверка перед публикацией, сомнительное — редактору)</option>
              <option value="full_auto">Автопубликация (кроме критических проблем качества)</option>
            </select>
          </Field>
          <div>
            <Toggle checked={f.auto_retry_unknown} onChange={(v) => set("auto_retry_unknown", v)} label="Повторять отправку при неизвестном исходе" />
            <p className="hint mt-1.5 max-w-[70ch]">Выключено по умолчанию: если Telegram не ответил, пост мог дойти, и повтор создаст дубль. Включайте для каналов, где дубль не страшен.</p>
          </div>
          <Toggle checked={f.active} onChange={(v) => set("active", v)} label="Проект активен (сбор и расписание работают)" />
        </Section>

        {!ro && (
          <div className="flex items-center gap-3 pt-2 rule-t py-5 sticky bottom-0 bg-paper">
            <button className="btn btn-primary h-10" disabled={save.isPending || !f.name.trim()}>{save.isPending ? "Сохраняем…" : creating ? "Создать проект" : "Сохранить"}</button>
            {creating && project && <button type="button" className="btn" onClick={() => setParams({})}>Отмена</button>}
            {!creating && project && <button type="button" className="btn btn-danger ml-auto" onClick={() => confirm(`Удалить проект «${project.name}» со всеми материалами, сюжетами и черновиками? Это нельзя отменить.`) && del.mutate()}>Удалить проект</button>}
          </div>
        )}
      </form>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="grid md:grid-cols-[180px_1fr] gap-x-10 gap-y-4">
      <h2 className="text-lg font-semibold md:pt-0.5">{title}</h2>
      <div className="space-y-5 min-w-0">{children}</div>
    </section>
  );
}
