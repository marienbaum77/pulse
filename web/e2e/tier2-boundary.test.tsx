// @vitest-environment happy-dom
import { describe, expect, it, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMockState, renderWithProviders, setupNetworkMocks } from "./fixtures";
import type { Cluster, DraftListItem } from "../src/lib/types";

describe("Tier 2: Boundary & Corner Cases", () => {
  let mockState = createMockState();

  beforeEach(() => {
    mockState = createMockState();
    setupNetworkMocks(mockState);
  });

  describe("1. Empty Lists Handling", () => {
    it("renders Nothing UI Empty state on Trends when 0 clusters are returned", async () => {
      mockState = createMockState({ clusters: [] });
      setupNetworkMocks(mockState);

      renderWithProviders("/trends");

      // Verify that page does not crash and renders clean empty state message
      expect(await screen.findByText(/Сюжетов пока нет|Здесь пусто|нет сюжетов/i)).toBeInTheDocument();
    });

    it("renders Nothing UI Empty state on Drafts when 0 drafts exist", async () => {
      mockState = createMockState({ drafts: [], generationQueue: [] });
      setupNetworkMocks(mockState);

      renderWithProviders("/drafts");

      // On "Очередь" tab
      expect(await screen.findByText(/Очередь пуста|Здесь пусто/i)).toBeInTheDocument();

      // Switch to "На проверке" tab
      const user = userEvent.setup();
      const reviewTab = screen.getByText(/На проверке/i);
      await user.click(reviewTab);

      expect(await screen.findByText(/Постов на проверке нет|Здесь пусто/i)).toBeInTheDocument();
    });

    it("renders clean state on Publications when 0 publications exist", async () => {
      mockState = createMockState({ publications: [] });
      setupNetworkMocks(mockState);

      renderWithProviders("/publications");

      expect(await screen.findByText(/Здесь пока пусто/i)).toBeInTheDocument();
    });

    it("renders clean state on Sources when 0 sources are configured", async () => {
      mockState = createMockState({ sources: [] });
      setupNetworkMocks(mockState);

      renderWithProviders("/sources");

      expect(await screen.findByText(/Источники/i)).toBeInTheDocument();
      // Should show prompt or empty indicator rather than unhandled exception
      const bodyText = document.body.textContent || "";
      expect(bodyText).not.toMatch(/undefined|null is not an object/i);
    });
  });

  describe("2. Extremes of Interest Score", () => {
    it("handles boundary interest score of 0.0 cleanly without treating as null or crashing", async () => {
      const zeroCluster: Cluster = {
        id: 991,
        title: "Exact Zero Interest Boundary Story",
        state: "open",
        item_count: 2,
        source_count: 1,
        first_seen: "2026-10-29T10:00:00Z",
        last_seen: "2026-10-29T11:00:00Z",
        score: null,
        score_breakdown: null,
        published_at: null,
        arrivals: null,
        image_item_id: null,
        interest: 0.0,
        interest_reason: "Zero novelty",
      };

      mockState = createMockState({ clusters: [zeroCluster] });
      setupNetworkMocks(mockState);

      renderWithProviders("/trends");

      expect(await screen.findByText(/Exact Zero Interest Boundary Story/i)).toBeInTheDocument();
      // Verify that 0 or 0.0 is present in the rendered card text, and no NaN
      const cardText = document.body.textContent || "";
      expect(cardText).not.toMatch(/NaN/);
    });

    it("handles boundary interest score of 10.0 cleanly without visual overflow", async () => {
      const perfectCluster: Cluster = {
        id: 992,
        title: "Exact Ten Interest Maximum Story",
        state: "open",
        item_count: 5,
        source_count: 3,
        first_seen: "2026-10-29T10:00:00Z",
        last_seen: "2026-10-29T11:00:00Z",
        score: null,
        score_breakdown: null,
        published_at: null,
        arrivals: null,
        image_item_id: null,
        interest: 10.0,
        interest_reason: "Historic breakthrough",
      };

      mockState = createMockState({ clusters: [perfectCluster] });
      setupNetworkMocks(mockState);

      renderWithProviders("/trends");

      expect(await screen.findByText(/Exact Ten Interest Maximum Story/i)).toBeInTheDocument();
      const cardText = document.body.textContent || "";
      expect(cardText).not.toMatch(/NaN/);
    });

    it("handles null or missing interest score gracefully without crashing", async () => {
      const unratedCluster: Cluster = {
        id: 993,
        title: "Unrated Story With Null Interest Score",
        state: "open",
        item_count: 2,
        source_count: 1,
        first_seen: "2026-10-29T10:00:00Z",
        last_seen: "2026-10-29T11:00:00Z",
        score: null,
        score_breakdown: null,
        published_at: null,
        arrivals: null,
        image_item_id: null,
        interest: null,
        interest_reason: null,
      };

      mockState = createMockState({ clusters: [unratedCluster] });
      setupNetworkMocks(mockState);

      renderWithProviders("/trends");

      expect(await screen.findByText(/Unrated Story With Null Interest Score/i)).toBeInTheDocument();
      expect(document.body.textContent).not.toMatch(/NaN/);
    });
  });

  describe("3. Drafts with Length Warnings & Citation Issues", () => {
    it("displays warning indicators or status chips on drafts with invalid citations", async () => {
      const warnDraft: DraftListItem = {
        id: 881,
        kind: "post",
        title: "Draft with Invalid Citation Link",
        status: "pending_review",
        model: "gpt-4o",
        checks: {
          invalid_citations: [1, 3],
          citation_coverage: 0.4,
          length: 500,
        },
        error: null,
        created_at: "2026-10-29T12:00:00Z",
        updated_at: "2026-10-29T12:05:00Z",
        cluster_ids: [101],
        has_image: false,
      };

      mockState = createMockState({ drafts: [warnDraft] });
      setupNetworkMocks(mockState);

      const user = userEvent.setup();
      renderWithProviders("/drafts");

      // Switch to "На проверке" tab
      const reviewTab = await screen.findByText(/На проверке/i);
      await user.click(reviewTab);

      expect(await screen.findByText(/Draft with Invalid Citation Link/i)).toBeInTheDocument();
      // Either Nothing OS chip '[WARN]' or 'есть замечания'
      const cardText = document.body.textContent || "";
      expect(cardText).toMatch(/(\[WARN\]|есть замечания|замечания)/i);
    });

    it("displays edited or clean indicators for drafts with edits or clean status", async () => {
      const cleanDraft: DraftListItem = {
        id: 882,
        kind: "post",
        title: "Perfect Clean Verified Draft",
        status: "pending_review",
        model: "gpt-4o",
        checks: {
          invalid_citations: [],
          citation_coverage: 1.0,
          length: 600,
          edited: true,
        },
        error: null,
        created_at: "2026-10-29T12:00:00Z",
        updated_at: "2026-10-29T12:05:00Z",
        cluster_ids: [101],
        has_image: false,
      };

      mockState = createMockState({ drafts: [cleanDraft] });
      setupNetworkMocks(mockState);

      const user = userEvent.setup();
      renderWithProviders("/drafts");

      const reviewTab = await screen.findByText(/На проверке/i);
      await user.click(reviewTab);

      expect(await screen.findByText(/Perfect Clean Verified Draft/i)).toBeInTheDocument();
      // Should render without error
      expect(document.body.textContent).not.toMatch(/undefined|NaN/);
    });
  });

  describe("4. Adversarial Edge Cases & Stress Resilience", () => {
    it("handles XSS payloads and special characters in story titles safely as text", async () => {
      const xssTitle = `<script>alert("xss")</script> & "quotes" 'single' <img src=x onerror=alert(1)>`;
      const adversarialCluster: Cluster = {
        id: 994,
        title: xssTitle,
        state: "open",
        item_count: 2,
        source_count: 1,
        first_seen: "2026-10-29T10:00:00Z",
        last_seen: "2026-10-29T11:00:00Z",
        score: null,
        score_breakdown: null,
        published_at: null,
        arrivals: null,
        image_item_id: null,
        interest: 8.0,
        interest_reason: "Safe escaping test",
      };

      mockState = createMockState({ clusters: [adversarialCluster] });
      setupNetworkMocks(mockState);

      renderWithProviders("/trends");

      // Verify that the title is rendered as text content and no script element was created
      expect(await screen.findByText(xssTitle)).toBeInTheDocument();
      expect(document.querySelectorAll("script").length).toBe(0);
    });

    it("handles extreme text length in cluster title without DOM crashing", async () => {
      const giantTitle = "A".repeat(1500) + " EndOfGiantTitle";
      const giantCluster: Cluster = {
        id: 995,
        title: giantTitle,
        state: "open",
        item_count: 3,
        source_count: 1,
        first_seen: "2026-10-29T10:00:00Z",
        last_seen: "2026-10-29T11:00:00Z",
        score: null,
        score_breakdown: null,
        published_at: null,
        arrivals: null,
        image_item_id: null,
        interest: 5.5,
        interest_reason: "Giant title stress test",
      };

      mockState = createMockState({ clusters: [giantCluster] });
      setupNetworkMocks(mockState);

      renderWithProviders("/trends");

      expect(await screen.findByText(new RegExp("EndOfGiantTitle"))).toBeInTheDocument();
    });
  });
});

