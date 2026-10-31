import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "happy-dom",
    globals: true,
    setupFiles: ["./e2e/setup.ts"],
    include: ["src/**/*.test.ts", "e2e/**/*.test.{ts,tsx}"],
  },
});
