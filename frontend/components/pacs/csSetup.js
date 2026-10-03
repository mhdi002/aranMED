// Cornerstone3D bootstrap for the AranMed viewer (browser only).
//
// We register our own image loader ("aranmed:") instead of the stock DICOM
// image loader: our DICOMweb server already parses the files, so the viewer
// only needs (1) WADO-RS metadata, which feeds a metadata provider, and
// (2) WADO-RS frames, which arrive as native pixels for uncompressed
// transfer syntaxes. That keeps WASM codecs and web workers out of the
// Next.js build. Frames in compressed transfer syntaxes fall back to the
// server-rendered /rendered image (8-bit, windowed server-side).
import * as cs from "@cornerstonejs/core";
import * as cst from "@cornerstonejs/tools";
import { apiUrl } from "../../lib/config";
import { DICOMWEB, tagValue } from "../../lib/pacs";

const NATIVE_TS = new Set([
  "1.2.840.10008.1.2", "1.2.840.10008.1.2.1", "1.2.840.10008.1.2.2", "1.2.840.10008.1.2.1.99",
]);

const meta = new Map();   // imageId -> parsed metadata
let token = null;
let initialised = false;

export function setToken(t) { token = t; }

function num(v, d) {
  const x = Array.isArray(v) ? v[0] : v;
  const n = typeof x === "string" ? parseFloat(x) : x;
  return Number.isFinite(n) ? n : d;
}

function arr(v) {
  if (v === undefined || v === null) return undefined;
  return (Array.isArray(v) ? v : [v]).map((x) => (typeof x === "string" ? parseFloat(x) : x));
}

/** Parse one instance's DICOM JSON into the modules Cornerstone asks for. */
export function registerInstance(studyUid, seriesUid, m) {
  const sop = tagValue(m, "00080018");
  const frames = num(tagValue(m, "00280008"), 1);
  const rows = num(tagValue(m, "00280010"), 0);
  const cols = num(tagValue(m, "00280011"), 0);
  const ps = arr(tagValue(m, "00280030")) || [1, 1];
  const iop = arr(tagValue(m, "00200037"));
  const ipp = arr(tagValue(m, "00200032"));
  const info = {
    studyUid, seriesUid, sop, frames, rows, cols,
    transferSyntax: tagValue(m, "00020010") || "1.2.840.10008.1.2.1",
    bitsAllocated: num(tagValue(m, "00280100"), 16),
    bitsStored: num(tagValue(m, "00280101"), 16),
    highBit: num(tagValue(m, "00280102"), 15),
    pixelRepresentation: num(tagValue(m, "00280103"), 0),
    samplesPerPixel: num(tagValue(m, "00280002"), 1),
    photometric: tagValue(m, "00280004") || "MONOCHROME2",
    slope: num(tagValue(m, "00281053"), 1),
    intercept: num(tagValue(m, "00281052"), 0),
    wc: arr(tagValue(m, "00281050")),
    ww: arr(tagValue(m, "00281051")),
    modality: tagValue(m, "00080060") || "OT",
    rowSpacing: ps[0] || 1, colSpacing: ps[1] || ps[0] || 1,
    iop, ipp,
    sliceThickness: num(tagValue(m, "00180050"), undefined),
    frameOfReference: tagValue(m, "00200052"),
    instanceNumber: num(tagValue(m, "00200013"), 0),
    raw: m,
  };
  const ids = [];
  for (let f = 1; f <= frames; f += 1) {
    const id = `aranmed:${studyUid}/${seriesUid}/${sop}/${f}`;
    meta.set(id, { ...info, frame: f });
    ids.push(id);
  }
  return { ids, info };
}

export function metaFor(imageId) { return meta.get(imageId); }

function metadataProvider(type, imageId) {
  const m = meta.get(imageId);
  if (!m) return undefined;
  switch (type) {
    case "imagePixelModule":
      return {
        samplesPerPixel: m.samplesPerPixel, photometricInterpretation: m.photometric,
        rows: m.rows, columns: m.cols, bitsAllocated: m.bitsAllocated, bitsStored: m.bitsStored,
        highBit: m.highBit, pixelRepresentation: m.pixelRepresentation,
      };
    case "generalSeriesModule":
      return { modality: m.modality, seriesInstanceUID: m.seriesUid, studyInstanceUID: m.studyUid };
    case "imagePlaneModule": {
      const iop = m.iop && m.iop.length === 6 ? m.iop : null;
      return {
        frameOfReferenceUID: m.frameOfReference, rows: m.rows, columns: m.cols,
        imageOrientationPatient: iop || [1, 0, 0, 0, 1, 0],
        rowCosines: iop ? iop.slice(0, 3) : [1, 0, 0],
        columnCosines: iop ? iop.slice(3, 6) : [0, 1, 0],
        imagePositionPatient: m.ipp && m.ipp.length === 3 ? m.ipp : [0, 0, m.instanceNumber || 0],
        pixelSpacing: [m.rowSpacing, m.colSpacing],
        rowPixelSpacing: m.rowSpacing, columnPixelSpacing: m.colSpacing,
        sliceThickness: m.sliceThickness,
      };
    }
    case "voiLutModule":
      return m.wc && m.ww ? { windowCenter: m.wc, windowWidth: m.ww } : undefined;
    case "modalityLutModule":
      return { rescaleSlope: m.slope, rescaleIntercept: m.intercept };
    case "transferSyntax":
      return { transferSyntaxUID: m.transferSyntax };
    case "generalImageModule":
      return { instanceNumber: m.instanceNumber };
    default:
      return undefined;
  }
}

function firstPart(buf, contentType) {
  const mm = /boundary="?([^";,]+)"?/i.exec(contentType || "");
  const bytes = new Uint8Array(buf);
  if (!mm) return bytes;
  // Find the header/body separator after the first boundary, then the next boundary.
  const sep = [13, 10, 13, 10];
  let start = -1;
  for (let i = 0; i < Math.min(bytes.length, 8192); i += 1) {
    if (bytes[i] === sep[0] && bytes[i + 1] === sep[1] && bytes[i + 2] === sep[2] && bytes[i + 3] === sep[3]) {
      start = i + 4; break;
    }
  }
  const header = new TextDecoder().decode(bytes.subarray(0, Math.max(0, start)));
  const len = /content-length:\s*(\d+)/i.exec(header);
  if (start >= 0 && len) return bytes.subarray(start, start + parseInt(len[1], 10));
  const tail = new TextEncoder().encode(`\r\n--${mm[1]}`);
  for (let i = bytes.length - tail.length; i > start; i -= 1) {
    let ok = true;
    for (let j = 0; j < tail.length; j += 1) if (bytes[i + j] !== tail[j]) { ok = false; break; }
    if (ok) return bytes.subarray(start, i);
  }
  return bytes.subarray(start);
}

async function fetchNative(m) {
  const url = apiUrl(`${DICOMWEB}/studies/${m.studyUid}/series/${m.seriesUid}/instances/${m.sop}/frames/${m.frame}`);
  const r = await fetch(url, {
    headers: { Authorization: `Bearer ${token}`,
               Accept: 'multipart/related; type="application/octet-stream"' },
  });
  if (!r.ok) throw new Error(`frame HTTP ${r.status}`);
  const part = firstPart(await r.arrayBuffer(), r.headers.get("content-type"));
  const copy = part.slice().buffer;
  if (m.samplesPerPixel > 1 || m.bitsAllocated === 8) return { data: new Uint8Array(copy), color: m.samplesPerPixel > 1 };
  return { data: m.pixelRepresentation === 1 ? new Int16Array(copy) : new Uint16Array(copy), color: false };
}

async function fetchRendered(m) {
  const url = apiUrl(`${DICOMWEB}/studies/${m.studyUid}/series/${m.seriesUid}/instances/${m.sop}/frames/${m.frame}/rendered`);
  const r = await fetch(url, { headers: { Authorization: `Bearer ${token}`, Accept: "image/png" } });
  if (!r.ok) throw new Error(`rendered HTTP ${r.status}`);
  const bmp = await createImageBitmap(await r.blob());
  const c = document.createElement("canvas");
  c.width = bmp.width; c.height = bmp.height;
  const ctx = c.getContext("2d");
  ctx.drawImage(bmp, 0, 0);
  const rgba = ctx.getImageData(0, 0, c.width, c.height).data;
  const gray = new Uint8Array(c.width * c.height);
  for (let i = 0; i < gray.length; i += 1) gray[i] = rgba[i * 4];
  return { data: gray, color: false, rendered: true, width: c.width, height: c.height };
}

function loadImage(imageId) {
  const promise = (async () => {
    const m = meta.get(imageId);
    if (!m) throw new Error(`no metadata for ${imageId}`);
    const native = NATIVE_TS.has(m.transferSyntax);
    const px = native ? await fetchNative(m) : await fetchRendered(m);
    const width = px.width || m.cols;
    const height = px.height || m.rows;
    let scalar;
    let slope = 1;
    let intercept = 0;
    if (px.color) {
      scalar = px.data;
    } else if (px.rendered) {
      scalar = new Float32Array(px.data);
    } else {
      // Pre-apply the modality LUT so values are in modality units (HU for CT):
      // windowing presets, probes and ROI statistics then read true values.
      slope = m.slope; intercept = m.intercept;
      scalar = new Float32Array(px.data.length);
      for (let i = 0; i < px.data.length; i += 1) scalar[i] = px.data[i] * slope + intercept;
    }
    let min = Infinity;
    let max = -Infinity;
    for (let i = 0; i < scalar.length; i += 1) {
      const v = scalar[i];
      if (v < min) min = v;
      if (v > max) max = v;
    }
    const wc = px.rendered ? 127.5 : (m.wc ? m.wc[0] : (min + max) / 2);
    const ww = px.rendered ? 255 : (m.ww ? m.ww[0] : Math.max(1, max - min));
    const numberOfComponents = px.color ? 3 : 1;
    const voxelManager = cs.utilities.VoxelManager.createImageVoxelManager({
      width, height, scalarData: scalar, numberOfComponents,
    });
    return {
      imageId, minPixelValue: min, maxPixelValue: max, slope: 1, intercept: 0,
      windowCenter: wc, windowWidth: ww, voiLUTFunction: "LINEAR",
      getPixelData: () => scalar, getCanvas: undefined,
      rows: height, columns: width, height, width,
      color: px.color, rgba: false, numberOfComponents,
      columnPixelSpacing: m.colSpacing, rowPixelSpacing: m.rowSpacing,
      invert: m.photometric === "MONOCHROME1", photometricInterpretation: px.color ? "RGB" : m.photometric,
      sizeInBytes: scalar.byteLength, dataType: scalar.constructor.name,
      isPreScaled: !px.color && !px.rendered,
      preScale: { enabled: true, scaled: !px.color && !px.rendered,
                  scalingParameters: { modality: m.modality, rescaleSlope: slope, rescaleIntercept: intercept } },
      voxelManager,
    };
  })();
  return { promise };
}

function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch (_) { return false; }
}

export async function ensureInit() {
  if (initialised) return { cs, cst };
  await cs.init();
  if (process.env.NEXT_PUBLIC_VIEWER_CPU === "1" || !webglAvailable()) {
    cs.setUseCPURendering(true);
  }
  cs.registerImageLoader("aranmed", loadImage);
  cs.metaData.addProvider(metadataProvider, 10000);
  cst.init();
  [cst.WindowLevelTool, cst.PanTool, cst.ZoomTool, cst.StackScrollTool, cst.LengthTool,
   cst.AngleTool, cst.EllipticalROITool, cst.RectangleROITool, cst.ProbeTool,
   cst.PlanarRotateTool].forEach((T) => { try { cst.addTool(T); } catch (_) { /* already added */ } });
  initialised = true;
  return { cs, cst };
}

export { cs, cst };
