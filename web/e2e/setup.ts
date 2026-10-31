import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

// Mock EventSource for useLiveEvents
if (typeof window !== "undefined") {
  class MockEventSource {
    onopen: ((event: Event) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    close() {}
    constructor(_url: string) {
      setTimeout(() => {
        if (this.onopen) this.onopen(new Event("open"));
      }, 0);
    }
  }
  (window as unknown as { EventSource: unknown }).EventSource = MockEventSource;

  // Mock window.scrollTo
  window.scrollTo = vi.fn();

  // Mock matchMedia
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
  }

  // Mock window.confirm
  window.confirm = vi.fn(() => true);
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("pulse.project", "1");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
