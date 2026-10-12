#!/usr/bin/env node
// Builds docs/سند فنی.docx — the Persian technical document — from
// scripts/docs/tech_doc_fa.js, with test totals read from docs/test-results/.
//
// Right-to-left throughout: bidi paragraphs and section, RTL runs with a
// complex-script font and fa-IR language, visually right-to-left tables,
// Persian digits in generated numbers; commands and identifiers stay LTR.
//
// Usage: node scripts/docs/build_tech_doc_fa.js   (docx-js: npm "docx")

const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");
const {
  AlignmentType, BorderStyle, Bookmark, Document, Footer, Header, HeadingLevel, ImageRun,
  InternalHyperlink, LevelFormat, Packer, PageBreak, PageNumber, Paragraph, ShadingType,
  Table, TableCell, TableRow, TextRun, VerticalAlign, WidthType,
} = require("docx");
const { META, BLOCKS } = require("./tech_doc_fa");

const ROOT = path.resolve(__dirname, "..", "..");
const OUT = path.join(ROOT, "docs", "سند فنی.docx");

const PAGE_W = 11906, PAGE_H = 16838, MARGIN = 1134;
const CONTENT_W = PAGE_W - 2 * MARGIN;
const C = { primary: "1F4E79", accent: "0B7B74", head: "1F4E79", zebra: "F2F6FA", muted: "5D6575",
            border: "BFC9D4", noteBg: "EEF6F5", codeBg: "F3F3F3", ok: "11704E", bad: "B42A33" };

const FONT = { ascii: "Tahoma", hAnsi: "Tahoma", cs: "Tahoma", eastAsia: "Tahoma" };
const MONO = { ascii: "Consolas", hAnsi: "Consolas", cs: "Consolas" };
const FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹";
const fa = (v) => String(v).replace(/[0-9]/g, (d) => FA_DIGITS[d]).replace(/\./g, "٫");

const border = { style: BorderStyle.SINGLE, size: 4, color: C.border };
const BORDERS = { top: border, bottom: border, left: border, right: border };

// ---------------------------------------------------------------- primitives
const run = (text, o = {}) => new TextRun({ text, font: FONT, rightToLeft: true, ...o });
const ltr = (text, o = {}) => new TextRun({ text, font: MONO, rightToLeft: false, size: 18, ...o });

function para(children, o = {}) {
  return new Paragraph({
    children: Array.isArray(children) ? children : [typeof children === "string" ? run(children) : children],
    bidirectional: true, alignment: o.alignment || AlignmentType.JUSTIFIED,
    spacing: { after: 140, line: 330, ...(o.spacing || {}) }, ...(o.extra || {}),
  });
}

function heading(text, level, id) {
  const r = run(text);
  return new Paragraph({
    heading: level, bidirectional: true, alignment: AlignmentType.RIGHT, keepNext: true,
    children: id ? [new Bookmark({ id, children: [r] })] : [r],
  });
}

function cell(children, width, fill) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA }, borders: BORDERS, verticalAlign: VerticalAlign.CENTER,
    margins: { top: 70, bottom: 70, left: 110, right: 110 },
    shading: fill ? { fill, type: ShadingType.CLEAR, color: "auto" } : undefined,
    children: Array.isArray(children) ? children : [children],
  });
}

function table({ widths, head, rows }) {
  const total = widths.reduce((a, b) => a + b, 0);
  const small = (t, o = {}) => para([run(String(t), { size: 19, ...o })],
                                    { alignment: AlignmentType.RIGHT, spacing: { after: 0, line: 290 } });
  return new Table({
    width: { size: total, type: WidthType.DXA }, columnWidths: widths, visuallyRightToLeft: true,
    rows: [
      new TableRow({ tableHeader: true, children: head.map((h, i) =>
        cell(small(h, { bold: true, color: "FFFFFF" }), widths[i], C.head)) }),
      ...rows.map((r, ri) => new TableRow({ cantSplit: true, children: r.map((c, i) =>
        cell(small(c, i === 0 ? { bold: true } : {}), widths[i], ri % 2 ? C.zebra : undefined)) })),
    ],
  });
}

function pngSize(file) {
  const b = fs.readFileSync(file);
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20), data: b };
}

function loadShot(file) {
  const img = pngSize(file);
  const maxAspect = 1.3;
  if (img.h / img.w <= maxAspect) return img;
  try {
    const h = Math.round(img.w * maxAspect);
    const data = execSync(`convert "${file}" -crop ${img.w}x${h}+0+0 +repage png:-`, { maxBuffer: 64 << 20 });
    return { w: img.w, h, data };
  } catch { return img; }
}

let figNo = 0;
function figure(key, caption) {
  const file = path.join(ROOT, "docs", "screenshots", "fa", `${key}.png`);
  if (!fs.existsSync(file)) return [];
  const { w, h, data } = loadShot(file);
  const maxW = Math.floor((CONTENT_W / 1440) * 96) - 20, maxH = 560;
  const s = Math.min(1, maxW / w, maxH / h);
  figNo += 1;
  return [
    new Paragraph({ alignment: AlignmentType.CENTER, keepNext: true, spacing: { before: 120, after: 60 },
      children: [new ImageRun({ type: "png", data, transformation: { width: Math.round(w * s), height: Math.round(h * s) },
                                altText: { title: key, description: caption, name: key } })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: true, spacing: { after: 240 },
      children: [run(`شکل ${fa(figNo)}: ${caption}`, { italics: true, size: 18, color: C.muted })] }),
  ];
}

function codeBlock(lines) {
  return lines.map((l, i) => new Paragraph({
    bidirectional: false, alignment: AlignmentType.LEFT,
    shading: { fill: C.codeBg, type: ShadingType.CLEAR, color: "auto" },
    indent: { left: 240, right: 240 }, spacing: { after: i === lines.length - 1 ? 200 : 0, before: i === 0 ? 80 : 0 },
    children: [ltr(l)],
  }));
}

function note(text) {
  return new Paragraph({
    bidirectional: true, alignment: AlignmentType.RIGHT, spacing: { before: 120, after: 200, line: 320 },
    shading: { fill: C.noteBg, type: ShadingType.CLEAR, color: "auto" },
    border: { right: { style: BorderStyle.SINGLE, size: 18, color: C.accent, space: 6 } },
    children: [run(text, { size: 20 })],
  });
}

// ---------------------------------------------------------------- test totals
function junit(file) {
  const p = path.join(ROOT, "docs", "test-results", file);
  if (!fs.existsSync(p)) return null;
  const xml = fs.readFileSync(p, "utf8");
  const m = xml.match(/<testsuite[^>]*errors="(\d+)"[^>]*failures="(\d+)"[^>]*skipped="(\d+)"[^>]*tests="(\d+)"[^>]*time="([\d.]+)"/);
  if (!m) return null;
  const [errors, failures, skipped, tests, time] = m.slice(1).map(Number);
  return { passed: tests - errors - failures - skipped, failed: errors + failures, skipped, time };
}

function playwright(file) {
  const p = path.join(ROOT, "docs", "test-results", file);
  if (!fs.existsSync(p)) return null;
  const d = JSON.parse(fs.readFileSync(p, "utf8"));
  const s = d.stats || {};
  return { passed: s.expected || 0, failed: (s.unexpected || 0) + (s.flaky || 0), skipped: s.skipped || 0,
           time: (s.duration || 0) / 1000 };
}

function testsTable() {
  const runs = [
    ["مجموعه کامل پایتون روی SQLite (واحد، عملکردی، MedRAG)", junit("pytest-sqlite.xml")],
    ["مجموعه سرور روی PostgreSQL", junit("pytest-postgres.xml")],
    ["شبکه ترکیبی: بیمارستان‌ها روی SQLite و PostgreSQL", junit("pytest-mixed-network.xml")],
    ["مرورگر: عملکرد و دسترس‌پذیری (Playwright و axe)", playwright("e2e.json")],
  ].filter(([, r]) => r);
  const mins = (t) => (t >= 60 ? `${fa((t / 60).toFixed(1))} دقیقه` : `${fa(t.toFixed(0))} ثانیه`);
  const total = runs.reduce((a, [, r]) => a + r.passed, 0);
  const failed = runs.reduce((a, [, r]) => a + r.failed, 0);
  return [
    table({ widths: [CONTENT_W - 4 * 1200, 1200, 1200, 1200, 1200], head: ["اجرا", "موفق", "ناموفق", "رد شده", "زمان"],
            rows: runs.map(([name, r]) => [name, fa(r.passed), fa(r.failed), fa(r.skipped), mins(r.time)]) }),
    para([run(`جمع آزمون‌های موفق در این اجراها: ${fa(total)} · ناموفق: ${fa(failed)}. `, { bold: true }),
          run("آزمون‌های «رد شده» یا به سرور مدل واقعی نیاز دارند یا اجرای تصویربرداری از صفحات هستند که جداگانه برای هر زبان انجام می‌شود.", { size: 19, color: C.muted })],
         { spacing: { before: 120 } }),
  ];
}

// ---------------------------------------------------------------- document
function build() {
  const body = [];
  const commit = (() => { try { return execSync("git rev-parse --short HEAD", { cwd: ROOT }).toString().trim(); } catch { return ""; } })();
  const d = new Date();
  const date = new Intl.DateTimeFormat("fa-IR-u-ca-persian", { year: "numeric", month: "long", day: "numeric" }).format(d);

  // Cover
  body.push(
    new Paragraph({ spacing: { before: 2600 }, children: [] }),
    new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: true, spacing: { after: 240 },
      children: [run("سند فنی", { bold: true, size: 64, color: C.primary })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: true, spacing: { after: 360 },
      children: [run("سامانه آران‌مد", { bold: true, size: 44, color: C.accent })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: true, spacing: { after: 600 },
      children: [run(META.subtitle, { size: 24, color: C.muted })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: true, spacing: { after: 120 },
      children: [run(META.version, { size: 22 })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: true, spacing: { after: 120 },
      children: [run(`تاریخ: ${date}`, { size: 22 })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 600 },
      children: [ltr(commit ? `commit ${commit}` : "", { color: C.muted })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: true,
      children: [run(META.audience, { size: 20, color: C.muted })] }),
    new Paragraph({ children: [new PageBreak()] }),
  );

  // Contents (static links: no field update needed)
  const h1s = BLOCKS.filter((b) => b.h1).map((b, i) => [`s${i + 1}`, b.h1]);
  body.push(heading("فهرست مطالب", HeadingLevel.HEADING_1));
  h1s.forEach(([id, title], i) => body.push(para([
    new InternalHyperlink({ anchor: id, children: [run(`${fa(i + 1)}. ${title}`, { color: C.accent, size: 23 })] }),
  ], { alignment: AlignmentType.RIGHT, spacing: { after: 90 } })));
  body.push(new Paragraph({ children: [new PageBreak()] }));

  let n = 0;
  for (const b of BLOCKS) {
    if (b.pagebreak) body.push(new Paragraph({ children: [new PageBreak()] }));
    else if (b.h1) { n += 1; body.push(heading(`${fa(n)}. ${b.h1}`, HeadingLevel.HEADING_1, `s${n}`)); }
    else if (b.h2) body.push(heading(b.h2, HeadingLevel.HEADING_2));
    else if (b.h3) body.push(heading(b.h3, HeadingLevel.HEADING_3));
    else if (b.p) body.push(para(b.p));
    else if (b.note) body.push(note(b.note));
    else if (b.bullets) b.bullets.forEach((t) => body.push(new Paragraph({
      numbering: { reference: "bullets", level: 0 }, bidirectional: true, alignment: AlignmentType.JUSTIFIED,
      spacing: { after: 90, line: 320 }, children: [run(t)] })));
    else if (b.steps) {
      const ref = `steps${n}_${body.length}`;
      stepsRefs.push(ref);
      b.steps.forEach((t) => body.push(new Paragraph({
        numbering: { reference: ref, level: 0 }, bidirectional: true, alignment: AlignmentType.JUSTIFIED,
        spacing: { after: 90, line: 320 }, children: [run(t)] })));
    }
    else if (b.table) body.push(table(b.table), para("", { spacing: { after: 120 } }));
    else if (b.code) body.push(...codeBlock(b.code));
    else if (b.img) body.push(...figure(b.img, b.caption));
    else if (b.tests) body.push(...testsTable());
  }

  const numbering = [
    { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.RIGHT,
        style: { paragraph: { indent: { left: 540, hanging: 300 } } } }] },
    ...stepsRefs.map((reference) => ({ reference, levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.",
        alignment: AlignmentType.RIGHT, style: { paragraph: { indent: { left: 540, hanging: 360 } } } }] })),
  ];

  return new Document({
    creator: "AranMed", title: META.title, description: META.subtitle,
    styles: {
      default: { document: { run: { font: FONT, size: 22, rightToLeft: true,
                                    language: { value: "fa-IR", bidirectional: "fa-IR" } } } },
      paragraphStyles: [
        { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { font: FONT, size: 36, bold: true, color: C.primary, rightToLeft: true },
          paragraph: { spacing: { before: 240, after: 200 }, outlineLevel: 0,
                       border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: C.accent, space: 4 } } } },
        { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { font: FONT, size: 28, bold: true, color: C.accent, rightToLeft: true },
          paragraph: { spacing: { before: 280, after: 140 }, outlineLevel: 1 } },
        { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { font: FONT, size: 24, bold: true, rightToLeft: true },
          paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 2 } },
      ],
    },
    numbering: { config: numbering },
    sections: [{
      properties: {
        page: { size: { width: PAGE_W, height: PAGE_H }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } },
      },
      headers: {
        default: new Header({ children: [new Paragraph({ alignment: AlignmentType.LEFT, bidirectional: true,
          children: [run("سند فنی آران‌مد", { size: 16, color: C.muted })] })] }),
      },
      footers: {
        default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, bidirectional: true,
          children: [run("صفحه ", { size: 16, color: C.muted }),
                     new TextRun({ children: [PageNumber.CURRENT], size: 16, color: C.muted, font: FONT })] })] }),
      },
      children: body,
    }],
  });
}

const stepsRefs = [];

// docx-js has no option for a right-to-left *section* (w:bidi in sectPr),
// which is what makes Word lay out columns, page numbers and the gutter for
// RTL. Add it to the finished package; schema order puts it before docGrid.
async function markSectionRtl(buf) {
  const JSZip = require(require.resolve("jszip", { paths: [path.dirname(require.resolve("docx"))] }));
  const zip = await JSZip.loadAsync(buf);
  const xml = await zip.file("word/document.xml").async("string");
  const fixed = xml.replace(/<w:docGrid /g, "<w:bidi/><w:docGrid ");
  zip.file("word/document.xml", fixed);
  return zip.generateAsync({ type: "nodebuffer", compression: "DEFLATE" });
}

(async () => {
  const buf = await markSectionRtl(await Packer.toBuffer(build()));
  fs.writeFileSync(OUT, buf);
  console.log(`wrote ${path.relative(ROOT, OUT)} (${(buf.length / 1048576).toFixed(1)} MB, ${figNo} figures)`);
})();
