// @vitest-environment happy-dom
import { describe, expect, it, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMockState, renderWithProviders, setupNetworkMocks } from "./fixtures";

describe("Tier 4: Real-World Editorial Workflows", () => {
  let mockState = createMockState();

  beforeEach(() => {
    mockState = createMockState();
    setupNetworkMocks(mockState);
  });

  it("completes full editorial flow: discover top story by interest -> generate post -> 1-click approve -> verify publication", async () => {
    const user = userEvent.setup();

    // 1. Discover top story on Trends
    renderWithProviders("/trends");

    // The story with interest 9.8 is discovered
    const topStoryTitle = await screen.findByText(/Quantum Computing Breakthrough in Silicon/i);
    expect(topStoryTitle).toBeInTheDocument();

    // 2. Generate post via inline "+ Создать пост" on the trend card
    const generateButtons = await screen.findAllByRole("button", { name: /(\+ Создать пост|\+ Пост|Создать пост)/i });
    expect(generateButtons.length).toBeGreaterThan(0);
    await user.click(generateButtons[0]);

    // Verify network call was made for generation
    await waitFor(() => {
      expect(mockState.postedGenerations.length).toBeGreaterThan(0);
    });

    // 3. Move to Drafts page to inspect pending draft
    renderWithProviders("/drafts");

    // Click "На проверке" tab
    const reviewTab = await screen.findByText(/На проверке/i);
    await user.click(reviewTab);

    // The draft corresponding to the top story is visible
    const pendingDraft = await screen.findByText(/Квантовый прорыв: кремниевые кубиты достигли рекордной точности/i);
    expect(pendingDraft).toBeInTheDocument();

    // 4. 1-Click Approve the draft
    const [approveBtn] = await screen.findAllByRole("button", { name: /(✓|опубликовать|утвердить)/i });
    await user.click(approveBtn);

    // Verify draft approval API was called
    await waitFor(() => {
      expect(mockState.approvedDrafts).toContain(201);
    });

    // 5. Navigate to Publications and verify the status
    renderWithProviders("/publications");

    // The publication record for the approved post is in the list
    expect(await screen.findByText(/Квантовый прорыв: кремниевые кубиты/i)).toBeInTheDocument();
  });
});
