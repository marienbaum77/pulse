// @vitest-environment happy-dom
import { describe, expect, it, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMockState, renderWithProviders, setupNetworkMocks } from "./fixtures";

describe("Tier 3: Cross-Feature Combinations", () => {
  let mockState = createMockState();

  beforeEach(() => {
    mockState = createMockState();
    setupNetworkMocks(mockState);
  });

  describe("1. Switching Tabs Across Pages", () => {
    it("switches tabs on Trends between 'Новые' and 'Опубликованные'", async () => {
      const user = userEvent.setup();
      renderWithProviders("/trends");

      // Initially on 'Новые'
      expect(await screen.findByText(/Quantum Computing Breakthrough in Silicon/i)).toBeInTheDocument();
      expect(screen.queryByText(/Already Published Milestone Post/i)).not.toBeInTheDocument();

      // Click "Опубликованные"
      const publishedTab = screen.getByText(/Опубликованные/i);
      await user.click(publishedTab);

      // Now published cluster should appear and open clusters disappear
      expect(await screen.findByText(/Already Published Milestone Post/i)).toBeInTheDocument();
      expect(screen.queryByText(/Quantum Computing Breakthrough in Silicon/i)).not.toBeInTheDocument();
    });

    it("switches tabs on Drafts between 'Очередь', 'На проверке', and 'Утверждены'", async () => {
      const user = userEvent.setup();
      renderWithProviders("/drafts");

      // On 'Очередь' tab initially
      expect((await screen.findAllByText(/Фоновая генерация по новому сюжету|Генерация дайджеста/i)).length).toBeGreaterThan(0);

      // Switch to 'На проверке'
      const reviewTab = screen.getByText(/На проверке/i);
      await user.click(reviewTab);

      expect(await screen.findByText(/Квантовый прорыв: кремниевые кубиты/i)).toBeInTheDocument();

      // Switch to 'Утверждены'
      const approvedTab = screen.getByText(/Утверждены/i);
      await user.click(approvedTab);

      expect(await screen.findByText(/Утвержденный пост о квантовом превосходстве/i)).toBeInTheDocument();
      expect(screen.queryByText(/Квантовый прорыв: кремниевые кубиты/i)).not.toBeInTheDocument();
    });

    it("switches tabs on Publications between 'Все' and 'Отправлены'", async () => {
      const user = userEvent.setup();
      renderWithProviders("/publications");

      // On 'Все' tab
      expect(await screen.findByText(/Утвержденный пост о квантовом превосходстве/i)).toBeInTheDocument();
      expect(screen.getByText(/Архитектура нового поколения/i)).toBeInTheDocument();

      // Switch to 'Отправлены'
      const sentTab = screen.getByText(/Отправлены/i);
      await user.click(sentTab);

      expect(await screen.findByText(/Утвержденный пост о квантовом превосходстве/i)).toBeInTheDocument();
      expect(screen.queryByText(/Архитектура нового поколения/i)).not.toBeInTheDocument();
    });
  });

  describe("2. Filtering with Interest Sorting", () => {
    it("filters clusters by search text while maintaining interest-driven layout", async () => {
      const user = userEvent.setup();
      renderWithProviders("/trends");

      await screen.findByText(/Quantum Computing Breakthrough in Silicon/i);

      // Find search input
      const searchInput = screen.getByPlaceholderText(/поиск/i) || screen.getByRole("searchbox") || screen.getByRole("textbox");
      await user.type(searchInput, "Silicon");

      // Only Silicon cluster should remain visible
      await waitFor(() => {
        expect(screen.getByText(/Quantum Computing Breakthrough in Silicon/i)).toBeInTheDocument();
        expect(screen.queryByText(/New AI Architecture Announced/i)).not.toBeInTheDocument();
        expect(screen.queryByText(/Routine Dependency Maintenance/i)).not.toBeInTheDocument();
      });
    });
  });

  describe("3. Quick Approval with Live Feedback", () => {
    it("approves pending draft and provides immediate user feedback", async () => {
      const user = userEvent.setup();
      renderWithProviders("/drafts");

      // Go to 'На проверке'
      const reviewTab = await screen.findByText(/На проверке/i);
      await user.click(reviewTab);

      // Look for approve button (either 1-click on card or inside editor)
      const [approveBtn] = await screen.findAllByRole("button", { name: /(✓|опубликовать|утвердить)/i });
      await user.click(approveBtn);

      await waitFor(() => {
        expect(mockState.approvedDrafts.length).toBeGreaterThan(0);
      });
    });
  });
});
