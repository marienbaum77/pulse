import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useAuth, useProject, useToast } from "../lib/context";
import { CONTENT_PROFILES } from "../lib/labels";
import type { Project, ProjectInput } from "../lib/types";
import { Field, NumberInput, PageHeader, Spinner, Toggle } from "../components/ui";

const BLANK: ProjectInput = {
  name: "", topic: "", topic_aspects: [], language: "ru", tone: "нейтральный, информативный", max_length: 900, article_max_length: 1500, prompt_template: "",
  generation_mode: "llm", publish_mode: "review", window_hours: 48, sim_threshold: 0.72, topic_threshold: 0.4,
  weights: { coverage: 0.15, authority: 0.075, freshness: 0.1, velocity: 0.075, topic_fit: 0.6 },
  context_items: 6, context_sentences: 3, min_items: 2, auto_retry_unknown: false, show_sources: true, active: true,
  content_profile: "news", curated_window_hours: 336, source_cooldown_hours: 0,
  instant_interest: null, instant_daily_limit: 3, single_min_interest: null, slot_min_interest: null, interest_criteria: "", reference_tg_channels: [], reply_related_days: 14,
};
const toInput = (p: Project): ProjectInput => { const { id: _id, prompt_version: _v, topic_aspects_auto: _a, ...rest } = p; return rest; };

type EditorialState = {
  stage: string; pr_auc: number | null; roc_auc: number | null; positive_rate: number | null;
  paired_roc_auc: number | null; llm_roc_auc: number | null; precision_at_8: number | null;
  n_pos: number; n_neg: number; n_gold: number; n_test: number; n_compared: number;
  trained_at: string | null; top_features: { name: string; weight: number }[];
};

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
  // Аспекты модель составляет в фоне после сохранения: подставляем их, если поле в форме всё ещё пустое.
  const aspectsKey = project?.topic_aspects.join("\n") ?? "";
  useEffect(() => {
    if (!creating && project?.topic_aspects_auto) setF((x) => (x.topic_aspects.some((a) => a.trim()) ? x : { ...x, topic_aspects: project.topic_aspects }));
  }, [aspectsKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation({
    mutationFn: () => (creating ? api<Project>("/projects", { method: "POST", body: f }) : api<Project>(`/projects/${project!.id}`, { method: "PUT", body: f })),
    onSuccess: (p) => {
      toast(creating ? "Проект создан" : "Настройки сохранены");
      qc.invalidateQueries({ queryKey: ["projects"] });
      if (creating) { setProjectId(p.id); setParams({}); nav("/"); }
    },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  // Стандартная инструкция видна в пустом поле: иначе непонятно, по каким правилам пишет модель.
  const defaults = useQuery({ queryKey: ["prompt-defaults"], queryFn: () => api<{ formats: Record<string, string> }>("/prompt-defaults"), staleTime: Infinity });
  const defaultPrompt = defaults.data?.formats[f.content_profile === "curated" ? "article" : "news"] ?? "";
  const del = useMutation({
    mutationFn: () => api(`/projects/${project!.id}`, { method: "DELETE" }),
    onSuccess: () => { toast("Проект удалён"); qc.invalidateQueries(); nav("/"); },
    onError: (e: Error) => toast(e.message, "bad"),
  });
  const editorial = useQuery({
    queryKey: ["editorial-model", project?.id],
    queryFn: () => api<EditorialState>(`/projects/${project!.id}/editorial-model`),
    enabled: !creating && !!project,
  });
  const retrain = useMutation({
    mutationFn: () => api<EditorialState>(`/projects/${project!.id}/editorial-model/retrain`, { method: "POST" }),
    onSuccess: () => { toast("Переобучение поставлено в очередь"); qc.invalidateQueries({ queryKey: ["editorial-model", project?.id] }); },
    onError: (e: Error) => toast(e.message, "bad"),
  });

  if (loading) return <Spinner />;
  const set = <K extends keyof ProjectInput>(k: K, v: ProjectInput[K]) => setF((x) => ({ ...x, [k]: v }));
  const ro = !can("editor");

  return (
    <div className="fadein max-w-[860px]">
      <PageHeader
        title={creating ? "Новый проект" : "Настройки проекта"}
        sub={<>Тема, правила кластеризации и отбора сюжетов, стиль текста и режим публикации. Изменения действуют на новые материалы и следующие генерации.{!creating && project && <span className="block mt-1 text-[13px]">Номер проекта для скриптов: <b className="num">{project.id}</b></span>}</>}
        actions={!creating && can("editor") && <button className="btn" onClick={() => setParams({ new: "1" })}>Новый проект</button>}
      />
      <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }} className="space-y-12">
        <Section title="Основное">
          <Field label="Название"><input className="field" required value={f.name} onChange={(e) => set("name", e.target.value)} disabled={ro} /></Field>
          <Field label="Тема" hint="Одно-два предложения: фокус и область интересов проекта для отбора новостей.">
            <textarea className="field" rows={3} value={f.topic} onChange={(e) => set("topic", e.target.value)} disabled={ro} placeholder="Например: базы данных, облачная инфраструктура, ИИ и ИТ-инциденты" />
          </Field>
          <Field label="Аспекты темы" hint={<>Каждый аспект — отдельная фраза с новой строки. Материал считается тематичным, если его заголовок близок хотя бы к одному аспекту; близость сюжета — среднее по его материалам. Пусто — после сохранения аспекты составит языковая модель по теме (без модели — разбор строки «Тема» по запятым). Свои аспекты: 5–10 конкретных фраз без слов-ловушек («инциденты», «новости»).</>}>
            <textarea
              className="field font-mono text-[13px]"
              rows={6}
              value={f.topic_aspects.join("\n")}
              onChange={(e) => set("topic_aspects", e.target.value.split("\n"))}
              disabled={ro}
              placeholder={"базы данных и СУБД\nоблачные сервисы и инфраструктура\nискусственный интеллект и машинное обучение\nразработка и программирование\nкибербезопасность\nгаджеты и потребительская электроника"}
            />
            {!creating && project?.topic_aspects_auto && f.topic_aspects.join("\n") === project.topic_aspects.join("\n") && (
              <p className="hint mt-1.5">
                Составлены моделью по теме и составятся заново при её смене; правка вручную это отключит.{" "}
                {!ro && <button type="button" className="underline" onClick={() => set("topic_aspects", [])}>Составить заново</button>}
              </p>
            )}
          </Field>
          <Field label="Тип канала" hint={<>{CONTENT_PROFILES[f.content_profile].hint} Смена типа действует на материалы, собранные после сохранения.</>}>
            <select className="field max-w-[420px]" value={f.content_profile} onChange={(e) => set("content_profile", e.target.value as ProjectInput["content_profile"])} disabled={ro}>
              {(Object.keys(CONTENT_PROFILES) as ProjectInput["content_profile"][]).map((k) => <option key={k} value={k}>{CONTENT_PROFILES[k].label}</option>)}
            </select>
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
            <Field label="Максимальная длина, символов"><NumberInput min={200} max={3500} fallback={900} value={f.max_length} onChange={(v) => set("max_length", v)} disabled={ro} /></Field>
            <Field label="Длина разбора статьи, символов" hint="Аналитику и лонгриды (статья из одного источника длиннее 6000 знаков) модель читает целиком и пересказывает разбором, а не заметкой по первым абзацам. Тот же лимит — у статей в подборке."><NumberInput min={300} max={3500} fallback={1500} value={f.article_max_length} onChange={(v) => set("article_max_length", v)} disabled={ro} /></Field>
            <Field label="Язык"><input className="field" value={f.language} onChange={(e) => set("language", e.target.value)} disabled={ro} /></Field>
            <Field label="Тон" hint="Стиль общения модели: нейтральный, деловой, разговорный и т.п. Учитывается при генерации моделью и обязательном переводе."><input className="field" value={f.tone} onChange={(e) => set("tone", e.target.value)} disabled={ro} /></Field>
          </div>
          {(f.generation_mode === "llm" || f.generation_mode === "auto") && (
            <>
              <Field label="Инструкция для модели" hint={<>Пусто — используется стандартная, она показана в поле серым{f.content_profile !== "news" ? " (для статей; у видео и постов своя)" : ""}. Своя инструкция заменяет стандартные для всех видов материалов. Можно подставлять <code>{"{language}"}</code>, <code>{"{tone}"}</code>, <code>{"{topic}"}</code>, <code>{"{max_length}"}</code>, <code>{"{formatting}"}</code> (правило про **жирный** из стандартной инструкции). Каждое изменение увеличивает версию промпта, она сохраняется в черновиках.</>}>
                <textarea className="field font-mono text-[13px]" rows={f.prompt_template.trim() ? 7 : 12} value={f.prompt_template} onChange={(e) => set("prompt_template", e.target.value)} disabled={ro} placeholder={defaultPrompt} />
              </Field>
              {!ro && !f.prompt_template.trim() && defaultPrompt && (
                <button type="button" className="btn btn-sm -mt-3" onClick={() => set("prompt_template", defaultPrompt)}>Взять стандартную за основу</button>
              )}
              <div className="grid sm:grid-cols-2 gap-5">
                <Field label="Материалов в контексте" hint="Сколько репрезентативных материалов сюжета видит модель."><NumberInput min={1} max={15} fallback={6} value={f.context_items} onChange={(v) => set("context_items", v)} disabled={ro} /></Field>
                <Field label="Предложений из материала" hint="Берутся ближайшие к смыслу сюжета — так контекст короче, а источник каждого факта известен."><NumberInput min={1} max={8} fallback={3} value={f.context_sentences} onChange={(v) => set("context_sentences", v)} disabled={ro} /></Field>
              </div>
            </>
          )}
        </Section>

        <Section title="Поиск сюжетов">
          {f.content_profile !== "news" && (
            <Field label="Сколько дней подборочный материал остаётся кандидатом" hint="Видео и статьи не устаревают за двое суток, как новости: кандидат ждёт публикации весь этот срок, а свежесть падает вдвое каждую четверть срока. Окно и минимум материалов ниже действуют только на новостные сюжеты.">
              <NumberInput className="max-w-[160px]" min={1} max={90} fallback={14} value={Math.round(f.curated_window_hours / 24)} onChange={(v) => set("curated_window_hours", v * 24)} disabled={ro} />
            </Field>
          )}
          <div className="grid sm:grid-cols-2 gap-5">
            <Field label="Окно, часов" hint="Материалы группируются в сюжет, если вышли в пределах окна.">
              <NumberInput min={1} max={720} fallback={48} value={f.window_hours} onChange={(v) => set("window_hours", v)} disabled={ro} />
            </Field>
            <Field label="Минимум материалов в сюжете" hint="По умолчанию 2: одиночные заметки не идут в автопостинг и дайджесты. Поставьте 1, если нужны и одиночные материалы — тогда имеет смысл поднять авторитетность источников, иначе в канал попадёт всё подряд.">
              <NumberInput min={1} max={50} fallback={2} value={f.min_items} onChange={(v) => set("min_items", v)} disabled={ro} />
            </Field>
          </div>
          {f.min_items > 1 && (
            <div>
              <Toggle checked={f.single_min_interest != null} onChange={(v) => set("single_min_interest", v ? 7 : null)} label="Пропускать одиночные сюжеты с высокой интересностью" />
              <p className="hint mt-1.5 max-w-[70ch]">Новостной сюжет, в котором меньше материалов, чем задано выше, всё равно идёт в публикацию, если модель оценила его не ниже порога. Мелочь из одного источника по-прежнему отсекается. В LLM-режиме нужна чат-модель, в локальном — редакционная.</p>
              {f.single_min_interest != null && (
                <div className="mt-4">
                  <Field label="Порог интересности для одиночного сюжета, из 10">
                    <NumberInput className="max-w-[160px]" min={0} max={10} step={0.5} fallback={7} value={f.single_min_interest} onChange={(v) => set("single_min_interest", v)} disabled={ro} />
                  </Field>
                </div>
              )}
            </div>
          )}
          <Field label={`Порог сходства: ${f.sim_threshold.toFixed(2)}`} hint="Выше — строже: сюжеты мельче и чище. Ниже — крупнее, но растёт риск склеить разные события. Значение подбирают на размеченных данных: scripts/eval_clustering.py.">
            <input type="range" min={0.3} max={0.95} step={0.01} className="w-full accent-[var(--brand)]" value={f.sim_threshold} onChange={(e) => set("sim_threshold", Number(e.target.value))} disabled={ro} />
          </Field>
          <Field label={`Минимальная близость к теме: ${f.topic_threshold.toFixed(2)}`} hint="Близость сюжета — среднее по его материалам: насколько заголовки близки к аспектам темы. Это косинусное сходство, а не вероятность; сверяйте порог с карточкой сюжета. Поставьте 0, чтобы отключить фильтр.">
            <input type="range" min={0} max={0.8} step={0.01} className="w-full accent-[var(--brand)]" value={f.topic_threshold} onChange={(e) => set("topic_threshold", Number(e.target.value))} disabled={ro} />
          </Field>
        </Section>

        <Section title="Редакционная модель">
          <Field label="Эталонные Telegram-каналы" hint="До пяти публичных каналов, по одному @name или ссылке на строку. Их посты задают вкус локальной модели отбора; сами по себе они ничего не публикуют в ваш канал. Сохраните изменения перед переобучением.">
            <textarea className="field font-mono text-[13px]" rows={4} value={f.reference_tg_channels.join("\n")}
              onChange={(e) => set("reference_tg_channels", e.target.value.split("\n").map((v) => v.trim()).filter(Boolean))} disabled={ro} placeholder={"@example\nhttps://t.me/another"} />
          </Field>
          {!creating && (
            <div className="rule rounded-lg p-4 space-y-3">
              <div className="text-sm">
                Стадия: <b>{editorial.data?.stage === "AB" ? "вкус и признаки сюжета" : editorial.data?.stage === "A_only" ? "только вкус, оценка не выше 7" : "модель ещё не обучена"}</b>
                {editorial.data?.trained_at && <span className="block hint mt-1">Обучена: {new Date(editorial.data.trained_at).toLocaleString("ru-RU")}</span>}
              </div>
              {editorial.data && editorial.data.stage !== "untrained" && (
                <div className="grid sm:grid-cols-3 gap-3 text-sm">
                  <span>PR-AUC (тест, n={editorial.data.n_test}): <b className="num">{editorial.data.pr_auc == null ? "н/д" : editorial.data.pr_auc.toFixed(3)}</b></span>
                  <span>ROC-AUC (тест): <b className="num">{editorial.data.roc_auc == null ? "н/д" : editorial.data.roc_auc.toFixed(3)}</b></span>
                  <span>Доля позитивов (база): <b className="num">{editorial.data.positive_rate == null ? "н/д" : editorial.data.positive_rate.toFixed(3)}</b></span>
                  {editorial.data.n_compared > 0 && <span>ROC-AUC на {editorial.data.n_compared} общих строках: <b className="num">{editorial.data.paired_roc_auc == null ? "н/д" : editorial.data.paired_roc_auc.toFixed(3)}</b> локальная / <b className="num">{editorial.data.llm_roc_auc == null ? "н/д" : editorial.data.llm_roc_auc.toFixed(3)}</b> LLM</span>}
                  {editorial.data.stage === "AB" && <span>Precision при 8+ (тест): <b className="num">{editorial.data.precision_at_8 == null ? "н/д" : editorial.data.precision_at_8.toFixed(3)}</b></span>}
                  <span>Примеров: <b className="num">{editorial.data.n_pos + editorial.data.n_neg}</b> (решений человека: {editorial.data.n_gold})</span>
                </div>
              )}
              {!ro && <button type="button" className="btn btn-sm" disabled={retrain.isPending} onClick={() => retrain.mutate()}>{retrain.isPending ? "Ставим в очередь…" : "Переобучить"}</button>}
            </div>
          )}
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
          <Field label="Пауза между постами из одного источника, часов" hint="0 — без ограничений. Например, 12: если YouTube-канал выложил пять видео сразу, в канал попадёт одно, следующее — не раньше чем через 12 часов, а слоты займут другие источники.">
            <NumberInput className="max-w-[160px]" min={0} max={168} fallback={0} value={f.source_cooldown_hours} onChange={(v) => set("source_cooldown_hours", v)} disabled={ro} />
          </Field>
          <div>
            <Toggle checked={f.show_sources} onChange={(v) => set("show_sources", v)} label="Список источников в конце поста" />
            <p className="hint mt-1.5 max-w-[70ch]">Строка «Источники: …» со ссылками на материалы. Выключите, если пост и так ведёт на первоисточник строкой «👉» или ссылка не нужна. Подпись канала задаётся в «Каналы и расписание».</p>
          </div>
          <Field label="Отвечать на связанные посты за последние, дней" hint="Пост-продолжение выходит в Telegram ответом (reply) на прежний пост: обновление того же сюжета, тот же репозиторий или первоисточник, близкая тема с общим именем в заголовке. Связь ищется без языковой модели. 0 — всегда отдельным сообщением.">
            <NumberInput className="max-w-[160px]" min={0} max={60} fallback={14} value={f.reply_related_days} onChange={(v) => set("reply_related_days", v)} disabled={ro} />
          </Field>
          <Field label="Критерии интересности" hint="Чем ваш канал считает материал сильным, а чем — слабым. Заменяют стандартные критерии модели («важные события, громкие релизы»). После изменения открытые сюжеты будут оценены заново. Пусто — стандартные критерии.">
            <textarea
              className="field text-[13px]" rows={4} value={f.interest_criteria} maxLength={2000} onChange={(e) => set("interest_criteria", e.target.value)} disabled={ro}
              placeholder={"Высоко: инструменты и модели, которые можно попробовать сегодня; open-source репозитории; конкретные цифры (цена, звёзды, бенчмарки).\nНизко: пресс-релизы, корпоративные назначения, мнения без фактов, отчёты о рынке."}
            />
          </Field>
          <div>
            <Toggle checked={f.slot_min_interest != null} onChange={(v) => set("slot_min_interest", v ? 6 : null)} label="Не заполнять слот слабым сюжетом" />
            <p className="hint mt-1.5 max-w-[70ch]">Слот расписания берёт сюжет, только если модель оценила его не ниже порога. Если подходящих нет, слот остаётся пустым: пустой слот лучше слабого поста. Сюжеты без оценки (модель недоступна) тоже пропускаются. Выключено — слот берёт лучший из имеющихся. В LLM-режиме нужна чат-модель, в локальном — редакционная.</p>
            {f.slot_min_interest != null && (
              <div className="mt-4">
                <Field label="Минимальная интересность для слота, из 10">
                  <NumberInput className="max-w-[160px]" min={0} max={10} step={0.5} fallback={6} value={f.slot_min_interest} onChange={(v) => set("slot_min_interest", v)} disabled={ro} />
                </Field>
              </div>
            )}
          </div>
          <div>
            <Toggle checked={f.instant_interest != null} onChange={(v) => set("instant_interest", v ? 8 : null)} label="Публиковать самые интересные сюжеты сразу" />
            <p className="hint mt-1.5 max-w-[70ch]">Сюжет, который модель оценила не ниже порога, публикуется сразу после оценки, не дожидаясь слота расписания. Действует и при ручном утверждении: пост без замечаний автопроверок уходит сам, сомнительный остаётся редактору. В LLM-режиме нужна чат-модель, в локальном — редакционная.</p>
            {f.instant_interest != null && (
              <div className="grid sm:grid-cols-2 gap-5 mt-4">
                <Field label="Порог интересности, из 10">
                  <NumberInput min={0} max={10} step={0.5} fallback={8} value={f.instant_interest} onChange={(v) => set("instant_interest", v)} disabled={ro} />
                </Field>
                <Field label="Не больше постов за 24 часа" hint="Посты по слотам расписания в лимит не входят.">
                  <NumberInput min={1} max={50} fallback={3} value={f.instant_daily_limit} onChange={(v) => set("instant_daily_limit", v)} disabled={ro} />
                </Field>
              </div>
            )}
          </div>
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
