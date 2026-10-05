// Cornerstone3D bootstrap for the AranMed viewer (browser only).
//
// We register our own image loader ("aranmed:") instead of the stock DICOM
// image loader: our DICOMweb server already parses and decodes the files, so
// the viewer only needs (1) WADO-RS metadata, which feeds a metadata
// provider, and (2) frames from /frames/N?normalize=1. Those arrive decoded
// from any transfer syntax: JPEG baseline/extended/lossless, JPEG-LS, JPEG
// 2000, HTJ2K, RLE, Big Endian and Deflate. Palette colour and YBR come as
// RGB, 1-bit as 8-bit. Each frame carries its own rescale/window, so Enhanced
// multi-frame is exact. That keeps WASM codecs and web workers out of the
// Next.js build while every device's images show true modality values.
// If a frame cannot be decoded, the server-rendered PNG is the fallback.
import * as cs from "@cornerstonejs/core";
import * as cst from "@cornerstonejs/tools";
import { apiUrl } from "../../lib/config";
import { DICOMWEB, tagValue } from "../../lib/pacs";

export const VIDEO_TS = new Set([
  "1.2.840.10008.1.2.4.100", "1.2.840.10008.1.2.4.100.1", "1.2.840.10008.1.2.4.101",
  "1.2.840.10008.1.2.4.101.1", "1.2.840.10008.1.2.4.102", "1.2.840.10008.1.2.4.102.1",
  "1.2.840.10008.1.2.4.103", "1.2.840.10008.1.2.4.103.1", "1.2.840.10008.1.2.4.104",
  "1.2.840.10008.1.2.4.104.1", "1.2.840.10008.1.2.4.105", "1.2.840.10008.1.2.4.105.1",
  "1.2.840.10008.1.2.4.106", "1.2.840.10008.1.2.4.106.1", "1.2.840.10008.1.2.4.107",
  "1.2.840.10008.1.2.4.108",
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
  const photometric = tagValue(m, "00280004") || "MONOCHROME2";
  const spp = num(tagValue(m, "00280002"), 1);
  // Palette colour and YBR are delivered as RGB by the server; 1-bit as 8-bit.
  const rgb = spp > 1 || photometric === "PALETTE COLOR";
  const bits = num(tagValue(m, "00280100"), 16);
  const info = {
    studyUid, seriesUid, sop, frames, rows, cols,
    transferSyntax: tagValue(m, "00020010") || "1.2.840.10008.1.2.1",
    bitsAllocated: rgb || bits === 1 ? 8 : bits,
    bitsStored: rgb || bits === 1 ? 8 : num(tagValue(m, "00280101"), 16),
    highBit: rgb || bits === 1 ? 7 : num(tagValue(m, "00280102"), 15),
    pixelRepresentation: num(tagValue(m, "00280103"), 0),
    samplesPerPixel: rgb ? 3 : 1,
    photometric: rgb ? "RGB" : photometric,
    video: VIDEO_TS.has(tagValue(m, "00020010") || ""),
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

const ARRAYS = { u8: Uint8Array, i8: Int8Array, u16: Uint16Array, i16: Int16Array,
                 u32: Uint32Array, i32: Int32Array, f32: Float32Array };

/** One decoded frame from /frames/N?normalize=1 with its per-frame parameters. */
async function fetchDisplay(m) {
  const url = apiUrl(`${DICOMWEB}/studies/${m.studyUid}/series/${m.seriesUid}/instances/${m.sop}/frames/${m.frame}?normalize=1`);
  const r = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) throw new Error(`frame HTTP ${r.status}`);
  const h = (k) => r.headers.get(k);
  const Arr = ARRAYS[h("X-Pixel-Format")] || Uint16Array;
  const data = new Arr(await r.arrayBuffer());
  const f = (k) => (h(k) !== null && h(k) !== "" ? parseFloat(h(k)) : undefined);
  return {
    data, color: h("X-Samples-Per-Pixel") === "3",
    width: parseInt(h("X-Columns"), 10) || m.cols, height: parseInt(h("X-Rows"), 10) || m.rows,
    slope: f("X-Rescale-Slope") ?? m.slope, intercept: f("X-Rescale-Intercept") ?? m.intercept,
    wc: f("X-Window-Center"), ww: f("X-Window-Width"), photometric: h("X-Photometric") || m.photometric,
  };
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
    let px;
    try {
      px = await fetchDisplay(m);
    } catch (e) {
      px = await fetchRendered(m);   // undecodable on the server: show its best PNG
    }
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
      // Enhanced multi-frame objects have a slope/intercept per frame.
      slope = px.slope ?? m.slope; intercept = px.intercept ?? m.intercept;
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
    const wc = px.rendered || px.color ? 127.5 : (px.wc ?? (m.wc ? m.wc[0] : (min + max) / 2));
    const ww = px.rendered || px.color ? 255 : (px.ww ?? (m.ww ? m.ww[0] : Math.max(1, max - min)));
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
      invert: (px.photometric || m.photometric) === "MONOCHROME1",
      photometricInterpretation: px.color ? "RGB" : (px.photometric || m.photometric),
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
