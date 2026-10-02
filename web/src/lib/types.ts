export type Role = "admin" | "editor" | "viewer";
export interface User { id: number; email: string; role: Role; created_at?: string }

export interface Project {
  id: number; name: string; topic: string; language: string; tone: string; max_length: number;
  prompt_template: string; prompt_version: number; generation_mode: "llm" | "extractive" | "auto"; publish_mode: "review" | "auto" | "full_auto";
  window_hours: number; sim_threshold: number; topic_threshold: number; weights: Record<string, number>;
  context_items: number; context_sentences: number; min_items: number; auto_retry_unknown: boolean; show_sources: boolean; active: boolean;
}
export type ProjectInput = Omit<Project, "id" | "prompt_version">;

export interface ScorePart { value: number; weight: number; contribution: number; neutral?: boolean }
export type ClusterState = "open" | "drafted" | "published" | "closed" | "excluded";
export interface Cluster {
  id: number; title: string; state: ClusterState; item_count: number; source_count: number;
  first_seen: string; last_seen: string; score: number | null; score_breakdown: Record<string, ScorePart> | null;
  published_at: string | null; arrivals: string[] | null; image_item_id: number | null;
}
export interface ClusterDetail extends Omit<Cluster, "arrivals"> {
  project_id: number;
  interest: number | null; interest_reason: string | null;
  image_item_id: number | null;
  items: { id: number; title: string; url: string; published_at: string; source_name: string | null; has_image: boolean }[];
  drafts: { id: number; title: string; status: DraftStatus; created_at: string }[];
}

export type DraftStatus = "generating" | "pending_review" | "approved" | "rejected" | "failed";
export interface Citation { n: number; item_id: number; title: string; url: string; source: string; excerpt: string; image_url?: string | null }
export interface DraftChecks {
  mode?: string; unsupported_numbers?: string[]; invalid_citations?: number[]; citation_coverage?: number;
  length?: number; edited?: boolean; fallback?: string;
  automatic_review?: { passed: boolean; reasons: string[]; overridden?: boolean; auto_publish_enabled?: boolean };
}
export interface DraftListItem {
  id: number; kind: "post" | "digest"; title: string; status: DraftStatus; model: string | null;
  checks: DraftChecks; error: string | null; created_at: string; updated_at: string; cluster_ids: number[]; has_image: boolean;
}
export interface GenerationJob {
  id: number; status: "queued"; created_at: string; kind: "post" | "digest"; title: string;
}
export interface Draft extends DraftListItem {
  project_id: number; image_url: string | null; body: string; citations: Citation[]; params: Record<string, unknown>;
  publications: { id: number; status: PubStatus; error: string | null; sent_at: string | null; channel_name: string }[];
}

export type PubStatus = "pending" | "sending" | "sent" | "failed" | "unknown" | "cancelled";
export interface Publication {
  id: number; status: PubStatus; due_at: string; attempts: number; external_id: string | null; error: string | null;
  sent_at: string | null; draft_id: number; draft_title: string; channel_name: string; channel_type: string;
}

export interface Source {
  id: number; project_id: number; type: "rss" | "manual"; name: string; url: string; authority: number; poll_minutes: number;
  enabled: boolean; last_fetched_at: string | null; last_ok_at: string | null; last_error: string | null; item_count: number;
  last_entry_count: number; last_new_count: number;
}
export interface Channel {
  id: number; project_id: number; type: "telegram" | "webhook" | "console"; name: string; config: Record<string, string>;
  enabled: boolean; has_secret: boolean;
}
export interface Schedule {
  id: number; project_id: number; name: string; cron: string; tz: string; kind: "post" | "digest"; top_n: number;
  enabled: boolean; last_fired_at: string | null; next_fire: string | null;
}

export interface Dashboard {
  metrics: { items: number; items_prev: number; open_clusters: number; pending_drafts: number; published: number };
  daily: { date: string; items: number }[];
  top_clusters: { id: number; title: string; score: number | null; item_count: number; source_count: number; last_seen: string }[];
  review: { id: number; title: string; created_at: string }[];
  generating: { id: number; kind: "post" | "digest"; title: string; created_at: string }[];
  generation_queue: GenerationJob[];
  queue: { id: number; status: PubStatus; due_at: string; title: string }[];
  upcoming: { id: number; name: string; kind: string; at: string }[];
  pipeline: { worker_seen_seconds: number | null; last_ingest_ok: string | null; failing_sources: number; rss_enabled: number; jobs_queued: number; jobs_failed_24h: number };
  llm: { calls: number; avg_chat_ms: number; prompt_tokens: number; completion_tokens: number; errors: number; provider: string; chat_model: string; embed_model: string };
}

export interface AuditEntry { id: number; user_email: string | null; action: string; entity: string; entity_id: number | null; detail: Record<string, unknown>; created_at: string }
export interface Health { db: boolean; worker_seen_seconds: number | null; provider: string; model?: { ok: boolean; provider: string; error?: string; embed_dim?: number; chat_model?: string; embed_model?: string } }
export interface SystemConfig {
  llm_provider: string; llm_base_url: string | null; llm_model: string; embed_model: string;
  llm_model_env?: string; embed_model_env?: string; runtime_override?: boolean;
  telegram_token_configured: boolean; allow_private_urls: boolean; retention_days: number; worker_concurrency: number;
}
