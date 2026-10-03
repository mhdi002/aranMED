// Browser E2E: the PACS user journey against a live, seeded stack.
const { test, expect } = require("@playwright/test");
const { state, loginAs, canvasStats, shot } = require("./helpers");

test.describe.configure({ mode: "serial" });

test("UI sign-in lands a doctor in the app with Imaging in the nav", async ({ page }) => {
  const st = state();
  await page.goto("/login");
  await page.locator(".role-card").first().click();
  const inputs = page.locator("form.auth-form input.input");
  await inputs.nth(0).fill("e2e_doctor");
  await inputs.nth(1).fill(st.user_password);
  await page.locator("form.auth-form button[type=submit]").click();
  await expect(page.locator(".sidebar")).toContainText("PACS");
  await page.getByRole("button", { name: "PACS" }).click();
  await expect(page).toHaveURL(/\/pacs$/);
  await expect(page.locator("h1")).toHaveText("Imaging Archive (PACS)");
});

test("study browser: stats, filters, details panel", async ({ page, request }) => {
  await loginAs(page, request, "e2e_doctor", state().user_password);
  await page.goto("/pacs");
  const stats = page.getByTestId("pacs-stats");
  // Two studies are seeded (CT + MR); the third arrives in the import test.
  await expect(stats.locator(".stat").first()).toContainText("Studies2");
  await expect(stats.locator(".stat").nth(1)).toContainText("Images32");
  const table = page.getByTestId("study-table");
  await expect(table.locator("tbody tr")).toHaveCount(2);

  await page.locator("input[name=q]").fill("karimi");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table).toContainText("CT CHEST");
  await expect(table).toContainText("E2E-CT-1");

  await page.locator("input[name=q]").fill("");
  await page.locator("select[name=modality]").selectOption("MR");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table).toContainText("MRI BRAIN");

  await table.locator("tbody tr").first().click();
  const panel = page.getByTestId("study-panel");
  await expect(panel).toContainText("Rahimi Neda");
  await expect(panel).toContainText("MR #1");
  await expect(panel).toContainText("8 img");
  await shot(page, "pacs-browser");
});

test("viewer renders real pixels; W/L presets, scrolling and measurements work", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "e2e_rad", st.user_password);
  await page.goto(`/pacs/viewer?study=${st.ct_uid}`);
  await expect(page.getByTestId("viewer-title")).toContainText("Karimi Ali");
  const vp = page.getByTestId("viewport-0");
  await expect(vp).toHaveAttribute("data-loaded", "true", { timeout: 60_000 });
  await expect(page.getByTestId("overlay-0")).toContainText("W 400 · L 40");

  await expect.poll(async () => (await canvasStats(page))?.lit ?? 0).toBeGreaterThan(0.02);
  const before = await canvasStats(page);
  expect(before.w).toBeGreaterThan(100);

  // Lung window: overlay and pixels both change.
  await page.getByTestId("wl-preset").selectOption("lung");
  await expect(page.getByTestId("overlay-0")).toContainText("W 1500 · L -600");
  await expect.poll(async () => (await canvasStats(page)).hash).not.toBe(before.hash);
  const lung = await canvasStats(page);
  expect(lung.mean).toBeGreaterThan(before.mean); // background ramp becomes visible

  // Stack scrolling with the keyboard moves through the series.
  const tl = vp.locator("xpath=..").locator(".vp-ov.tl");
  await expect(tl).toContainText("Im 7/12");
  await vp.click();
  await page.keyboard.press("ArrowDown");
  await expect(tl).toContainText("Im 8/12");

  // Length measurement by dragging on the image.
  await page.locator('[data-tool="Length"]').click();
  const box = await vp.boundingBox();
  await page.mouse.move(box.x + box.width * 0.3, box.y + box.height * 0.5);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.5, { steps: 8 });
  await page.mouse.up();
  await page.locator('[data-tab="measure"]').click();
  await expect(page.getByTestId("measurements")).toContainText(/Length\s+\d+(\.\d)?\s*mm/);
  await shot(page, "pacs-viewer");

  // Layout switch shows a second series in another viewport.
  await page.locator('[data-layout="1x2"]').click();
  await expect(page.getByTestId("viewport-1")).toHaveAttribute("data-loaded", "true", { timeout: 60_000 });
  await expect(page.getByTestId("viewport-0")).not.toHaveAttribute(
    "data-series", await page.getByTestId("viewport-1").getAttribute("data-series"));
});

test("AI draft read then radiologist sign-off from the viewer", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "e2e_rad", st.user_password);
  await page.goto(`/pacs/viewer?study=${st.ct_uid}`);
  await page.locator('[data-tab="ai"]').click();
  await page.getByTestId("ai-analyze").click();
  await expect(page.getByTestId("ai-result")).toContainText("E2E-REPORT", { timeout: 60_000 });

  await page.locator('[data-tab="report"]').click();
  const editor = page.getByTestId("report-editor");
  await expect(editor).toContainText("draft");
  await expect(editor.locator("textarea")).toHaveValue(/E2E-REPORT/);
  await editor.locator("textarea").fill("CT CHEST\nIMPRESSION: Right lower lobe pneumonia. Signed in E2E.");
  await page.getByTestId("sign-report").click();
  await expect(editor).toContainText("final");
  await expect(page.getByTestId("viewer-title")).toContainText("reported");
  await shot(page, "pacs-report");
});

test("import a DICOM file and open it", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "e2e_doctor", st.user_password);
  await page.goto("/pacs/upload");
  await page.getByTestId("upload-input").setInputFiles(st.upload_file);
  await page.getByTestId("upload-start").click();
  const res = page.getByTestId("upload-result");
  await expect(res).toContainText("1 stored");
  await expect(res).toContainText("CR CHEST PA");
  await res.getByRole("button", { name: "Open viewer" }).click();
  await expect(page).toHaveURL(new RegExp(st.upload_uid));
  await expect(page.getByTestId("viewport-0")).toHaveAttribute("data-loaded", "true", { timeout: 60_000 });
});

test("worklist: seeded STAT order and scheduling a new one", async ({ page, request }) => {
  await loginAs(page, request, "e2e_doctor", state().user_password);
  await page.goto("/pacs/worklist");
  const table = page.getByTestId("worklist-table");
  await expect(table).toContainText("CT ABDOMEN");
  await expect(table).toContainText("STAT");
  await page.getByTestId("wl-new").click();
  const dlg = page.getByRole("dialog");
  await dlg.locator("input").nth(0).fill("Moradi^Reza");
  await dlg.locator("input").nth(1).fill("WL-77");
  await dlg.locator("select").nth(1).selectOption("MR");
  await dlg.locator("input").nth(3).fill("MRI Knee");
  await dlg.getByRole("button", { name: "Add to worklist" }).click();
  await expect(table).toContainText("MRI Knee");
  const row = table.locator("tr", { hasText: "MRI Knee" });
  await row.getByRole("button", { name: "Start" }).click();
  await expect(row).toContainText("in_progress");
});

test("admin: DICOM nodes with a live C-ECHO; doctors cannot see the page", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "admin", st.admin_password);
  await page.goto("/pacs/nodes");
  const table = page.getByTestId("nodes-table");
  const row = table.locator("tr", { hasText: "Self (loopback SCP)" });
  await row.getByRole("button", { name: "Echo" }).click();
  await expect(row).toContainText("ok", { timeout: 20_000 });
  await page.getByTestId("node-add").click();
  const dlg = page.getByRole("dialog");
  await dlg.locator("input").nth(0).fill("Peer DICOMweb");
  await dlg.locator("select").first().selectOption("dicomweb");
  await dlg.locator("input").nth(1).fill("http://127.0.0.1:9/dicom-web");
  await dlg.getByRole("button", { name: "Save node" }).click();
  await expect(table).toContainText("Peer DICOMweb");

  // Node administration is admin-only: a doctor is redirected away.
  const ctx = await page.context().browser().newContext();
  const doc = await ctx.newPage();
  await loginAs(doc, request, "e2e_doctor", st.user_password);
  await doc.goto(`${test.info().project.use.baseURL}/pacs/nodes`);
  await expect(doc).not.toHaveURL(/\/pacs\/nodes/);
  await expect(doc.locator(".sidebar")).not.toContainText("DICOM Nodes");
  await ctx.close();
});

test("Persian UI switches to RTL with translated imaging nav", async ({ page, request }) => {
  await loginAs(page, request, "e2e_doctor", state().user_password);
  await page.goto("/pacs");
  await page.locator(".lang-pick button", { hasText: "FA" }).click();
  await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
  await expect(page.locator(".sidebar")).toContainText("آرشیو تصاویر (PACS)");
  await expect(page.locator(".page-title")).toHaveText("آرشیو تصاویر پزشکی");
  await shot(page, "pacs-fa");
});
