// lib/api.js
import { apiUrl } from "./config";

export async function fetchWithTimeout(resource, options = {}, timeoutMs = 120000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(resource, { ...options, signal: controller.signal });
    clearTimeout(timeout);
    return response;
  } catch (error) {
    clearTimeout(timeout);
    if (error.name === 'AbortError') {
      throw new Error(`Request timed out after ${timeoutMs / 1000} seconds`);
    }
    throw error;
  }
}

// Enhanced apiFetch that uses absolute backend URL and includes auth token
export async function apiFetch(endpoint, options = {}, token = null) {
  const url = endpoint.startsWith("http") ? endpoint : apiUrl(endpoint);
  const headers = { ...(options.headers || {}) };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  if (!(options.body instanceof FormData) && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  const response = await fetchWithTimeout(url, { ...options, headers }, 60000);
  if (!response.ok) {
    let errorDetail = await response.text().catch(() => `HTTP ${response.status}`);
    throw new Error(errorDetail);
  }
  return response.json();
}