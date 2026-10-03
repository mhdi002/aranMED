// Browser E2E: the clinical EHR, cross-hospital records, transfers, EMS and
// interop admin — against a live stack with a real second (peer) hospital.
const { test, expect } = require("@playwright/test");
const { state, loginAs, shot } = require("./helpers");

test.describe.configure({ mode: "serial" });

test("find a patient and open the unified chart", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "e2e_doctor", st.user_password);
  await page.goto("/clinical");
  await page.locator("input[name=q]").fill("L-1001");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const results = page.getByTestId("patient-results");
  await expect(results).toContainText("Reza Farahani");
  await results.getByRole("button", { name: "Open chart" }).first().click();
  await expect(page.getByTestId("chart-name")).toHaveText("Reza Farahani");
  const summary = page.getByTestId("summary");
  await expect(summary).toContainText("Coronary artery disease");
  await expect(summary).toContainText("Creatinine");             // abnormal lab
  await expect(summary).not.toContainText("Iodinated contrast"); // held at the peer only
});

test("records from the peer hospital appear with their source; imaging is retrieved on demand", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "e2e_doctor", st.user_password);
  await page.goto(`/clinical/chart?id=${st.local_person}`);
  await page.getByTestId("toggle-remote").check();
  const summary = page.getByTestId("summary");
  await expect(summary).toContainText("Iodinated contrast", { timeout: 30_000 });
  await expect(summary.locator(".src-badge.remote", { hasText: "E2E Peer Hospital" }).first()).toBeVisible();
  await expect(summary).toContainText("Clopidogrel");
  await shot(page, "chart-remote");

  await page.locator('[data-tab="imaging"]').click();
  const imaging = page.getByTestId("imaging-table");
  await expect(imaging).toContainText("CT CORONARY");
  await imaging.getByTestId("retrieve-remote").click();
  await expect(imaging.getByRole("button", { name: "Open viewer" })).toBeVisible({ timeout: 60_000 });
  await imaging.getByRole("button", { name: "Open viewer" }).click();
  await expect(page.getByTestId("viewport-0")).toHaveAttribute("data-loaded", "true", { timeout: 60_000 });
});

test("results trend, documents, adding data and CCD export", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "e2e_doctor", st.user_password);
  await page.goto(`/clinical/chart?id=${st.local_person}`);
  await page.locator('[data-tab="results"]').click();
  const results = page.getByTestId("results-table");
  await expect(results).toContainText("1.8 mg/dL");
  await expect(results.locator("svg.spark")).toHaveCount(1);
  await page.locator('[data-tab="documents"]').click();
  await page.getByTestId("documents").getByRole("button", { name: "View" }).first().click();
  await expect(page.getByTestId("doc-content")).toContainText("Stable angina");
  await page.keyboard.press("Escape");

  await page.locator('[data-tab="summary"]').click();
  await page.getByTestId("summary").getByRole("button", { name: "+ Add" }).first().click();
  const form = page.getByTestId("add-allergies");
  await form.locator("input[name=display]").fill("Latex");
  await form.locator("input[name=reaction]").fill("Urticaria");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId("summary")).toContainText("Latex");

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CCD" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/^ccd-.*\.xml$/);
});

test("imaging order from the chart lands on the worklist", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "e2e_doctor", st.user_password);
  await page.goto(`/clinical/chart?id=${st.local_person}`);
  await page.locator('[data-tab="orders"]').click();
  await page.getByRole("button", { name: "+ Order imaging" }).click();
  const form = page.getByTestId("add-service-requests");
  await form.locator("input[name=display]").fill("CT Coronary Angiography");
  await form.locator("select").nth(2).selectOption("CT");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId("orders")).toContainText(/ACC \S+/);
  await page.goto("/pacs/worklist");
  await expect(page.getByTestId("worklist-table")).toContainText("CT Coronary Angiography");
});

test("restricted record requires break-the-glass", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "e2e_doctor", st.user_password);
  await page.goto(`/clinical/chart?id=${st.vip_person}`);
  const box = page.getByTestId("restricted");
  await expect(box).toContainText("Restricted record");
  await box.locator("input").fill("Collapsed in clinic, need full history now");
  await box.getByRole("button", { name: "Break the glass" }).click();
  await expect(page.getByTestId("chart-name")).toHaveText("Staff Vip");
  await expect(page.getByTestId("chart")).toContainText("break-glass");
});

test("register a new patient", async ({ page, request }) => {
  await loginAs(page, request, "e2e_doctor", state().user_password);
  await page.goto("/clinical");
  await page.getByTestId("register-open").click();
  const dlg = page.getByRole("dialog");
  await dlg.locator("input").nth(0).fill("Moradi");
  await dlg.locator("input").nth(1).fill("Sara");
  await dlg.locator("input").nth(2).fill("1995-05-05");
  await dlg.locator("input").nth(3).fill("1122334455");
  await dlg.getByRole("button", { name: "Register" }).click();
  await expect(page.getByTestId("chart-name")).toHaveText("Sara Moradi");
});

test("incoming transfer: accept, package arrives, arrive and complete", async ({ page, request }) => {
  await loginAs(page, request, "e2e_doctor", state().user_password);
  await page.goto("/transfers");
  const card = page.getByTestId("transfer-requested");
  await expect(card).toContainText("E2E Peer Hospital");
  await expect(card).toContainText("Primary PCI");
  await card.getByRole("button", { name: "Accept" }).click();
  const accepted = page.getByTestId("transfer-accepted");
  await expect(accepted).toContainText("received", { timeout: 60_000 }); // package status after auto-send
  await expect(accepted).toContainText("AllergyIntolerance 1");
  await accepted.getByRole("button", { name: "Patient arrived" }).click();
  await page.getByTestId("transfer-arrived").getByRole("button", { name: "Complete" }).click();
  await expect(page.getByTestId("transfer-completed")).toBeVisible();
  await shot(page, "transfers");
});

test("EMS board: inbound critical patient through handover", async ({ page, request }) => {
  await loginAs(page, request, "e2e_doctor", state().user_password);
  await page.goto("/ems");
  const board = page.getByTestId("ems-board");
  await expect(board).toContainText("Chest pain");
  await expect(board).toContainText("MEDIC-21");
  await expect(board.locator(".vital-chips .abn").first()).toBeVisible(); // BP 84/50 flagged
  await board.getByRole("button", { name: "Acknowledge" }).click();
  await expect(board).toContainText("acknowledged");
  await board.getByRole("button", { name: "Arrived" }).click();
  await board.getByRole("button", { name: "Handed over" }).click();
  await expect(board).toContainText("handed_over");
  await shot(page, "ems");
});

test("interop admin: peer test, HL7 message, message log", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "admin", st.admin_password);
  await page.goto("/interop");
  await expect(page.getByTestId("interop-status")).toContainText("E2E General Hospital");
  await page.getByTestId("test-2.25.901").click();
  const res = page.getByTestId("test-result-2.25.901");
  await expect(res.locator(".pill.ok")).toHaveCount(3, { timeout: 30_000 });
  await page.getByTestId("hl7-send").click();
  await expect(page.getByTestId("hl7-ack")).toContainText("MSA|AA|MSG0001");
  await expect(page.getByTestId("messages-table")).toContainText("hl7v2");
  await shot(page, "interop");
});

test("Persian chart renders RTL", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "e2e_doctor", st.user_password);
  await page.goto(`/clinical/chart?id=${st.local_person}`);
  await page.locator(".lang-pick button", { hasText: "FA" }).click();
  await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
  await expect(page.locator('[data-tab="summary"]')).toHaveText("خلاصه");
  await expect(page.locator(".sidebar")).toContainText("بیماران");
});
