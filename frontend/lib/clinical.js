// Clinical EHR / transfers / EMS / interop API helpers (same-origin /api proxy).
import { apiFetch, fetchWithTimeout } from "./api";
import { apiUrl } from "./config";
import { qs } from "./pacs";

const J = (body) => JSON.stringify(body);

export const clinicalApi = {
  search: (params, token) => apiFetch(`/api/clinical/patients${qs(params)}`, {}, token),
  register: (body, token) => apiFetch("/api/clinical/patients", { method: "POST", body: J(body) }, token),
  chart: (id, params, token) => apiFetch(`/api/clinical/patients/${id}/chart${qs(params)}`, {}, token),
  timeline: (id, params, token) => apiFetch(`/api/clinical/patients/${id}/timeline${qs(params)}`, {}, token),
  add: (id, type, body, token) =>
    apiFetch(`/api/clinical/patients/${id}/${type}`, { method: "POST", body: J(body) }, token),
  patch: (type, rid, changes, version, token) =>
    apiFetch(`/api/clinical/resources/${type}/${rid}`, { method: "PATCH", body: J({ changes, version }) }, token),
  remove: (type, rid, token) => apiFetch(`/api/clinical/resources/${type}/${rid}`, { method: "DELETE" }, token),
  document: (docId, token) => apiFetch(`/api/clinical/documents/${docId}`, {}, token),
  remoteDocument: (personId, facility, docId, token) =>
    apiFetch(`/api/clinical/patients/${personId}/remote-documents/${encodeURIComponent(facility)}/${encodeURIComponent(docId)}`, {}, token),
  breakGlass: (id, reason, token) =>
    apiFetch(`/api/clinical/patients/${id}/break-glass`, { method: "POST", body: J({ reason }) }, token),
  ask: (id, question, includeRemote, token) =>
    apiFetch(`/api/clinical/patients/${id}/ask`, { method: "POST", body: J({ question, include_remote: includeRemote }) }, token),
  discover: (id, purpose, token) => apiFetch(`/api/interop/discover/${id}${qs({ purpose })}`, {}, token),
};

export const transferApi = {
  list: (params, token) => apiFetch(`/api/transfers${qs(params)}`, {}, token),
  get: (id, token) => apiFetch(`/api/transfers/${id}`, {}, token),
  create: (body, token) => apiFetch("/api/transfers", { method: "POST", body: J(body) }, token),
  step: (id, action, body, token) =>
    apiFetch(`/api/transfers/${id}/${action}`, { method: "POST", body: J(body || {}) }, token),
};

export const emsApi = {
  board: (params, token) => apiFetch(`/api/ems/board${qs(params)}`, {}, token),
  status: (id, status, token) => apiFetch(`/api/ems/${id}/${status}`, { method: "POST" }, token),
};

export const interopApi = {
  status: (token) => apiFetch("/api/interop/status", {}, token),
  capabilities: (token) => apiFetch("/api/interop/capabilities", {}, token),
  facilities: (token) => apiFetch("/api/facilities", {}, token),
  saveFacility: (body, token) => apiFetch("/api/facilities", { method: "POST", body: J(body) }, token),
  deleteFacility: (oid, token) => apiFetch(`/api/facilities/${oid}`, { method: "DELETE" }, token),
  testPeer: (oid, token) => apiFetch("/api/interop/peers/test", { method: "POST", body: J({ oid }) }, token),
  messages: (params, token) => apiFetch(`/api/interop/messages${qs(params)}`, {}, token),
  mpiLinks: (token) => apiFetch("/api/mpi/links", {}, token),
  resolveLink: (id, accept, token) =>
    apiFetch(`/api/mpi/links/${id}/resolve${qs({ accept })}`, { method: "POST" }, token),
  async hl7(message, token) {
    const r = await fetchWithTimeout(apiUrl("/api/interop/hl7"), {
      method: "POST", body: message,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "x-application/hl7-v2+er7" },
    }, 60000);
    if (!r.ok) throw new Error(await r.text());
    return r.text();
  },
  async importCda(xml, token) {
    const r = await fetchWithTimeout(apiUrl("/api/interop/cda"), {
      method: "POST", body: xml,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/xml" },
    }, 60000);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  async exportCda(personId, token) {
    const r = await fetchWithTimeout(apiUrl(`/api/interop/cda/${personId}`), {
      headers: { Authorization: `Bearer ${token}` },
    }, 60000);
    if (!r.ok) throw new Error(await r.text());
    return r.blob();
  },
};

export function personLabel(p) {
  if (!p) return "—";
  return p.name || [p.demographics?.given, p.demographics?.family].filter(Boolean).join(" ") || "(unnamed)";
}

export function ageFrom(birth) {
  if (!birth) return null;
  const y = parseInt(String(birth).slice(0, 4), 10);
  if (!y) return null;
  return new Date().getFullYear() - y;
}

export function fmtDate(v) {
  if (!v) return "—";
  if (typeof v === "number") return new Date(v * 1000).toLocaleString();
  return String(v).replace("T", " ").replace(/:00Z?$/, "").slice(0, 16);
}

export const TRANSFER_STEPS = ["requested", "accepted", "in_transit", "arrived", "completed"];
