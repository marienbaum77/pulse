import type { ClusterState, DraftStatus, PubStatus } from "./types";

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
export const CHANNEL_TYPES = { telegram: "Telegram", webhook: "Webhook", console: "Консоль (журнал)" } as const;
