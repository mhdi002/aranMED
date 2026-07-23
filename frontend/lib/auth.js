// Authentication context + fetch wrapper.
//
//   const { user, token, login, register, logout, ready } = useAuth();
//   const data = await apiFetch("/api/ehr", { method: "GET" }, token);
//
// On startup we read the JWT-like token from localStorage and call
// /api/auth/me to revalidate; if revalidation fails we drop the token so
// the user is bounced to the login screen.
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { useRouter } from "next/router";
import { fetchWithTimeout } from "./api";
import { apiUrl } from "./config";

const Ctx = createContext({
  user: null, token: null, ready: false,
  login: async () => {}, register: async () => {}, logout: () => {},
});

const TOKEN_KEY = "asr.token";

export async function apiFetch(url, opts = {}, token = null) {
  const headers = new Headers(opts.headers || {});
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  // Only set JSON content-type when sending JSON body (not FormData).
  if (opts.body && !(opts.body instanceof FormData) &&
      !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const r = await fetchWithTimeout(url, { ...opts, headers }, 15000);
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      detail = j.detail || JSON.stringify(j);
    } catch (_) {}
    throw new Error(detail);
  }
  if (r.status === 204) return null;
  const ct = r.headers.get("content-type") || "";
  return ct.includes("application/json") ? r.json() : r.text();
}

export function AuthProvider({ children }) {
  const router = useRouter();
  const [token, setToken] = useState(null);
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const t = window.localStorage.getItem(TOKEN_KEY);
    if (!t) { setReady(true); return; }
    apiFetch(apiUrl("/api/auth/me"), { method: "GET" }, t)
      .then((u) => { setUser(u); setToken(t); })
      .catch(() => { window.localStorage.removeItem(TOKEN_KEY); })
      .finally(() => setReady(true));
  }, []);

  const _store = useCallback((data) => {
    setToken(data.access_token);
    setUser(data.user);
    if (typeof window !== "undefined")
      window.localStorage.setItem(TOKEN_KEY, data.access_token);
  }, []);

  const login = useCallback(async (username, password) => {
    const fd = new FormData();
    fd.append("username", username);
    fd.append("password", password);
    const r = await fetchWithTimeout(apiUrl("/api/auth/login"), { method: "POST", body: fd }, 15000);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || "login failed");
    }
    _store(await r.json());
  }, [_store]);

  const register = useCallback(async (payload) => {
    const data = await apiFetch(apiUrl("/api/auth/register"),
                                { method: "POST", body: JSON.stringify(payload) });
    _store(data);
  }, [_store]);

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
    if (typeof window !== "undefined")
      window.localStorage.removeItem(TOKEN_KEY);
    router.push("/login");
  }, [router]);

  return (
    <Ctx.Provider value={{ user, token, ready, login, register, logout }}>
      {children}
    </Ctx.Provider>
  );
}

export const useAuth = () => useContext(Ctx);
