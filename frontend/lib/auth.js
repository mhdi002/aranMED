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
import { apiFetch, onUnauthorized } from "./api";

// Kept for existing imports: the single client lives in lib/api.js.
export { apiFetch } from "./api";

const Ctx = createContext({
  user: null, token: null, ready: false,
  login: async () => {}, register: async () => {}, logout: () => {}, setSession: () => {},
});

const TOKEN_KEY = "asr.token";

export function AuthProvider({ children }) {
  const router = useRouter();
  const [token, setToken] = useState(null);
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const t = window.localStorage.getItem(TOKEN_KEY);
    if (!t) { setReady(true); return; }
    apiFetch("/api/auth/me", { method: "GET", timeoutMs: 15000 }, t)
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

  const login = useCallback(async (username, password, code) => {
    const fd = new FormData();
    fd.append("username", username);
    fd.append("password", password);
    if (code) fd.append("client_secret", code);
    _store(await apiFetch("/api/auth/login", { method: "POST", body: fd, timeoutMs: 15000 }));
  }, [_store]);

  const register = useCallback(async (payload) => {
    const data = await apiFetch("/api/auth/register",
                                { method: "POST", body: JSON.stringify(payload), timeoutMs: 15000 });
    if (data && data.access_token) _store(data);
    return data;
  }, [_store]);

  const clear = useCallback(() => {
    setToken(null);
    setUser(null);
    if (typeof window !== "undefined")
      window.localStorage.removeItem(TOKEN_KEY);
  }, []);

  const logout = useCallback(() => {
    const t = token;
    clear();
    // Revoke the token server-side too; signing out must not leave it usable.
    if (t) apiFetch("/api/auth/logout", { method: "POST", timeoutMs: 8000 }, t).catch(() => {});
    router.push("/login");
  }, [router, token, clear]);

  // Any API call that comes back 401 with this session's token means the
  // session ended (expired, revoked, password changed): back to sign-in.
  useEffect(() => {
    onUnauthorized(() => {
      clear();
      if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
        router.push("/login?expired=1");
      }
    });
    return () => onUnauthorized(null);
  }, [clear, router]);

  return (
    <Ctx.Provider value={{ user, token, ready, login, register, logout, setSession: _store }}>
      {children}
    </Ctx.Provider>
  );
}

export const useAuth = () => useContext(Ctx);
