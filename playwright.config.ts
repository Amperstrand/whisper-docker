import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  screenshot: "on",
  timeout: 180_000,
  retries: 0,
  use: {
    baseURL: process.env.BASE_URL || "https://listen.silent.energy",
    trace: "on-first-retry",
    serviceWorkers: "block",
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
});
