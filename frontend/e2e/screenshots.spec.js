// Documentation screenshots: one PNG per screen of the app, taken against
// the same seeded two-hospital stack the E2E journeys use, in either UI
// language (Persian renders right-to-left).
//
//   E2E_SCREENSHOTS=1 SCREENSHOT_LANG=en npx playwright test screenshots
//   E2E_SCREENSHOTS=1 SCREENSHOT_LANG=fa npx playwright test screenshots
//
// Output: docs/screenshots/<lang>/<key>.png (override with SCREENSHOT_DIR).
// docs/USER_GUIDE.md and the DOCX guides (scripts/docs/build_docx.py) embed
// these by key. Skipped in normal E2E runs; run on its own so the seed is
// fresh (pending transfer, inbound ambulance, …). Selectors never depend on
// UI text, so both languages take the same path through the app.
const fs = require("fs");
const path = require("path");
const { test, expect } = require("@playwright/test");
const { state } = require("./helpers");

const LANG = process.env.SCREENSHOT_LANG === "fa" ? "fa" : "en";
const OUT = process.env.SCREENSHOT_DIR || path.join(__dirname, "..", "..", "docs", "screenshots", LANG);

test.describe.configure({ mode: "serial" });
test.skip(!process.env.E2E_SCREENSHOTS, "set E2E_SCREENSHOTS=1 to regenerate documentation screenshots");
test.beforeAll(() => fs.mkdirSync(OUT, { recursive: true }));

const USERS = {
  doctor: () => ["e2e_doctor", state().user_password],
  rad: () => ["e2e_rad", state().user_password],
  admin: () => ["admin", state().admin_password],
};

/** Sign in via the API and preload token, language and theme before any page script runs. */
async function as(page, request, who, { theme = "light" } = {}) {
  const [username, password] = USERS[who]();
  const r = await request.post(`${state().backend}/api/auth/login`, { form: { username, password } });
  expect(r.ok()).toBeTruthy();
  const { access_token: token } = await r.json();
  await page.addInitScript(([t, l, th]) => {
    window.localStorage.setItem("asr.token", t);
    window.localStorage.setItem("asr.lang", l);
    window.localStorage.setItem("theme", th);
  }, [token, LANG, theme]);
  return token;
}

async function snap(page, name) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(400); // let transitions and charts settle
  if (LANG === "fa") await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
  // Grow the viewport to the content height rather than using fullPage, so
  // 100vh elements (sidebar, viewer) fill the whole capture.
  const size = page.viewportSize();
  const h = await page.evaluate(() => document.documentElement.scrollHeight);
  if (h > size.height) {
    await page.setViewportSize({ width: size.width, height: Math.min(h, 4000) });
    await page.waitForTimeout(300);
  }
  await page.screenshot({ path: path.join(OUT, `${name}.png`) });
  await page.setViewportSize(size);
}

async function open(page, url, ready) {
  await page.goto(url);
  await expect(page.locator(ready).first()).toBeVisible();
}

// ------------------------------------------------------------------ sign-in
test("sign-in", async ({ page }) => {
  await page.goto("/login");
  await page.evaluate((l) => localStorage.setItem("asr.lang", l), LANG);
  await page.reload();
  await expect(page.locator(".role-card").first()).toBeVisible();
  await snap(page, "01-login-role");
  await page.locator('[data-role="doctor"]').click();
  await expect(page.getByTestId("admin-creates")).toBeVisible();
  await snap(page, "02-login-credentials");
});

// ------------------------------------------------------------------ reporting workspace
test("reporting workspace", async ({ page, request }) => {
  await as(page, request, "doctor");
  await open(page, "/dictate", "textarea[dir=auto]");
  await page.locator("textarea[dir=auto]").fill(
    "CT chest without contrast. Patchy consolidation in the right lower lobe. No pleural effusion. No pneumothorax.");
  await page.getByTestId("dictate-generate").click();
  await expect(page.getByText("E2E-REPORT").first()).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("save-report").click();
  await snap(page, "10-dictate");
  await open(page, "/radiology", ".sidebar");
  await snap(page, "11-radiology");
  await open(page, "/reports", ".sidebar");
  await expect(page.getByText("E2E-REPORT").first()).toBeVisible();
  await snap(page, "12-reports");
  await open(page, "/templates", "main button.nav-item");
  await page.locator("main button.nav-item", { hasText: "Chest sonography" }).click();
  await expect(page.locator("main .report:not(.empty)")).toBeVisible();
  await snap(page, "13-templates");
});

// ------------------------------------------------------------------ PACS
test("pacs browser", async ({ page, request }) => {
  await as(page, request, "doctor");
  await open(page, "/pacs", '[data-testid="study-table"] tbody tr');
  await page.getByTestId("study-table").locator("tbody tr", { hasText: "CT CHEST" }).click();
  await expect(page.getByTestId("study-panel")).toContainText("Karimi Ali");
  await snap(page, "20-pacs-browser");
});

test("pacs viewer with measurement, AI read and report", async ({ page, request }) => {
  const st = state();
  await as(page, request, "rad");
  await page.goto(`/pacs/viewer?study=${st.ct_uid}`);
  const vp = page.getByTestId("viewport-0");
  await expect(vp).toHaveAttribute("data-loaded", "true", { timeout: 60_000 });
  await page.locator('[data-tool="Length"]').click();
  const box = await vp.boundingBox();
  await page.mouse.move(box.x + box.width * 0.3, box.y + box.height * 0.45);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.62, box.y + box.height * 0.55, { steps: 8 });
  await page.mouse.up();
  await page.locator('[data-tab="measure"]').click();
  await expect(page.getByTestId("measurements")).toContainText(/\d+(\.\d)?\s*mm/);
  await snap(page, "21-pacs-viewer");
  await page.locator('[data-layout="1x2"]').click();
  await expect(page.getByTestId("viewport-1")).toHaveAttribute("data-loaded", "true", { timeout: 60_000 });
  await page.locator('[data-tab="ai"]').click();
  await page.getByTestId("ai-analyze").click();
  await expect(page.getByTestId("ai-result")).toContainText("E2E-REPORT", { timeout: 60_000 });
  await snap(page, "22-pacs-viewer-ai");
  await page.locator('[data-tab="report"]').click();
  await expect(page.getByTestId("report-editor").locator("textarea")).toHaveValue(/E2E-REPORT/);
  await snap(page, "23-pacs-viewer-report");
});

test("pacs worklist, upload, nodes", async ({ page, request }) => {
  const st = state();
  await as(page, request, "admin");
  await open(page, "/pacs/worklist", '[data-testid="worklist-table"]');
  const row = page.getByTestId("worklist-table").locator("tr", { hasText: "CT ABDOMEN" });
  await row.getByTestId("wl-start").click();   // MPPS in progress
  await expect(row).toContainText("in_progress");
  await snap(page, "24-pacs-worklist");
  await open(page, "/pacs/upload", '[data-testid="upload-start"]');
  await page.getByTestId("upload-input").setInputFiles(st.upload_file);
  await page.getByTestId("upload-start").click();
  await expect(page.getByTestId("upload-result")).toContainText("CR CHEST PA", { timeout: 30_000 });
  await snap(page, "25-pacs-upload");
  await open(page, "/pacs/nodes", '[data-testid="nodes-table"] tbody tr');
  await page.getByTestId("nodes-table").getByTestId("node-echo").first().click();
  await expect(page.getByTestId("nodes-table").locator(".pill.ok").first()).toBeVisible({ timeout: 20_000 });
  await snap(page, "26-pacs-nodes");
});

test("pacs devices: compressed colour US, JPEG Lossless CT, SR and PDF", async ({ page, request }) => {
  const st = state();
  await as(page, request, "rad");
  await page.goto(`/pacs/viewer?study=${st.dev_uid}`);
  await expect(page.getByTestId("viewport-0")).toHaveAttribute("data-loaded", "true", { timeout: 60_000 });
  await page.locator('[data-layout="1x2"]').click();
  await expect(page.getByTestId("viewport-1")).toHaveAttribute("data-loaded", "true", { timeout: 60_000 });
  await expect(page.getByTestId("study-objects")).toBeVisible();
  await snap(page, "27-pacs-devices");
  await page.getByTestId("study-objects").locator('[data-object-kind="sr"]').click();
  await expect(page.getByTestId("sr-text")).toContainText("Pneumonia");
  await snap(page, "28-pacs-objects-sr");
});

// ------------------------------------------------------------------ EHR section
test("EHR: patients and registration", async ({ page, request }) => {
  await as(page, request, "doctor");
  await page.goto("/ehr?tab=patients");
  await page.locator("input[name=q]").fill("Farahani");
  await page.getByTestId("patient-search").click();
  await expect(page.getByTestId("patient-results")).toContainText("Reza Farahani");
  await snap(page, "30-ehr-patients");
  await page.getByTestId("register-open").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await snap(page, "31-ehr-register");
});

test("EHR: unified chart tabs", async ({ page, request }) => {
  const st = state();
  await as(page, request, "doctor");
  await page.goto(`/ehr/chart?id=${st.local_person}`);
  await expect(page.getByTestId("chart-name")).toHaveText("Reza Farahani");
  await snap(page, "32-chart-summary");
  await page.getByTestId("toggle-remote").check();
  await expect(page.getByTestId("summary")).toContainText("Iodinated contrast", { timeout: 30_000 });
  await snap(page, "33-chart-with-peer-records");
  for (const [tab, name, ready] of [
    ["results", "34-chart-results", '[data-testid="results-table"] svg.spark'],
    ["imaging", "35-chart-imaging", '[data-testid="imaging-table"]'],
    ["documents", "36-chart-documents", '[data-testid="documents"]'],
  ]) {
    await page.locator(`[data-tab="${tab}"]`).click();
    await expect(page.locator(ready).first()).toBeVisible();
    await snap(page, name);
  }
  await page.locator('[data-tab="orders"]').click();
  await page.getByTestId("order-imaging").click();
  const form = page.getByTestId("add-service-requests");
  await form.locator("input[name=display]").fill("CT Coronary Angiography");
  await form.locator("select").nth(2).selectOption("CT");
  await form.locator("button[type=submit]").click();
  await expect(page.getByTestId("orders")).toContainText(/ACC\S*/);
  await snap(page, "37-chart-orders");
  for (const tab of ["encounters", "medications", "timeline", "access"]) {
    await page.locator(`[data-tab="${tab}"]`).click();
    await snap(page, `38-chart-${tab}`);
  }
});

test("EHR: restricted record (break the glass)", async ({ page, request }) => {
  await as(page, request, "doctor");
  await page.goto(`/ehr/chart?id=${state().vip_person}`);
  await expect(page.getByTestId("restricted")).toBeVisible();
  await snap(page, "39-chart-restricted");
});

test("EHR: transfers, EMS, medication alerts, text intake", async ({ page, request }) => {
  await as(page, request, "doctor");
  await page.goto("/ehr?tab=transfers");
  await expect(page.getByTestId("transfer-requested")).toContainText("E2E Peer Hospital");
  await snap(page, "40-ehr-transfers");
  await page.goto("/ehr?tab=ems");
  await expect(page.getByTestId("ems-board")).toContainText("MEDIC-21");
  await snap(page, "41-ehr-ems");
  await page.goto("/ehr?tab=alerts");
  await page.getByTestId("alerts-check").click();
  await expect(page.getByText("Metformin").first()).toBeVisible({ timeout: 20_000 });
  await snap(page, "42-ehr-alerts");
  await open(page, "/ehr?tab=intake", ".list-row");
  await page.locator(".list-row", { hasText: "Maryam Ahmadi" }).getByRole("button").first().click();
  await expect(page.getByText("Azithromycin").first()).toBeVisible();
  await snap(page, "43-ehr-intake");
});

test("interoperability admin", async ({ page, request }) => {
  await as(page, request, "admin");
  await page.goto("/interop");
  await expect(page.getByTestId("interop-status")).toContainText("E2E General Hospital");
  await page.getByTestId("test-2.25.901").click();
  await expect(page.getByTestId("test-result-2.25.901").locator(".pill.ok")).toHaveCount(3, { timeout: 30_000 });
  await page.getByTestId("hl7-send").click();
  await expect(page.getByTestId("hl7-ack")).toContainText("MSA|AA");
  await snap(page, "45-interop");
});

// ------------------------------------------------------------------ education, models, settings
test("education, models, settings", async ({ page, request }) => {
  await as(page, request, "admin");
  for (const [name, url] of [["50-education", "/education"], ["51-asr", "/asr"],
                             ["52-llm", "/llm"], ["53-vision", "/vision"]]) {
    await open(page, url, ".sidebar");
    await snap(page, name);
  }
  await open(page, "/settings", '[data-testid="users-table"] tbody tr');
  await expect(page.getByTestId("users-table")).toContainText("e2e_doctor");
  await snap(page, "54-settings-users");
});

// ------------------------------------------------------------------ dark theme
test("dark theme", async ({ page, request }) => {
  const st = state();
  await as(page, request, "rad", { theme: "dark" });
  await page.goto(`/pacs/viewer?study=${st.mr_uid}`);
  await expect(page.getByTestId("viewport-0")).toHaveAttribute("data-loaded", "true", { timeout: 60_000 });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await snap(page, "70-dark-viewer");
  await page.goto(`/ehr/chart?id=${st.local_person}`);
  await expect(page.getByTestId("chart-name")).toHaveText("Reza Farahani");
  await snap(page, "71-dark-chart");
});
