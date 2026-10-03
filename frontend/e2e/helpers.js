const fs = require("fs");
const path = require("path");

function state() {
  return JSON.parse(fs.readFileSync(path.join(__dirname, ".state.json"), "utf8"));
}

/** Sign in through the API and seed the token the app reads from localStorage. */
async function loginAs(page, request, username, password) {
  const st = state();
  const r = await request.post(`${st.backend}/api/auth/login`, {
    form: { username, password },
  });
  if (!r.ok()) throw new Error(`login failed for ${username}: ${r.status()}`);
  const { access_token: token } = await r.json();
  await page.addInitScript((t) => {
    window.localStorage.setItem("asr.token", t);
    window.localStorage.setItem("asr.lang", "en");
  }, token);
  return token;
}

/** Fraction of non-black pixels and a cheap hash of the viewport's 2D canvas. */
async function canvasStats(page, index = 0) {
  return page.evaluate((i) => {
    const vp = document.querySelector(`[data-testid="viewport-${i}"]`);
    const canvas = vp && vp.querySelector("canvas");
    if (!canvas) return null;
    const c = document.createElement("canvas");
    c.width = canvas.width; c.height = canvas.height;
    const ctx = c.getContext("2d");
    ctx.drawImage(canvas, 0, 0);
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let lit = 0; let hash = 0; let sum = 0;
    for (let k = 0; k < d.length; k += 4) {
      const v = d[k] + d[k + 1] + d[k + 2];
      if (v > 30) lit += 1;
      sum += v;
      hash = (hash * 31 + d[k]) % 1000000007;
    }
    return { lit: lit / (d.length / 4), mean: sum / (d.length / 4) / 3, hash, w: c.width, h: c.height };
  }, index);
}

/** Save a screenshot when E2E_SHOTS names a directory (visual review aid). */
async function shot(page, name) {
  if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/${name}.png`, fullPage: false });
}

module.exports = { state, loginAs, canvasStats, shot };
