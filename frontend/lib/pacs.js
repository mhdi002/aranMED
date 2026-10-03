// PACS client helpers: /api/pacs management calls + DICOMweb (/api/dicom-web).
// All requests go through the same-origin /api proxy (lib/config.apiUrl) with
// the user's bearer token; nothing here names a host.
import { apiFetch, fetchWithTimeout } from "./api";
import { apiUrl } from "./config";

export const DICOMWEB = process.env.NEXT_PUBLIC_DICOMWEB_PREFIX || "/api/dicom-web";

export function qs(params = {}) {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") p.append(k, v);
  });
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const pacsApi = {
  studies: (params, token) => apiFetch(`/api/pacs/studies${qs(params)}`, {}, token),
  study: (uid, token) => apiFetch(`/api/pacs/studies/${uid}`, {}, token),
  patchStudy: (uid, body, token) =>
    apiFetch(`/api/pacs/studies/${uid}`, { method: "PATCH", body: JSON.stringify(body) }, token),
  deleteStudy: (uid, token) => apiFetch(`/api/pacs/studies/${uid}`, { method: "DELETE" }, token),
  verify: (uid, token) => apiFetch(`/api/pacs/studies/${uid}/verify`, {}, token),
  saveReport: (uid, body, token) =>
    apiFetch(`/api/pacs/studies/${uid}/reports`, { method: "POST", body: JSON.stringify(body) }, token),
  analyze: (uid, body, token) =>
    apiFetch(`/api/pacs/studies/${uid}/analyze`, { method: "POST", body: JSON.stringify(body || {}) }, token),
  ask: (uid, question, token) =>
    apiFetch(`/api/pacs/studies/${uid}/ask`, { method: "POST", body: JSON.stringify({ question }) }, token),
  stats: (token) => apiFetch("/api/pacs/stats", {}, token),
  worklist: (params, token) => apiFetch(`/api/pacs/worklist${qs(params)}`, {}, token),
  createWorklist: (body, token) =>
    apiFetch("/api/pacs/worklist", { method: "POST", body: JSON.stringify(body) }, token),
  patchWorklist: (id, body, token) =>
    apiFetch(`/api/pacs/worklist/${id}`, { method: "PATCH", body: JSON.stringify(body) }, token),
  mpps: (token) => apiFetch("/api/pacs/mpps", {}, token),
  nodes: (token) => apiFetch("/api/pacs/nodes", {}, token),
  saveNode: (body, id, token) =>
    apiFetch(id ? `/api/pacs/nodes/${id}` : "/api/pacs/nodes",
      { method: id ? "PUT" : "POST", body: JSON.stringify(body) }, token),
  deleteNode: (id, token) => apiFetch(`/api/pacs/nodes/${id}`, { method: "DELETE" }, token),
  echo: (id, token) => apiFetch(`/api/pacs/nodes/${id}/echo`, { method: "POST" }, token),
  queryNode: (id, filters, token) =>
    apiFetch(`/api/pacs/nodes/${id}/query`, { method: "POST", body: JSON.stringify({ filters }) }, token),
  retrieve: (body, token) =>
    apiFetch("/api/pacs/retrieve", { method: "POST", body: JSON.stringify(body) }, token),
  send: (body, token) => apiFetch("/api/pacs/send", { method: "POST", body: JSON.stringify(body) }, token),
  jobs: (token) => apiFetch("/api/pacs/jobs", {}, token),
};

/** Multipart upload with progress (XHR: fetch has no upload progress). */
export function uploadDicom(files, token, onProgress) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", apiUrl("/api/pacs/upload"));
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
      else reject(new Error(xhr.responseText || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("network error"));
    xhr.send(fd);
  });
}

/** Authenticated fetch of a DICOMweb resource as a Blob object URL (thumbnails). */
export async function dicomwebObjectUrl(path, token, accept = "image/jpeg") {
  const r = await fetchWithTimeout(apiUrl(`${DICOMWEB}${path}`), {
    headers: { Authorization: `Bearer ${token}`, Accept: accept },
  }, 60000);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return URL.createObjectURL(await r.blob());
}

export async function dicomwebJson(path, token) {
  const r = await fetchWithTimeout(apiUrl(`${DICOMWEB}${path}`), {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/dicom+json" },
  }, 120000);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ---- DICOM JSON helpers ----------------------------------------------------
export function tagValue(meta, tag) {
  const e = meta?.[tag];
  if (!e || !e.Value) return undefined;
  return e.Value.length === 1 ? e.Value[0] : e.Value;
}

export function pnString(v) {
  if (!v) return "";
  if (typeof v === "string") return v.replace(/\^/g, " ").trim();
  if (v.Alphabetic) return v.Alphabetic.replace(/\^/g, " ").trim();
  return String(v);
}

export function fmtDicomDate(d) {
  if (!d || d.length !== 8) return d || "—";
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
}

export function fmtBytes(n) {
  if (!n && n !== 0) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`;
}

export const STATUS_TONE = {
  received: "info", in_review: "warn", read: "warn", reported: "ok", final: "ok",
  archived: "muted", scheduled: "info", in_progress: "warn", completed: "ok",
  discontinued: "err", cancelled: "muted", draft: "warn", preliminary: "info",
  amended: "info", done: "ok", failed: "err", queued: "muted", running: "info",
};

export const WINDOW_PRESETS = [
  { id: "default", label: "Default", c: null, w: null },
  { id: "soft", label: "Soft tissue", c: 40, w: 400 },
  { id: "lung", label: "Lung", c: -600, w: 1500 },
  { id: "bone", label: "Bone", c: 400, w: 1800 },
  { id: "brain", label: "Brain", c: 40, w: 80 },
  { id: "liver", label: "Liver", c: 60, w: 160 },
  { id: "mediastinum", label: "Mediastinum", c: 50, w: 350 },
];
