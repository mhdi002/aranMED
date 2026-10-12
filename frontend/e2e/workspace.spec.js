// Browser E2E for the reporting workspace and account management:
// dictation → report → saved reports, password change with session hand-over,
// administrator account controls, and the expired-session redirect.
const { test, expect } = require("@playwright/test");
const { state, loginAs } = require("./helpers");

test.describe.configure({ mode: "serial" });

async function adminToken(request) {
  const st = state();
  const r = await request.post(`${st.backend}/api/auth/login`,
                               { form: { username: "admin", password: st.admin_password } });
  return (await r.json()).access_token;
}

async function makeUser(request, username, password, role = "doctor") {
  const st = state();
  const tok = await adminToken(request);
  const r = await request.post(`${st.backend}/api/auth/register`, {
    headers: { Authorization: `Bearer ${tok}` }, data: { username, password, role },
  });
  expect([200, 400]).toContain(r.status());
}

async function canLogin(request, username, password) {
  const r = await request.post(`${state().backend}/api/auth/login`, { form: { username, password } });
  return r.status();
}

test("dictation: transcript to structured report, saved to Reports", async ({ page, request }) => {
  await loginAs(page, request, "e2e_doctor", state().user_password);
  await page.goto("/dictate");
  await page.getByLabel("Transcript").fill("Chest sonography. No pleural effusion. No pneumothorax.");
  await page.getByLabel("Report template").selectOption("chest");
  await page.getByTestId("dictate-generate").click();
  const report = page.locator(".report");
  await expect(report).not.toHaveClass(/empty/, { timeout: 60_000 });
  await expect(report).not.toHaveText("");
  await page.getByTestId("save-report").click();
  await expect(page.getByTestId("toast")).toContainText("Saved to Reports");
  await page.goto("/reports");
  await expect(page.locator("article").first()).toContainText("chest");
});

test("settings: change password, keep working, old password refused", async ({ page, request }) => {
  await makeUser(request, "pw_changer", "first-password-2026");
  await loginAs(page, request, "pw_changer", "first-password-2026");
  await page.goto("/settings");
  await page.getByLabel("Current password").fill("first-password-2026");
  await page.getByLabel(/^New password/).first().fill("second-password-2026");
  await page.getByLabel("New password again").fill("second-password-2026");
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByTestId("toast")).toContainText("Password changed");
  // Still signed in with the fresh token (in-app navigation: a full reload
  // would re-run the test's init script, which plants the old token).
  await page.locator(".sidebar").getByRole("button", { name: "Reports" }).click();
  await expect(page).toHaveURL(/\/reports/);
  await expect(page.locator(".card").first()).toBeVisible();
  expect(await canLogin(request, "pw_changer", "first-password-2026")).toBe(401);
  expect(await canLogin(request, "pw_changer", "second-password-2026")).toBe(200);
});

test("admin: disable, re-enable and reset an account from Settings → Users", async ({ page, request }) => {
  await makeUser(request, "to_disable", "disable-me-2026");
  await loginAs(page, request, "admin", state().admin_password);
  await page.goto("/settings");
  const row = page.getByTestId("users-table").locator("tr", { hasText: "to_disable" });
  await row.getByRole("button", { name: "Disable" }).click();
  await page.getByTestId("confirm-ok").click();
  await expect(row).toContainText("disabled");
  expect(await canLogin(request, "to_disable", "disable-me-2026")).toBe(401);

  await row.getByRole("button", { name: "Enable" }).click();
  await page.getByTestId("confirm-ok").click();
  await expect(row).toContainText("active");
  expect(await canLogin(request, "to_disable", "disable-me-2026")).toBe(200);

  await row.getByRole("button", { name: "Reset password" }).click();
  await page.getByTestId("confirm-ok").click();
  const temp = (await page.getByTestId("temp-password").textContent()).trim();
  expect(temp.length).toBeGreaterThan(10);
  expect(await canLogin(request, "to_disable", temp)).toBe(200);
});

test("an ended session sends the user back to sign-in", async ({ page, request }) => {
  await makeUser(request, "ended_session", "ended-session-2026");
  await loginAs(page, request, "ended_session", "ended-session-2026");
  await page.goto("/reports");
  await expect(page).toHaveURL(/\/reports/);
  // The administrator disables the account; the next API call is a 401.
  const st = state();
  const tok = await adminToken(request);
  const users = await (await request.get(`${st.backend}/api/auth/users`,
                                         { headers: { Authorization: `Bearer ${tok}` } })).json();
  const id = users.users.find((u) => u.username === "ended_session").id;
  await request.patch(`${st.backend}/api/auth/users/${id}`,
                      { headers: { Authorization: `Bearer ${tok}` }, data: { disabled: true } });
  await page.goto("/ehr");
  await expect(page).toHaveURL(/\/login/);
});
