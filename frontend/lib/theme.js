import { createContext, useCallback, useContext, useEffect, useState } from "react";

// One theme state for the whole app (top-bar toggle and Settings used to
// keep separate copies and disagreed). pages/_document.js applies the stored
// choice before first paint, so this only mirrors what is already on <html>.
const Ctx = createContext({ theme: "light", setTheme: () => {} });
const KEY = "theme";

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState("light");

  useEffect(() => {
    const current = document.documentElement.getAttribute("data-theme");
    let stored = null;
    try { stored = localStorage.getItem(KEY); } catch (_) { /* private mode */ }
    const initial = current || stored
      || (window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    setThemeState(initial);
    document.documentElement.setAttribute("data-theme", initial);
  }, []);

  const setTheme = useCallback((next) => {
    setThemeState(next);
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(KEY, next); } catch (_) { /* private mode */ }
  }, []);

  return <Ctx.Provider value={{ theme, setTheme }}>{children}</Ctx.Provider>;
}

export const useTheme = () => useContext(Ctx);
