import type { ClusterState, ContentKind, ContentProfile, DraftStatus, PubStatus } from "./types";

export const SCORE_LABELS: Record<string, string> = {
  topic_fit: "Близость заголовка к теме",
  coverage: "Охват источниками",
  authority: "Авторитетность",
  freshness: "Свежесть",
  velocity: "Скорость роста",
};
export const SCORE_HINTS: Record<string, string> = {
  topic_fit: "Семантическое сходство заголовков материалов в сюжете с темой проекта; содержимое статей не влияет на этот показатель",
  coverage: "Сколько разных источников пишет об этом",
  authority: "Средний вес источников, которые упомянули сюжет",
  freshness: "Падает вдвое каждую четверть окна кластеризации",
  velocity: "Сколько материалов пришло за последние 6 часов",
};
/** Признаки подборочного (curated) контента: в разложении веса они идут вместе с новостными, но настраиваемых весов у них нет. */
export const BREAKDOWN_LABELS: Record<string, string> = {
  ...SCORE_LABELS,
  engagement: "Вовлечённость",
  depth: "Насыщенность",
};
export const BREAKDOWN_HINTS: Record<string, string> = {
  ...SCORE_HINTS,
  freshness: "Падает вдвое каждую четверть окна: у новостей — окна кластеризации, у подборки — окна жизни кандидата",
  engagement: "Просмотры, оценки или апвоуты материала в логарифмической шкале; н/д — площадка их не отдаёт",
  depth: "Сколько текста удалось получить: полная статья или расшифровка видео весит больше сниппета",
};

export const CONTENT_KINDS: Record<ContentKind, string> = { article: "статья", video: "видео", post: "пост" };
export const CONTENT_PROFILES: Record<ContentProfile, { label: string; hint: string }> = {
  news: {
    label: "Новостной канал",
    hint: "Материалы об одном событии собираются в сюжеты; в публикацию идут сюжеты, о которых пишут несколько источников, пока они свежие.",
  },
  curated: {
    label: "Тематический канал (подборка контента)",
    hint: "Каждое видео, статья или пост — самостоятельный кандидат: второй источник не нужен, кандидат живёт дольше, а вес учитывает вовлечённость и насыщенность материала.",
  },
  mixed: {
    label: "Смешанный",
    hint: "RSS-ленты и Telegram дают новостные сюжеты, YouTube, веб-поиск, сайты и Reddit — подборочный контент. Режим можно переопределить у каждого источника.",
  },
};

export const CLUSTER_STATE: Record<ClusterState, { label: string; tone: Tone }> = {
  open: { label: "Ждёт решения", tone: "info" },
  drafted: { label: "Есть черновик", tone: "warn" },
  published: { label: "Опубликован", tone: "ok" },
  closed: { label: "Устарел", tone: "muted" },
  excluded: { label: "Исключён", tone: "muted" },
};
export const DRAFT_STATUS: Record<DraftStatus, { label: string; tone: Tone }> = {
  generating: { label: "Генерируется", tone: "info" },
  pending_review: { label: "На проверке", tone: "warn" },
  approved: { label: "Утверждён", tone: "ok" },
  rejected: { label: "Отклонён", tone: "muted" },
  failed: { label: "Ошибка", tone: "bad" },
};
export const PUB_STATUS: Record<PubStatus, { label: string; tone: Tone }> = {
  pending: { label: "В очереди", tone: "info" },
  sending: { label: "Отправляется", tone: "info" },
  sent: { label: "Отправлено", tone: "ok" },
  failed: { label: "Не отправлено", tone: "bad" },
  unknown: { label: "Исход неизвестен", tone: "warn" },
  cancelled: { label: "Отменено", tone: "muted" },
};
export type Tone = "ok" | "warn" | "bad" | "info" | "muted";
export const CHANNEL_TYPES = {
  telegram: "Telegram",
  vk: "ВКонтакте",
  discord: "Discord",
  webhook: "Webhook",
  console: "Консоль (журнал)",
} as const;
