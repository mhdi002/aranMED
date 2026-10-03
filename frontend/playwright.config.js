// Browser E2E for the clinical UI. `npm run test:e2e` starts a complete
// seeded stack (tests/e2e/stack.py: fake LLM + backend + DICOM SCP + seed +
// frontend) and drives it with Chromium.
const { defineConfig } = require("@playwright/test");

const FRONT = process.env.E2E_FRONTEND_PORT || "3100";
const PY = process.env.E2E_PYTHON || "../.venv/bin/python";

module.exports = defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${FRONT}`,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: process.env.E2E_CHROMIUM ? { executablePath: process.env.E2E_CHROMIUM } : {},
  },
  webServer: {
    command: `${PY} ../tests/e2e/stack.py`,
    url: `http://127.0.0.1:${FRONT}/login`,
    timeout: 240_000,
    reuseExistingServer: false,
    stdout: "pipe",
    stderr: "pipe",
  },
});
