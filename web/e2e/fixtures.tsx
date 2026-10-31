import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import App from "../src/App";
import { AuthProvider, ProjectProvider, ToastProvider } from "../src/lib/context";
import type {
  Cluster,
  ClusterDetail,
  Dashboard,
  Draft,
  DraftListItem,
  GenerationJob,
  Project,
  Publication,
  Source,
  User,
} from "../src/lib/types";
import { vi } from "vitest";

export const mockUser: User = {
  id: 1,
  email: "editor@pulse.local",
  role: "editor",
  created_at: "2026-01-01T00:00:00Z",
};

export const mockProject: Project = {
  id: 1,
  name: "Pulse Intelligence",
  topic: "Artificial Intelligence and Emerging Hardware",
  topic_aspects: ["quantum", "llm", "chips"],
  language: "ru",
  tone: "informative",
  max_length: 1000,
  article_max_length: 3000,
  prompt_template: "Standard",
  prompt_version: 1,
  generation_mode: "auto",
  publish_mode: "review",
  window_hours: 48,
  sim_threshold: 0.75,
  topic_threshold: 0.3,
  weights: { topic_fit: 0.35, coverage: 0.25, authority: 0.2, freshness: 0.1, velocity: 0.1 },
  context_items: 5,
  context_sentences: 10,
  min_items: 2,
  auto_retry_unknown: true,
  show_sources: true,
  active: true,
  content_profile: "mixed",
  curated_window_hours: 72,
  source_cooldown_hours: 4,
  instant_interest: 8.5,
  instant_daily_limit: 10,
  single_min_interest: 7.0,
  slot_min_interest: 6.0,
  interest_criteria: "Strict technical relevance",
  reply_related_days: 7,
  topic_aspects_auto: true,
};

export const mockClusters: Cluster[] = [
  {
    id: 101,
    title: "Quantum Computing Breakthrough in Silicon",
    state: "open",
    item_count: 5,
    source_count: 3,
    first_seen: "2026-10-29T08:00:00Z",
    last_seen: "2026-10-29T13:45:00Z",
    score: 0.85,
    score_breakdown: {
      topic_fit: { value: 0.95, weight: 0.35, contribution: 0.33 },
      coverage: { value: 0.8, weight: 0.25, contribution: 0.2 },
      authority: { value: 0.75, weight: 0.2, contribution: 0.15 },
      freshness: { value: 0.9, weight: 0.1, contribution: 0.09 },
      velocity: { value: 0.8, weight: 0.1, contribution: 0.08 },
    },
    published_at: null,
    arrivals: ["2026-10-29T08:00:00Z", "2026-10-29T10:00:00Z", "2026-10-29T13:45:00Z"],
    image_item_id: null,
    interest: 9.8,
    interest_reason: "Exceptional breakthrough in fault-tolerant quantum logic gates",
    mode: "news",
    kind: "article",
  },
  {
    id: 102,
    title: "New AI Architecture Announced by Major Lab",
    state: "open",
    item_count: 4,
    source_count: 2,
    first_seen: "2026-10-29T09:00:00Z",
    last_seen: "2026-10-29T13:30:00Z",
    score: 0.9,
    score_breakdown: {
      topic_fit: { value: 0.9, weight: 0.35, contribution: 0.315 },
      coverage: { value: 0.85, weight: 0.25, contribution: 0.21 },
      authority: { value: 0.9, weight: 0.2, contribution: 0.18 },
      freshness: { value: 0.85, weight: 0.1, contribution: 0.085 },
      velocity: { value: 0.9, weight: 0.1, contribution: 0.09 },
    },
    published_at: null,
    arrivals: ["2026-10-29T09:00:00Z", "2026-10-29T13:30:00Z"],
    image_item_id: null,
    interest: 7.5,
    interest_reason: "High interest architecture paper with public benchmarks",
    mode: "news",
    kind: "article",
  },
  {
    id: 103,
    title: "Routine Dependency Maintenance Release",
    state: "open",
    item_count: 2,
    source_count: 1,
    first_seen: "2026-10-29T06:00:00Z",
    last_seen: "2026-10-29T07:00:00Z",
    score: 0.4,
    score_breakdown: {
      topic_fit: { value: 0.3, weight: 0.35, contribution: 0.105 },
      coverage: { value: 0.2, weight: 0.25, contribution: 0.05 },
      authority: { value: 0.5, weight: 0.2, contribution: 0.1 },
      freshness: { value: 0.7, weight: 0.1, contribution: 0.07 },
      velocity: { value: 0.4, weight: 0.1, contribution: 0.04 },
    },
    published_at: null,
    arrivals: ["2026-10-29T06:00:00Z"],
    image_item_id: null,
    interest: 3.2,
    interest_reason: "Low impact incremental release without new capabilities",
    mode: "news",
    kind: "article",
  },
  {
    id: 104,
    title: "Zero Interest Story Boundary Value",
    state: "open",
    item_count: 2,
    source_count: 1,
    first_seen: "2026-10-29T05:00:00Z",
    last_seen: "2026-10-29T05:30:00Z",
    score: 0.1,
    score_breakdown: null,
    published_at: null,
    arrivals: ["2026-10-29T05:00:00Z"],
    image_item_id: null,
    interest: 0.0,
    interest_reason: "Zero novelty and zero relevance to project scope",
    mode: "news",
    kind: "article",
  },
  {
    id: 105,
    title: "Maximum Score Perfect Ten Breakthrough",
    state: "open",
    item_count: 8,
    source_count: 4,
    first_seen: "2026-10-29T09:30:00Z",
    last_seen: "2026-10-29T14:00:00Z",
    score: 0.99,
    score_breakdown: null,
    published_at: null,
    arrivals: ["2026-10-29T09:30:00Z", "2026-10-29T14:00:00Z"],
    image_item_id: null,
    interest: 10.0,
    interest_reason: "Historic milestone achieving verified quantum advantage",
    mode: "news",
    kind: "article",
  },
  {
    id: 106,
    title: "Unrated Story With Null Interest",
    state: "open",
    item_count: 2,
    source_count: 1,
    first_seen: "2026-10-29T04:00:00Z",
    last_seen: "2026-10-29T04:30:00Z",
    score: 0.5,
    score_breakdown: null,
    published_at: null,
    arrivals: ["2026-10-29T04:00:00Z"],
    image_item_id: null,
    interest: null,
    interest_reason: null,
    mode: "news",
    kind: "article",
  },
  {
    id: 107,
    title: "Already Published Milestone Post",
    state: "published",
    item_count: 6,
    source_count: 3,
    first_seen: "2026-10-28T10:00:00Z",
    last_seen: "2026-10-28T18:00:00Z",
    score: 0.88,
    score_breakdown: null,
    published_at: "2026-10-28T19:00:00Z",
    arrivals: ["2026-10-28T10:00:00Z"],
    image_item_id: null,
    interest: 8.9,
    interest_reason: "Widely cited community milestone",
    mode: "news",
    kind: "article",
  },
  {
    id: 108,
    title: "Outdated Excluded Item",
    state: "closed",
    item_count: 1,
    source_count: 1,
    first_seen: "2026-10-25T10:00:00Z",
    last_seen: "2026-10-25T11:00:00Z",
    score: 0.25,
    score_breakdown: null,
    published_at: null,
    arrivals: ["2026-10-25T10:00:00Z"],
    image_item_id: null,
    interest: 2.1,
    interest_reason: "Deprecating announcement",
    mode: "news",
    kind: "article",
  },
];

export const mockDrafts: DraftListItem[] = [
  {
    id: 201,
    kind: "post",
    title: "Квантовый прорыв: кремниевые кубиты достигли рекордной точности",
    status: "pending_review",
    model: "gpt-4o",
    checks: {
      mode: "news",
      invalid_citations: [],
      citation_coverage: 1.0,
      length: 650,
      edited: false,
    },
    error: null,
    created_at: "2026-10-29T12:00:00Z",
    updated_at: "2026-10-29T12:05:00Z",
    cluster_ids: [101],
    has_image: false,
  },
  {
    id: 202,
    kind: "post",
    title: "Архитектура нового поколения для распределенного обучения",
    status: "pending_review",
    model: "gpt-4o",
    checks: {
      mode: "news",
      invalid_citations: [2],
      citation_coverage: 0.5,
      length: 520,
      edited: false,
    },
    error: null,
    created_at: "2026-10-29T11:00:00Z",
    updated_at: "2026-10-29T11:04:00Z",
    cluster_ids: [102],
    has_image: false,
  },
  {
    id: 203,
    kind: "post",
    title: "Слишком короткий черновик с предупреждением о длине",
    status: "pending_review",
    model: "gpt-4o",
    checks: {
      mode: "news",
      invalid_citations: [],
      citation_coverage: 1.0,
      length: 80,
      edited: true,
    },
    error: null,
    created_at: "2026-10-29T10:00:00Z",
    updated_at: "2026-10-29T10:02:00Z",
    cluster_ids: [103],
    has_image: false,
  },
  {
    id: 204,
    kind: "post",
    title: "Утвержденный пост о квантовом превосходстве",
    status: "approved",
    model: "gpt-4o",
    checks: {
      mode: "news",
      invalid_citations: [],
      citation_coverage: 1.0,
      length: 700,
      edited: false,
    },
    error: null,
    created_at: "2026-10-29T09:00:00Z",
    updated_at: "2026-10-29T09:30:00Z",
    cluster_ids: [105],
    has_image: false,
  },
  {
    id: 205,
    kind: "digest",
    title: "Генерация дайджеста ключевых технологий недели",
    status: "generating",
    model: "gpt-4o",
    checks: {},
    error: null,
    created_at: "2026-10-29T13:00:00Z",
    updated_at: "2026-10-29T13:00:00Z",
    cluster_ids: [101, 102],
    has_image: false,
  },
];

export const mockGenerationQueue: GenerationJob[] = [
  {
    id: 501,
    status: "queued",
    created_at: "2026-10-29T13:50:00Z",
    kind: "post",
    title: "Фоновая генерация по новому сюжету",
  },
];

export const mockPublications: Publication[] = [
  {
    id: 301,
    status: "sent",
    due_at: "2026-10-29T10:00:00Z",
    attempts: 1,
    external_id: "tg_msg_9876",
    error: null,
    sent_at: "2026-10-29T10:00:05Z",
    draft_id: 204,
    draft_title: "Утвержденный пост о квантовом превосходстве",
    channel_name: "Pulse Official Telegram",
    channel_type: "telegram",
  },
  {
    id: 302,
    status: "pending",
    due_at: "2026-10-29T15:00:00Z",
    attempts: 0,
    external_id: null,
    error: null,
    sent_at: null,
    draft_id: 201,
    draft_title: "Квантовый прорыв: кремниевые кубиты достигли рекордной точности",
    channel_name: "Pulse Official Telegram",
    channel_type: "telegram",
  },
  {
    id: 303,
    status: "unknown",
    due_at: "2026-10-29T08:00:00Z",
    attempts: 3,
    external_id: null,
    error: "Таймаут шлюза Telegram API",
    sent_at: null,
    draft_id: 202,
    draft_title: "Архитектура нового поколения для распределенного обучения",
    channel_name: "Pulse Official Telegram",
    channel_type: "telegram",
  },
  {
    id: 304,
    status: "failed",
    due_at: "2026-10-29T07:00:00Z",
    attempts: 5,
    external_id: null,
    error: "Канал недоступен или заблокирован",
    sent_at: null,
    draft_id: 203,
    draft_title: "Слишком короткий черновик с предупреждением о длине",
    channel_name: "Pulse Official Telegram",
    channel_type: "telegram",
  },
];

export const mockSources: Source[] = [
  {
    id: 401,
    project_id: 1,
    type: "rss",
    name: "ArXiv Quantum & AI News",
    url: "https://arxiv.org/rss/cs.AI",
    config: {},
    authority: 0.95,
    poll_minutes: 30,
    enabled: true,
    last_fetched_at: "2026-10-29T13:30:00Z",
    last_ok_at: "2026-10-29T13:30:00Z",
    last_error: null,
    item_count: 215,
    last_entry_count: 12,
    last_new_count: 4,
  },
  {
    id: 402,
    project_id: 1,
    type: "telegram",
    name: "AI Frontiers Telegram Channel",
    url: "https://t.me/ai_frontiers",
    config: { channel: "ai_frontiers" },
    authority: 0.85,
    poll_minutes: 60,
    enabled: true,
    last_fetched_at: "2026-10-29T13:00:00Z",
    last_ok_at: "2026-10-29T13:00:00Z",
    last_error: null,
    item_count: 88,
    last_entry_count: 5,
    last_new_count: 1,
  },
  {
    id: 403,
    project_id: 1,
    type: "youtube",
    name: "Quantum Tech Weekly",
    url: "https://youtube.com/@QuantumTechWeekly",
    config: { kind: "channel" },
    authority: 0.75,
    poll_minutes: 180,
    enabled: true,
    last_fetched_at: "2026-10-29T12:00:00Z",
    last_ok_at: "2026-10-29T12:00:00Z",
    last_error: null,
    item_count: 34,
    last_entry_count: 2,
    last_new_count: 0,
  },
];

export const mockDashboard: Dashboard = {
  metrics: {
    items: 337,
    items_prev: 280,
    open_clusters: 6,
    pending_drafts: 3,
    published: 1,
  },
  daily: [
    { date: "2026-10-23", items: 40 },
    { date: "2026-10-24", items: 45 },
    { date: "2026-10-25", items: 50 },
    { date: "2026-10-26", items: 48 },
    { date: "2026-10-27", items: 52 },
    { date: "2026-10-28", items: 55 },
    { date: "2026-10-29", items: 47 },
  ],
  top_clusters: [
    {
      id: 101,
      title: "Quantum Computing Breakthrough in Silicon",
      score: 0.85,
      item_count: 5,
      source_count: 3,
      last_seen: "2026-10-29T13:45:00Z",
    },
    {
      id: 102,
      title: "New AI Architecture Announced by Major Lab",
      score: 0.9,
      item_count: 4,
      source_count: 2,
      last_seen: "2026-10-29T13:30:00Z",
    },
    {
      id: 105,
      title: "Maximum Score Perfect Ten Breakthrough",
      score: 0.99,
      item_count: 8,
      source_count: 4,
      last_seen: "2026-10-29T14:00:00Z",
    },
  ],
  review: [
    { id: 201, title: "Квантовый прорыв: кремниевые кубиты достигли рекордной точности", created_at: "2026-10-29T12:00:00Z" },
    { id: 202, title: "Архитектура нового поколения для распределенного обучения", created_at: "2026-10-29T11:00:00Z" },
  ],
  generating: [
    { id: 205, kind: "digest", title: "Генерация дайджеста ключевых технологий недели", created_at: "2026-10-29T13:00:00Z" },
  ],
  generation_queue: mockGenerationQueue,
  queue: [
    { id: 302, status: "pending", due_at: "2026-10-29T15:00:00Z", title: "Квантовый прорыв" },
  ],
  upcoming: [],
  pipeline: {
    worker_seen_seconds: 5,
    last_ingest_ok: "2026-10-29T13:45:00Z",
    failing_sources: 0,
    rss_enabled: 2,
    jobs_queued: 1,
    jobs_failed_24h: 0,
  },
  llm: {
    calls: 150,
    avg_chat_ms: 1200,
    prompt_tokens: 85000,
    completion_tokens: 12000,
    errors: 0,
    provider: "openai",
    chat_model: "gpt-4o",
    embed_model: "text-embedding-3-small",
  },
};

export interface MockState {
  user: User | null;
  projects: Project[];
  clusters: Cluster[];
  drafts: DraftListItem[];
  generationQueue: GenerationJob[];
  publications: Publication[];
  sources: Source[];
  dashboard: Dashboard;
  postedGenerations: { clusterId: number }[];
  approvedDrafts: number[];
}

export function createMockState(overrides?: Partial<MockState>): MockState {
  return {
    user: mockUser,
    projects: [mockProject],
    clusters: [...mockClusters],
    drafts: [...mockDrafts],
    generationQueue: [...mockGenerationQueue],
    publications: [...mockPublications],
    sources: [...mockSources],
    dashboard: JSON.parse(JSON.stringify(mockDashboard)),
    postedGenerations: [],
    approvedDrafts: [],
    ...overrides,
  };
}

export function setupNetworkMocks(state: MockState) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const urlStr = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const url = new URL(urlStr, "http://localhost");
    const path = url.pathname.replace(/^\/api/, "");
    const method = (init?.method || "GET").toUpperCase();

    // 1. Auth & Projects
    if (path === "/auth/me") {
      return new Response(JSON.stringify(state.user), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (path === "/projects") {
      return new Response(JSON.stringify(state.projects), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (path === "/dashboard") {
      return new Response(JSON.stringify(state.dashboard), { status: 200, headers: { "Content-Type": "application/json" } });
    }

    // 2. Clusters & Trends
    if (path === "/clusters" && method === "GET") {
      return new Response(JSON.stringify(state.clusters), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    const clusterDetailMatch = path.match(/^\/clusters\/(\d+)$/);
    if (clusterDetailMatch && method === "GET") {
      const cId = Number(clusterDetailMatch[1]);
      const found = state.clusters.find((c) => c.id === cId) ?? state.clusters[0];
      const detail: ClusterDetail = {
        ...found,
        project_id: 1,
        interest: found.interest ?? null,
        interest_reason: found.interest_reason ?? null,
        image_item_id: found.image_item_id,
        items: [
          {
            id: 1,
            title: found.title,
            url: "https://example.com/source1",
            published_at: found.first_seen,
            source_name: "Tech News",
            has_image: false,
          },
        ],
        drafts: [],
      };
      return new Response(JSON.stringify(detail), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    const clusterGenMatch = path.match(/^\/clusters\/(\d+)\/generate$/);
    if (clusterGenMatch && method === "POST") {
      const cId = Number(clusterGenMatch[1]);
      state.postedGenerations.push({ clusterId: cId });
      return new Response(JSON.stringify({ job_id: 777 }), { status: 200, headers: { "Content-Type": "application/json" } });
    }

    // 3. Drafts
    if (path === "/drafts" && method === "GET") {
      return new Response(JSON.stringify(state.drafts), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (path === "/generation-queue" && method === "GET") {
      return new Response(JSON.stringify(state.generationQueue), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    const draftDetailMatch = path.match(/^\/drafts\/(\d+)$/);
    if (draftDetailMatch && method === "GET") {
      const dId = Number(draftDetailMatch[1]);
      const found = state.drafts.find((d) => d.id === dId) ?? state.drafts[0];
      const fullDraft: Draft = {
        ...found,
        project_id: 1,
        image_url: null,
        video_url: null,
        video_kind: "video",
        extra_images: [],
        reply_to_draft_id: null,
        body: `Текст поста #${found.id}: ${found.title}`,
        citations: [],
        params: {},
        publications: [],
      };
      return new Response(JSON.stringify(fullDraft), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    const draftApproveMatch = path.match(/^\/drafts\/(\d+)\/approve$/);
    if (draftApproveMatch && method === "POST") {
      const dId = Number(draftApproveMatch[1]);
      state.approvedDrafts.push(dId);
      const draft = state.drafts.find((d) => d.id === dId);
      if (draft) draft.status = "approved";
      return new Response(JSON.stringify({ ok: true, already: false, suppressed: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // 4. Publications
    if (path === "/publications" && method === "GET") {
      return new Response(JSON.stringify(state.publications), { status: 200, headers: { "Content-Type": "application/json" } });
    }

    // 5. Sources
    if (path === "/sources" && method === "GET") {
      return new Response(JSON.stringify(state.sources), { status: 200, headers: { "Content-Type": "application/json" } });
    }

    // Channels
    if (path === "/channels" && method === "GET") {
      return new Response(
        JSON.stringify([
          { id: 1, project_id: 1, type: "telegram", name: "Telegram Channel", config: {}, enabled: true, has_secret: false },
        ]),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }

    // Default fallback
    return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } });
  });
}

export function renderWithProviders(route = "/") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: 0,
        gcTime: 0,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <ToastProvider>
          <AuthProvider>
            <ProjectProvider>
              <App />
            </ProjectProvider>
          </AuthProvider>
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}
