import { useEffect, useState } from "react";
import { useRouter } from "next/router";
import Head from "next/head";
import Sidebar from "./Sidebar";
import ThemeToggle from "./ThemeToggle";
import { Globe, User, LogOut } from "./icons";
import { useAuth } from "../lib/auth";
import { useT } from "../lib/i18n";
import { canAccessView, defaultViewForRole, ROLE_LABELS } from "../lib/roles";
import { VIEW_TITLES, routeForView, viewForPathname } from "../lib/viewMeta";

/**
 * Shared app shell (sidebar + topbar + role gating) for every real,
 * deep-linkable view route — replaces the single `/` + `view` useState
 * switcher that used to live in pages/index.js. Each page under
 * frontend/pages/{view}.js renders <AppShell>{...}</AppShell>.
 */
export default function AppShell({ children, title }) {
  const { user, logout } = useAuth();
  const { t, lang, setLang } = useT();
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);

  const userRole = user?.role || "doctor";
  const view = viewForPathname(router.pathname);

  useEffect(() => {
    if (!user) return;
    if (!canAccessView(userRole, view)) {
      router.replace(routeForView(defaultViewForRole(userRole)));
    }
  }, [user, userRole, view, router]);

  const heading = title || VIEW_TITLES[view] || "aranmed";

  return (
    <>
      <Head>
        <title>{heading} · aranmed</title>
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
      </Head>

      <div className="app">
        <div className={`scrim ${menuOpen ? "show" : ""}`} onClick={() => setMenuOpen(false)} />
        <Sidebar
          active={view}
          open={menuOpen}
          role={userRole}
          onSelect={(id) => { router.push(routeForView(id)); setMenuOpen(false); }}
        />

        <div className="main">
          <header className="topbar">
            <button className="menu-btn" onClick={() => setMenuOpen(true)} aria-label="Open menu">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
            </button>
            <h1>{heading}</h1>
            <span className="crumb">/ {view}</span>
            <div className="topbar-spacer" />
            <div className="lang-pick" title={t("topbar.lang")}>
              <Globe size={14} />
              <button className={`btn-pill ${lang === "en" ? "active" : ""}`}
                      onClick={() => setLang("en")}>EN</button>
              <button className={`btn-pill ${lang === "fa" ? "active" : ""}`}
                      onClick={() => setLang("fa")}>FA</button>
            </div>
            <ThemeToggle />
            {user && (
              <div className="user-chip" title={user.username}>
                <User size={14} />
                <span className="muted">{user.username}</span>
                <span className="role-badge">{ROLE_LABELS[userRole] || userRole}</span>
                <button className="btn ghost icon-only" title={t("nav.logout")}
                        onClick={logout}><LogOut size={14} /></button>
              </div>
            )}
          </header>

          <main className="content">{children}</main>
        </div>
      </div>
    </>
  );
}
