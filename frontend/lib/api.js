// The one HTTP client for the backend API.
//
//   import { apiFetch, ApiError } from "../lib/api";
//   const data = await apiFetch("/api/ehr", { method: "GET" }, token);
//
// - Relative paths go through apiUrl() (same-origin /api in the browser).
// - The bearer token is attached when given.
// - JSON bodies get a JSON content type; FormData is left alone.
// - Responses: JSON, text, or null for 204.
// - Errors throw ApiError with .status, .detail (the server's "detail") and
//   .code (detail.code when the server sends a structured detail).
// - A 401 on an authenticated request means the session ended (expired,
//   revoked, password changed): the registered handler signs the user out.
import { apiUrl } from "./config";

export class ApiError extends Error {
  constructor(status, detail, message) {
    super(message || (typeof detail === "string" ? detail : `HTTP ${status}`));
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = detail && typeof detail === "object" ? detail.code : undefined;
  }
}

let unauthorizedHandler = null;

/** Called by AuthProvider: what to do when the session is no longer valid. */
export function onUnauthorized(fn) {
  unauthorizedHandler = fn;
}

export async function fetchWithTimeout(resource, options = {}, timeoutMs = 120000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(resource, { ...options, signal: options.signal || controller.signal });
  } catch (error) {
    if (error.name === "AbortError") {
      throw new ApiError(0, null, `Request timed out after ${Math.round(timeoutMs / 1000)} seconds`);
    }
    throw new ApiError(0, null, error.message || "Network error");
  } finally {
    clearTimeout(timeout);
  }
}

async function errorFrom(r) {
  let detail = `${r.status} ${r.statusText}`.trim();
  let message;
  try {
    const ct = r.headers.get("content-type") || "";
    if (ct.includes("json")) {
      const j = await r.json();
      detail = j.detail !== undefined ? j.detail : j;
      if (typeof detail === "object" && detail) {
        message = detail.message || detail.msg || (Array.isArray(detail)
          ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
          : JSON.stringify(detail));
      }
      if (j.error_id) message = `${message || detail} (error id ${j.error_id})`;
    } else {
      const t = await r.text();
      if (t) detail = t.slice(0, 500);
    }
  } catch (_) {
    // keep the status line
  }
  return new ApiError(r.status, detail, message);
}

export async function apiFetch(endpoint, options = {}, token = null) {
  const { timeoutMs = 60000, ...opts } = options;
  const url = endpoint.startsWith("http") ? endpoint : apiUrl(endpoint);
  const headers = new Headers(opts.headers || {});
  if (token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  if (opts.body && !(opts.body instanceof FormData) && !(opts.body instanceof Blob)
      && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const r = await fetchWithTimeout(url, { ...opts, headers }, timeoutMs);
  if (!r.ok) {
    const err = await errorFrom(r);
    if (r.status === 401 && token && unauthorizedHandler) unauthorizedHandler(err);
    throw err;
  }
  if (r.status === 204) return null;
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

/** Raw response (for blobs/streams) with the same auth and error handling. */
export async function apiRaw(endpoint, options = {}, token = null) {
  const { timeoutMs = 60000, ...opts } = options;
  const url = endpoint.startsWith("http") ? endpoint : apiUrl(endpoint);
  const headers = new Headers(opts.headers || {});
  if (token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  const r = await fetchWithTimeout(url, { ...opts, headers }, timeoutMs);
  if (!r.ok) {
    const err = await errorFrom(r);
    if (r.status === 401 && token && unauthorizedHandler) unauthorizedHandler(err);
    throw err;
  }
  return r;
}
