import { describe, expect, it } from "vitest";
import { CHANNEL_TYPES, SCORE_LABELS } from "./labels";

describe("labels", () => {
  it("covers all publication channel types", () => {
    expect(CHANNEL_TYPES).toMatchObject({
      telegram: expect.any(String),
      vk: expect.any(String),
      discord: expect.any(String),
      webhook: expect.any(String),
      console: expect.any(String),
    });
  });

  it("exposes score breakdown labels used on Trends", () => {
    for (const key of ["topic_fit", "coverage", "authority", "freshness", "velocity"]) {
      expect(SCORE_LABELS[key]).toBeTruthy();
    }
  });
});
