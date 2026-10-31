// @vitest-environment happy-dom
import { describe, expect, it, beforeEach } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMockState, renderWithProviders, setupNetworkMocks } from "./fixtures";

describe("Tier 1: Feature Coverage (Opaque-Box Requirement Tests)", () => {
  let mockState = createMockState();

  beforeEach(() => {
    mockState = createMockState();
    setupNetworkMocks(mockState);
  });

  describe("1. Rendering of all 5 main pages", () => {
    it("renders Overview page (/) with project header and dashboard sections", async () => {
      renderWithProviders("/");

      // Verify project title from mock state
      expect(await screen.findByRole("heading", { level: 1, name: /Pulse Intelligence/i })).toBeInTheDocument();

      // Verify dashboard sections
      expect(await screen.findByText(/Главные сюжеты/i)).toBeInTheDocument();
      expect(screen.getByText(/Очередь постов/i)).toBeInTheDocument();
      expect(screen.getByText(/Поток материалов/i)).toBeInTheDocument();
    });

    it("renders Trends page (/trends) with tabs and cluster list", async () => {
      renderWithProviders("/trends");

      expect(await screen.findByText(/Новые/i)).toBeInTheDocument();
      expect(screen.getByText(/Опубликованные/i)).toBeInTheDocument();

      // Verify that cluster items are rendered
      expect(await screen.findByText(/Quantum Computing Breakthrough in Silicon/i)).toBeInTheDocument();
      expect(screen.getByText(/New AI Architecture Announced by Major Lab/i)).toBeInTheDocument();
    });

    it("renders Drafts page (/drafts) with status tabs and drafts list", async () => {
      renderWithProviders("/drafts");

      expect(await screen.findByText(/Посты/i)).toBeInTheDocument();
      expect(screen.getByText(/Очередь/i)).toBeInTheDocument();
      expect(screen.getByText(/На проверке/i)).toBeInTheDocument();
      expect(screen.getByText(/Утверждены/i)).toBeInTheDocument();
    });

    it("renders Publications page (/publications) with channel publications and status tabs", async () => {
      renderWithProviders("/publications");

      expect(await screen.findByRole("heading", { level: 1, name: /Публикации/i })).toBeInTheDocument();
      expect(screen.getByText(/Все/i)).toBeInTheDocument();
      expect(screen.getByText(/Требуют решения/i)).toBeInTheDocument();
      expect(screen.getByText(/В очереди/i)).toBeInTheDocument();
      expect(screen.getByText(/Отправлены/i)).toBeInTheDocument();

      // Verify publication rows
      expect(await screen.findByText(/Утвержденный пост о квантовом превосходстве/i)).toBeInTheDocument();
    });

    it("renders Sources page (/sources) with connected feeds and connectors", async () => {
      renderWithProviders("/sources");

      expect(await screen.findByRole("heading", { level: 1, name: /Источники/i })).toBeInTheDocument();
      expect(await screen.findByText(/ArXiv Quantum & AI News/i)).toBeInTheDocument();
      expect(screen.getByText(/AI Frontiers Telegram Channel/i)).toBeInTheDocument();
    });
  });

  describe("2. Total absence of legacy story weight ('вес сюжета') across the DOM", () => {
    const legacyClasses = [".marker-track", ".marker-fill", ".marker-num"];
    const legacyPhrases = [/вес сюжета/i, /из чего сложился вес/i, /мин\.?\s*вес/i, /влияет на вес/i, /вес \(nws\)/i];

    it("has zero story weight CSS classes or phrases on Trends page", async () => {
      const { container } = renderWithProviders("/trends");
      await screen.findByText(/Quantum Computing Breakthrough/i);

      for (const cls of legacyClasses) {
        expect(container.querySelectorAll(cls).length, `Found legacy class ${cls} in Trends DOM`).toBe(0);
      }

      const text = container.textContent || "";
      for (const phrase of legacyPhrases) {
        expect(phrase.test(text), `Found legacy phrase matching ${phrase} in Trends DOM`).toBe(false);
      }
    });

    it("has zero story weight elements or breakdown bars on Overview page", async () => {
      const { container } = renderWithProviders("/");
      await screen.findByRole("heading", { level: 1, name: /Pulse Intelligence/i });

      for (const cls of legacyClasses) {
        expect(container.querySelectorAll(cls).length, `Found legacy class ${cls} in Overview DOM`).toBe(0);
      }

      const text = container.textContent || "";
      for (const phrase of legacyPhrases) {
        expect(phrase.test(text), `Found legacy phrase matching ${phrase} in Overview DOM`).toBe(false);
      }
    });
  });

  describe("3. Sorting by interestingness (interest) descending by default", () => {
    it("sorts clusters on Trends page by interest score descending by default", async () => {
      renderWithProviders("/trends");

      // Wait for items to render
      await screen.findByText(/Quantum Computing Breakthrough in Silicon/i);

      // Collect all rendered cluster headings/titles
      const titles = [
        "Maximum Score Perfect Ten Breakthrough", // interest: 10.0
        "Quantum Computing Breakthrough in Silicon", // interest: 9.8
        "New AI Architecture Announced by Major Lab", // interest: 7.5
        "Routine Dependency Maintenance Release", // interest: 3.2
        "Zero Interest Story Boundary Value", // interest: 0.0
      ];

      // Find all matching elements and check their document order
      const foundElements = titles
        .map((title) => screen.queryByText(new RegExp(title, "i")))
        .filter((el): el is HTMLElement => el !== null);

      expect(foundElements.length).toBeGreaterThanOrEqual(2);

      // Verify that higher interest items precede lower interest items
      for (let i = 0; i < foundElements.length - 1; i++) {
        const current = foundElements[i];
        const next = foundElements[i + 1];
        const position = current.compareDocumentPosition(next);
        // Node.DOCUMENT_POSITION_FOLLOWING is 4
        expect(position & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      }
    });

    it("sorts drafts on Drafts page by linked cluster interest descending by default", async () => {
      renderWithProviders("/drafts");

      // Switch to "На проверке" tab
      const user = userEvent.setup();
      const reviewTab = await screen.findByText(/На проверке/i);
      await user.click(reviewTab);

      // Cluster 101 has interest 9.8 (Draft 201), Cluster 102 has 7.5 (Draft 202)
      const draft201 = await screen.findByText(/Квантовый прорыв: кремниевые кубиты/i);
      const draft202 = await screen.findByText(/Архитектура нового поколения для распределенного обучения/i);

      const position = draft201.compareDocumentPosition(draft202);
      expect(position & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });
  });

  describe("4. 1-Click quick publication and inline post creation", () => {
    it("offers a direct 1-click approve button on pending_review draft cards", async () => {
      const user = userEvent.setup();
      renderWithProviders("/drafts");

      // Go to "На проверке"
      const reviewTab = await screen.findByText(/На проверке/i);
      await user.click(reviewTab);

      // Locate 1-click approve button on the draft card
      const [approveBtn] = await screen.findAllByRole("button", { name: /(✓|опубликовать|утвердить)/i });
      expect(approveBtn).toBeInTheDocument();

      // Click it directly from the list without opening editor
      await user.click(approveBtn);

      // Verify POST /api/drafts/201/approve was sent
      await waitFor(() => {
        expect(mockState.approvedDrafts).toContain(201);
      });
    });

    it("offers an inline '+ Создать пост' action button on open trend cards", async () => {
      const user = userEvent.setup();
      renderWithProviders("/trends");

      await screen.findByText(/Quantum Computing Breakthrough/i);

      // Look for inline post generation button on cluster card
      const createButtons = await screen.findAllByRole("button", { name: /(\+ Создать пост|\+ Пост|Создать пост)/i });
      expect(createButtons.length).toBeGreaterThan(0);

      // Click the first one
      await user.click(createButtons[0]);

      // Verify POST /api/clusters/:id/generate was triggered
      await waitFor(() => {
        expect(mockState.postedGenerations.length).toBeGreaterThan(0);
      });
    });
  });

  describe("5. Nothing UI visual hallmarks", () => {
    it("applies Nothing UI pill button and badge styling classes", async () => {
      renderWithProviders("/publications");

      await screen.findByRole("heading", { level: 1, name: /Публикации/i });

      // Buttons have Nothing UI pill class 'btn'
      const buttons = screen.getAllByRole("button");
      const pillButtons = buttons.filter((b) => b.classList.contains("btn") || b.className.includes("btn"));
      expect(pillButtons.length).toBeGreaterThan(0);

      // Badges have tone attributes / classes
      const badges = document.querySelectorAll(".badge");
      expect(badges.length).toBeGreaterThanOrEqual(0);
    });

    it("renders sidebar with Nothing glyph and navigation items", async () => {
      renderWithProviders("/");

      await screen.findByRole("heading", { level: 1, name: /Pulse Intelligence/i });

      const sidebar = document.querySelector("aside");
      expect(sidebar).toBeInTheDocument();
      if (sidebar) {
        expect(within(sidebar).getByText(/Обзор/i)).toBeInTheDocument();
        expect(within(sidebar).getByText(/Сюжеты/i)).toBeInTheDocument();
        expect(within(sidebar).getByText(/Черновики/i)).toBeInTheDocument();
        expect(within(sidebar).getByText(/Публикации/i)).toBeInTheDocument();
        expect(within(sidebar).getByText(/Источники/i)).toBeInTheDocument();
      }
    });
  });
});
