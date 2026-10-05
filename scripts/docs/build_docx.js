#!/usr/bin/env node
// Builds docs/AranMed_Guide_EN.docx and docs/AranMed_Guide_FA.docx.
//
// Inputs:
//   scripts/docs/content.js          bilingual text, screenshot keys, test ids per section
//   docs/screenshots/<lang>/*.png    from frontend/e2e/screenshots.spec.js (SCREENSHOT_LANG=en|fa)
//   docs/test-results/*.xml|json     pytest JUnit (sqlite, postgres, mixed) and Playwright JSON
//
// Usage: node scripts/docs/build_docx.js   (docx-js: npm "docx")

const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");
const {
  AlignmentType, BorderStyle, Bookmark, Document, Footer, HeadingLevel, ImageRun,
  InternalHyperlink, LevelFormat, Packer, PageBreak, PageNumber, Paragraph, ShadingType,
  Table, TableCell, TableRow, TextRun, VerticalAlign, WidthType,
} = require("docx");
const { TESTS, SECTIONS, UI } = require("./content");

const ROOT = path.resolve(__dirname, "..", "..");
const RESULTS = path.join(ROOT, "docs", "test-results");

// ---------------------------------------------------------------- test reports
function parseJUnit(file) {
  const out = new Map(); // id -> [{status, time, param}]
  if (!fs.existsSync(file)) return null;
  const xml = fs.readFileSync(file, "utf8");
  const re = /<testcase classname="([^"]*)" name="([^"]*)" time="([^"]*)"\s*(\/>|>([\s\S]*?)<\/testcase>)/g;
  let m;
  while ((m = re.exec(xml))) {
    const [, cls, name, time, , body] = m;
    const unesc = (s) => s.replace(/&gt;/g, ">").replace(/&lt;/g, "<").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
    const fn = unesc(name);
    const base = fn.replace(/\[.*$/, "");
    const param = fn.includes("[") ? fn.slice(fn.indexOf("[") + 1, -1) : null;
    const id = cls.replace(/\./g, "/") + ".py::" + base;
    let status = "passed";
    if (body && /<(failure|error)\b/.test(body)) status = "failed";
    else if (body && /<skipped\b/.test(body)) status = "skipped";
    if (!out.has(id)) out.set(id, []);
    out.get(id).push({ status, time: parseFloat(time) || 0, param });
  }
  return out;
}

function parsePlaywright(file) {
  const out = new Map();
  if (!fs.existsSync(file)) return null;
  const data = JSON.parse(fs.readFileSync(file, "utf8"));
  const walk = (suite) => {
    for (const spec of suite.specs || []) {
      for (const t of spec.tests || []) {
        const r = (t.results || [])[t.results.length - 1] || {};
        const status = r.status === "passed" ? "passed" : r.status === "skipped" || t.status === "skipped" ? "skipped" : "failed";
        out.set(`${suite.file || spec.file} › ${spec.title}`, [{ status, time: (r.duration || 0) / 1000, param: null }]);
      }
    }
    for (const s of suite.suites || []) walk({ ...s, file: s.file || suite.file });
  };
  for (const s of data.suites || []) walk(s);
  return out;
}

const RUNS = {
  sqlite: parseJUnit(path.join(RESULTS, "pytest-sqlite.xml")),
  postgres: parseJUnit(path.join(RESULTS, "pytest-postgres.xml")),
  mixed: parseJUnit(path.join(RESULTS, "pytest-mixed-network.xml")),
  e2e: parsePlaywright(path.join(RESULTS, "e2e.json")),
};
const RUN_LABEL = { sqlite: "SQLite", postgres: "PostgreSQL", mixed: { en: "Mixed", fa: "ترکیبی" }, e2e: "Playwright" };

function summarize(cases) {
  const s = { total: cases.length, passed: 0, failed: 0, skipped: 0, time: 0 };
  for (const c of cases) { s[c.status]++; s.time += c.time; }
  return s;
}

function runTotals(run) {
  const all = [];
  for (const cases of run.values()) all.push(...cases);
  return summarize(all);
}

// ---------------------------------------------------------------- device matrix from the run
const SYNTAX_ORDER = ["explicit", "implicit", "bigendian", "deflate", "rle", "jpeg_baseline", "jpeg_lossless", "jpegls", "j2k"];
const SYNTAX_LABEL = {
  explicit: "Explicit LE", implicit: "Implicit LE", bigendian: "Big Endian", deflate: "Deflate", rle: "RLE",
  jpeg_baseline: "JPEG Baseline", jpeg_lossless: "JPEG Lossless", jpegls: "JPEG-LS", j2k: "JPEG 2000",
};
const DEVICE_LABEL = {
  CT: ["CT", "CT"], MR: ["MR", "MR"], DX: ["DX (digital X-ray)", "DX (رادیوگرافی دیجیتال)"],
  MG: ["MG (mammography)", "MG (ماموگرافی)"], PT: ["PT (PET)", "PT (پت)"], NM: ["NM (palette colour)", "NM (پزشکی هسته‌ای، پالت رنگ)"],
  US: ["US (colour)", "US (سونوگرافی رنگی)"], US_CINE: ["US cine (multi-frame)", "US سینه (چندفریمی)"],
  XA: ["XA (angiography, multi-frame)", "XA (آنژیوگرافی چندفریمی)"], EMR: ["Enhanced MR (functional groups)", "MR پیشرفته (گروه‌های عملکردی)"],
  SC: ["Secondary capture RGB", "تصویر ثانویه RGB"], SEG: ["SEG (1-bit segmentation)", "SEG (بخش‌بندی یک‌بیتی)"],
  RTDOSE: ["RT Dose (32-bit)", "RT Dose (دوز ۳۲ بیتی)"],
};

function realFileDescriptions() {
  const src = fs.readFileSync(path.join(ROOT, "tests", "functional", "device_factory.py"), "utf8");
  const map = {};
  for (const m of src.matchAll(/^\s+"([^"]+\.dcm)": "([^"]+)",$/gm)) map[m[1]] = m[2];
  return map;
}

// ---------------------------------------------------------------- formatting helpers
const FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹";
const num = (lang, v) => {
  const s = String(v);
  return lang === "fa" ? s.replace(/[0-9]/g, (d) => FA_DIGITS[d]).replace(/\./g, "٫") : s;
};
const secs = (lang, t) => (t >= 60 ? `${num(lang, (t / 60).toFixed(1))} ${lang === "fa" ? "دقیقه" : "min"}`
  : `${num(lang, t.toFixed(t < 10 ? 2 : 1))} ${lang === "fa" ? "ثانیه" : "s"}`);
const L = (obj, lang) => (obj && typeof obj === "object" && !Array.isArray(obj) ? obj[lang] : obj);

const PAGE_W = 11906, PAGE_H = 16838, MARGIN = 1134;
const CONTENT_W = PAGE_W - 2 * MARGIN; // 9638 DXA
const MAX_IMG_W = Math.floor((CONTENT_W / 1440) * 96); // px at 96 dpi
const MAX_IMG_H = 760;

const COLORS = { primary: "1F4E79", accent: "2E75B6", headBg: "1F4E79", zebra: "F2F6FA", pass: "1E7B34", fail: "C00000", skip: "8A6D00", muted: "666666", border: "BFC9D4" };
const border = { style: BorderStyle.SINGLE, size: 4, color: COLORS.border };
const BORDERS = { top: border, bottom: border, left: border, right: border };

function makeKit(lang) {
  const rtl = lang === "fa";
  const font = rtl ? { ascii: "Tahoma", hAnsi: "Tahoma", cs: "Tahoma", eastAsia: "Tahoma" } : { ascii: "Calibri", hAnsi: "Calibri", cs: "Calibri", eastAsia: "Calibri" };
  const mono = { ascii: "Consolas", hAnsi: "Consolas", cs: "Consolas" };
  const align = rtl ? AlignmentType.RIGHT : AlignmentType.LEFT;

  // A run in the document's language direction.
  const run = (text, o = {}) => new TextRun({ text, font, rightToLeft: rtl, ...o });
  // Code, ids, file names: always left-to-right.
  const code = (text, o = {}) => new TextRun({ text, font: mono, rightToLeft: false, size: 17, ...o });

  const para = (children, o = {}) => new Paragraph({
    children: Array.isArray(children) ? children : [typeof children === "string" ? run(children) : children],
    bidirectional: rtl, alignment: o.alignment || align, spacing: { after: 120, line: 300, ...(o.spacing || {}) }, ...o.extra,
  });
  const ltrPara = (children, o = {}) => new Paragraph({
    children: Array.isArray(children) ? children : [children], bidirectional: false,
    alignment: AlignmentType.LEFT, spacing: { after: 60, ...(o.spacing || {}) }, ...o.extra,
  });
  const heading = (text, level, bookmarkId) => new Paragraph({
    heading: level, bidirectional: rtl, alignment: align, keepNext: true,
    children: bookmarkId ? [new Bookmark({ id: bookmarkId, children: [run(text)] })] : [run(text)],
  });

  const cell = (children, width, o = {}) => new TableCell({
    width: { size: width, type: WidthType.DXA }, borders: BORDERS, verticalAlign: VerticalAlign.CENTER,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    shading: o.fill ? { fill: o.fill, type: ShadingType.CLEAR, color: "auto" } : undefined,
    children: Array.isArray(children) ? children : [children],
  });
  const headCell = (text, width) => cell(para([run(text, { bold: true, color: "FFFFFF", size: 18 })], { spacing: { after: 0 } }), width, { fill: COLORS.headBg });
  const table = (widths, header, rows) => new Table({
    width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA }, columnWidths: widths,
    visuallyRightToLeft: rtl,
    rows: [
      new TableRow({ tableHeader: true, children: header.map((h, i) => headCell(h, widths[i])) }),
      ...rows.map((r, ri) => new TableRow({ cantSplit: true, children: r.map((c, i) => cell(c, widths[i], { fill: ri % 2 ? COLORS.zebra : undefined })) })),
    ],
  });
  return { rtl, font, mono, align, run, code, para, ltrPara, heading, table, cell };
}

function pngSize(file) {
  const b = fs.readFileSync(file);
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20), data: b };
}

// Full-page captures of long lists would shrink to an unreadable strip; keep the
// top of the page (ImageMagick when available, otherwise the whole capture).
const MAX_ASPECT = 1.35;
function loadShot(file) {
  const img = pngSize(file);
  if (img.h / img.w <= MAX_ASPECT) return img;
  try {
    const h = Math.round(img.w * MAX_ASPECT);
    const data = execSync(`convert "${file}" -crop ${img.w}x${h}+0+0 +repage png:-`, { maxBuffer: 64 << 20 });
    return { w: img.w, h, data };
  } catch { return img; }
}

function image(kit, lang, key, caption, figNo) {
  const file = path.join(ROOT, "docs", "screenshots", lang, `${key}.png`);
  if (!fs.existsSync(file)) return [kit.para([kit.run(`[missing screenshot ${key}]`, { color: COLORS.fail })])];
  const { w, h, data } = loadShot(file);
  const scale = Math.min(1, MAX_IMG_W / w, MAX_IMG_H / h);
  return [
    new Paragraph({
      alignment: AlignmentType.CENTER, keepNext: true, spacing: { before: 120, after: 60 },
      children: [new ImageRun({
        type: "png", data, transformation: { width: Math.round(w * scale), height: Math.round(h * scale) },
        altText: { title: key, description: caption, name: key },
      })],
    }),
    new Paragraph({
      alignment: AlignmentType.CENTER, bidirectional: kit.rtl, spacing: { after: 240 },
      children: [kit.run(`${lang === "fa" ? "شکل" : "Figure"} ${num(lang, figNo)}: ${caption}`, { italics: true, size: 18, color: COLORS.muted })],
    }),
  ];
}

// Result cell for a test: one line per run in which the test appears.
function resultLines(kit, lang, id) {
  const lines = [];
  const runs = id.includes(" › ") ? ["e2e"] : ["sqlite", "postgres", "mixed"];
  for (const r of runs) {
    const cases = RUNS[r] && RUNS[r].get(id);
    if (!cases) continue;
    const s = summarize(cases);
    const ok = s.failed === 0 && s.passed > 0;
    const color = s.failed ? COLORS.fail : s.passed ? COLORS.pass : COLORS.skip;
    const word = s.failed ? L(UI.fail, lang) : s.passed ? L(UI.pass, lang) : L(UI.skipped, lang);
    const count = s.total > 1 ? ` ${num(lang, s.passed)}/${num(lang, s.total)}` : "";
    lines.push(kit.para([
      kit.run(`${ok ? "✔" : s.failed ? "✘" : "–"} `, { color, bold: true, size: 17 }),
      kit.run(`${L(RUN_LABEL[r], lang)}: `, { size: 17, color: COLORS.muted }),
      kit.run(`${word}${count}`, { size: 17, bold: true, color }),
      kit.run(` · ${secs(lang, s.time)}`, { size: 15, color: COLORS.muted }),
    ], { spacing: { after: 0 } }));
  }
  if (!lines.length) lines.push(kit.para([kit.run(L(UI.notRun, lang), { color: COLORS.fail, size: 17 })], { spacing: { after: 0 } }));
  return lines;
}

function shortId(id) {
  if (id.includes(" › ")) return id.split(" › ")[0];
  return id.replace(/^tests\/(functional\/)?/, "");
}

function testsTable(kit, lang, ids) {
  const widths = [4300, 2938, 2400];
  const rows = ids.map((id) => {
    const desc = TESTS[id] ? TESTS[id][lang] : id;
    const cases = (RUNS.sqlite && RUNS.sqlite.get(id)) || [];
    const extra = cases.length > 1 ? ` (${num(lang, cases.length)} ${L(UI.cases, lang)})` : "";
    const isE2E = id.includes(" › ");
    const idLines = isE2E
      ? [kit.ltrPara(kit.code(shortId(id))), kit.ltrPara(kit.code(id.split(" › ")[1], { size: 14, color: COLORS.muted }), { spacing: { after: 0 } })]
      : [kit.ltrPara(kit.code(shortId(id).split("::")[0], { size: 14, color: COLORS.muted }), { spacing: { after: 0 } }),
         kit.ltrPara(kit.code(shortId(id).split("::")[1]), { spacing: { after: 0 } })];
    return [
      kit.para([kit.run(desc + extra, { size: 19 })], { spacing: { after: 0 } }),
      idLines,
      resultLines(kit, lang, id),
    ];
  });
  return kit.table(widths, [L(UI.verifies, lang), L(UI.test, lang), L(UI.result, lang)], rows);
}

function deviceMatrix(kit, lang) {
  const cases = (RUNS.sqlite && RUNS.sqlite.get("tests/functional/test_pacs_devices.py::test_device_in_transfer_syntax")) || [];
  const grid = {};
  for (const c of cases) {
    const i = c.param.lastIndexOf("-");
    const dev = c.param.slice(0, i), syn = c.param.slice(i + 1);
    (grid[dev] = grid[dev] || {})[syn] = c.status;
  }
  const devices = Object.keys(DEVICE_LABEL).filter((d) => grid[d]);
  const syntaxes = SYNTAX_ORDER.filter((s) => devices.some((d) => grid[d][s]));
  const first = 1900, rest = Math.floor((CONTENT_W - first) / syntaxes.length);
  const widths = [first, ...syntaxes.map(() => rest)];
  widths[0] += CONTENT_W - widths.reduce((a, b) => a + b, 0);
  const mark = (st) => {
    if (!st) return kit.para([kit.run("", { size: 16 })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } });
    const color = st === "passed" ? COLORS.pass : st === "failed" ? COLORS.fail : COLORS.skip;
    return kit.para([kit.run(st === "passed" ? "✔" : st === "failed" ? "✘" : "–", { color, bold: true, size: 20 })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } });
  };
  const header = [L(UI.device, lang), ...syntaxes.map((s) => SYNTAX_LABEL[s])];
  const rows = devices.map((d) => [
    kit.para([kit.run(DEVICE_LABEL[d][lang === "fa" ? 1 : 0], { size: 17 })], { spacing: { after: 0 } }),
    ...syntaxes.map((s) => mark(grid[d][s])),
  ]);
  const t = new Table({
    width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: widths, visuallyRightToLeft: kit.rtl,
    rows: [
      new TableRow({ tableHeader: true, children: header.map((h, i) => kit.cell(
        i === 0 ? kit.para([kit.run(h, { bold: true, color: "FFFFFF", size: 16 })], { spacing: { after: 0 } })
          : kit.ltrPara(new TextRun({ text: h, bold: true, color: "FFFFFF", size: 14, font: kit.font }), { spacing: { after: 0 }, extra: { alignment: AlignmentType.CENTER } }),
        widths[i], { fill: COLORS.headBg })) }),
      ...rows.map((r, ri) => new TableRow({ cantSplit: true, children: r.map((c, i) => kit.cell(c, widths[i], { fill: ri % 2 ? COLORS.zebra : undefined })) })),
    ],
  });
  const s = summarize(cases);
  return [
    kit.heading(L(UI.matrix, lang), HeadingLevel.HEADING_2),
    kit.para(`${lang === "fa" ? "ترکیب‌ها" : "Combinations"}: ${num(lang, s.total)} · ${L(UI.passed, lang)}: ${num(lang, s.passed)} · ${L(UI.failed, lang)}: ${num(lang, s.failed)}`),
    t,
    kit.para(""),
  ];
}

function realFilesTable(kit, lang) {
  const desc = realFileDescriptions();
  const ids = ["test_real_world_image_files", "test_real_world_corrupt_streams_fail_cleanly", "test_real_world_non_image_files"];
  const kind = {
    test_real_world_image_files: T2("decoded and displayed", "رمزگشایی و نمایش"),
    test_real_world_corrupt_streams_fail_cleanly: T2("archived; refused cleanly (406)", "بایگانی؛ رد با پیام روشن (۴۰۶)"),
    test_real_world_non_image_files: T2("stored and served", "ذخیره و ارائه"),
  };
  const rows = [];
  for (const fn of ids) {
    for (const c of (RUNS.sqlite && RUNS.sqlite.get(`tests/functional/test_pacs_devices.py::${fn}`)) || []) {
      const color = c.status === "passed" ? COLORS.pass : COLORS.fail;
      rows.push([
        kit.ltrPara(kit.code(c.param), { spacing: { after: 0 } }),
        kit.ltrPara(new TextRun({ text: desc[c.param] || "", size: 17, font: kit.font }), { spacing: { after: 0 } }),
        kit.para([kit.run(`${c.status === "passed" ? "✔" : "✘"} ${kind[fn][lang]}`, { size: 17, color })], { spacing: { after: 0 } }),
      ]);
    }
  }
  if (!rows.length) return [];
  return [
    kit.heading(L(UI.realFiles, lang), HeadingLevel.HEADING_2),
    kit.table([2900, 3938, 2800], [L(UI.file, lang), lang === "fa" ? "توضیح" : "Description", L(UI.result, lang)], rows),
    kit.para(""),
  ];
}
function T2(en, fa) { return { en, fa }; }

// ---------------------------------------------------------------- document
function build(lang) {
  const kit = makeKit(lang);
  const { run, para, heading, table } = kit;
  const children = [];
  let fig = 0;
  const commit = (() => { try { return execSync("git rev-parse --short HEAD", { cwd: ROOT }).toString().trim(); } catch { return ""; } })();
  const today = new Date().toISOString().slice(0, 10);

  // Cover
  children.push(
    new Paragraph({ spacing: { before: 3000 }, children: [] }),
    new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: kit.rtl, spacing: { after: 300 }, children: [run(L(UI.title, lang), { bold: true, size: 52, color: COLORS.primary })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: kit.rtl, spacing: { after: 600 }, children: [run(L(UI.subtitle, lang), { size: 26, color: COLORS.accent })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: kit.rtl, children: [run(`${L(UI.generated, lang)}: `, { size: 20, color: COLORS.muted }), new TextRun({ text: today, size: 20, color: COLORS.muted, font: kit.font })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, children: [kit.code(commit ? `commit ${commit}` : "", { color: COLORS.muted })] }),
    new Paragraph({ children: [new PageBreak()] }),
  );

  // Contents (static, with links; no field update needed)
  const toc = [
    ["about", L(UI.about, lang)], ["summary", L(UI.summary, lang)], ["setup", L(UI.setup, lang)],
    ...SECTIONS.map((s) => [`sec_${s.key}`, L(s.title, lang)]),
    ["protocols", L(UI.matrixTitle, lang)], ["other", L(UI.otherTests, lang)],
  ];
  children.push(heading(L(UI.contents, lang), HeadingLevel.HEADING_1));
  toc.forEach(([id, title], i) => children.push(para([
    new InternalHyperlink({ anchor: id, children: [run(`${num(lang, i + 1)}. ${title}`, { color: COLORS.accent, size: 22 })] }),
  ], { spacing: { after: 80 } })));
  children.push(new Paragraph({ children: [new PageBreak()] }));

  let n = 0;
  const H1 = (id, title) => heading(`${num(lang, ++n)}. ${title}`, HeadingLevel.HEADING_1, id);

  // About
  children.push(H1("about", L(UI.about, lang)), para(L(UI.aboutText, lang)));

  // Summary
  children.push(H1("summary", L(UI.summary, lang)));
  const sumRows = [];
  for (const r of ["sqlite", "postgres", "mixed", "e2e"]) {
    if (!RUNS[r]) continue;
    const s = runTotals(RUNS[r]);
    const color = s.failed ? COLORS.fail : COLORS.pass;
    const note = r === "e2e" && s.skipped ? (lang === "fa" ? " (رد شده‌ها: آزمون‌های تصویربرداری که جداگانه برای هر زبان اجرا می‌شوند)" : " (skipped: screenshot runs, executed separately per language)") : "";
    sumRows.push([
      para([run(L(UI.runs[r], lang) + note, { size: 19 })], { spacing: { after: 0 } }),
      para([run(num(lang, s.passed), { bold: true, color: COLORS.pass })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
      para([run(num(lang, s.failed), { bold: true, color })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
      para([run(num(lang, s.skipped), { color: COLORS.muted })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
      para([run(secs(lang, s.time), { size: 18 })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
    ]);
  }
  children.push(table([4838, 1200, 1200, 1200, 1200], [L(UI.run, lang), L(UI.passed, lang), L(UI.failed, lang), L(UI.skipped, lang), L(UI.time, lang)], sumRows), para(""));

  // Setup
  children.push(H1("setup", L(UI.setup, lang)));
  for (const t of UI.setupText[lang]) children.push(para(t));
  for (const c of UI.commands) children.push(kit.ltrPara(kit.code(c, { size: 18 }), { extra: { shading: { fill: "F3F3F3", type: ShadingType.CLEAR, color: "auto" }, indent: { left: 200, right: 200 } } }));
  children.push(para(lang === "fa"
    ? "اجرای آزمون‌ها: pytest (SQLite)، با DATABASE_URL روی PostgreSQL، و شبکه ترکیبی با HOSPITAL_PG_DSN_BASE و HOSPITAL_PG_OIDS؛ آزمون مرورگر: در frontend دستور npx playwright test."
    : "Running the tests: pytest (SQLite); with DATABASE_URL against PostgreSQL; the mixed network with HOSPITAL_PG_DSN_BASE and HOSPITAL_PG_OIDS; browser tests: npx playwright test in frontend.", { spacing: { before: 120 } }));

  // Sections
  for (const sec of SECTIONS) {
    children.push(new Paragraph({ children: [new PageBreak()] }), H1(`sec_${sec.key}`, L(sec.title, lang)));
    for (const t of sec.text[lang]) children.push(para(t));
    children.push(heading(L(UI.screens, lang), HeadingLevel.HEADING_2));
    for (const [key, cap] of sec.shots) children.push(...image(kit, lang, key, L(cap, lang), ++fig));
    children.push(heading(L(UI.tests, lang), HeadingLevel.HEADING_2), testsTable(kit, lang, sec.tests), para(""));
    if (sec.deviceMatrix) children.push(...deviceMatrix(kit, lang), ...realFilesTable(kit, lang));
  }

  // Protocols
  children.push(new Paragraph({ children: [new PageBreak()] }), H1("protocols", L(UI.matrixTitle, lang)));
  children.push(table([2400, 7238], [L(UI.protocol, lang), L(UI.scope, lang)],
    UI.protocols.map(([name, scope]) => [kit.ltrPara(new TextRun({ text: name, bold: true, size: 19, font: kit.font }), { spacing: { after: 0 } }), para([run(L(scope, lang), { size: 19 })], { spacing: { after: 0 } })])));

  // Other tests
  children.push(H1("other", L(UI.otherTests, lang)), para(L(UI.otherText, lang)));
  const covered = new Set(SECTIONS.flatMap((s) => s.tests));
  const perFile = new Map();
  for (const [id, cases] of RUNS.sqlite || []) {
    if (covered.has(id)) continue;
    const f = id.split("::")[0];
    const s = perFile.get(f) || [];
    perFile.set(f, s.concat(cases));
  }
  const otherRows = [...perFile.entries()].sort().map(([f, cases]) => {
    const s = summarize(cases);
    return [
      kit.ltrPara(kit.code(f.replace(/^tests\//, "")), { spacing: { after: 0 } }),
      para([run(num(lang, s.total))], { alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
      para([run(num(lang, s.passed), { color: COLORS.pass, bold: true })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
      para([run(num(lang, s.failed), { color: s.failed ? COLORS.fail : COLORS.muted })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
      para([run(num(lang, s.skipped), { color: COLORS.muted })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
    ];
  });
  children.push(table([5238, 1100, 1100, 1100, 1100], [L(UI.file, lang), L(UI.tests, lang), L(UI.passed, lang), L(UI.failed, lang), L(UI.skipped, lang)], otherRows));

  const baseFont = kit.font;
  return new Document({
    creator: "AranMed", title: L(UI.title, lang), description: L(UI.subtitle, lang),
    styles: {
      default: { document: { run: { font: baseFont, size: 21, ...(kit.rtl ? { rightToLeft: true, language: { value: "en-US", bidirectional: "fa-IR" } } : {}) } } },
      paragraphStyles: [
        { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { font: baseFont, size: 34, bold: true, color: COLORS.primary },
          paragraph: { spacing: { before: 240, after: 180 }, outlineLevel: 0, border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: COLORS.accent, space: 4 } } } },
        { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { font: baseFont, size: 26, bold: true, color: COLORS.accent },
          paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 1 } },
      ],
    },
    numbering: { config: [{ reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT }] }] },
    sections: [{
      properties: { page: { size: { width: PAGE_W, height: PAGE_H }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
      footers: {
        default: new Footer({ children: [new Paragraph({
          alignment: AlignmentType.CENTER, bidirectional: kit.rtl,
          children: [run(`${L(UI.title, lang)} · `, { size: 16, color: COLORS.muted }), new TextRun({ children: [PageNumber.CURRENT], size: 16, color: COLORS.muted, font: baseFont })],
        })] }),
      },
      children,
    }],
  });
}

(async () => {
  for (const [lang, name] of [["en", "AranMed_Guide_EN.docx"], ["fa", "AranMed_Guide_FA.docx"]]) {
    const buf = await Packer.toBuffer(build(lang));
    const out = path.join(ROOT, "docs", name);
    fs.writeFileSync(out, buf);
    console.log(`wrote ${path.relative(ROOT, out)} (${(buf.length / 1048576).toFixed(1)} MB)`);
  }
  for (const [r, run] of Object.entries(RUNS)) {
    if (!run) { console.log(`${r}: report missing`); continue; }
    const s = runTotals(run);
    console.log(`${r}: ${s.passed} passed, ${s.failed} failed, ${s.skipped} skipped`);
  }
  const missing = SECTIONS.flatMap((s) => s.tests).filter((id) => !(id.includes(" › ") ? RUNS.e2e && RUNS.e2e.get(id) : RUNS.sqlite && RUNS.sqlite.get(id)));
  if (missing.length) { console.log("tests listed in content.js but absent from the reports:"); missing.forEach((m) => console.log("  " + m)); process.exitCode = 1; }
})();
