import Head from "next/head";
import { useRouter } from "next/router";
import { useEffect, useState } from "react";
import { useAuth } from "../lib/auth";
import { fetchWithTimeout } from "../lib/api";
import { apiUrl } from "../lib/config";
import { useT } from "../lib/i18n";
import { XRay, Stethoscope, GradCap, Brain, User } from "../components/icons";

const ROLE_OPTIONS = [
  { id: "radiologist", icon: XRay,        descKey: "auth.roleDesc.radiologist" },
  { id: "doctor",      icon: Stethoscope, descKey: "auth.roleDesc.doctor" },
  { id: "resident",    icon: Brain,       descKey: "auth.roleDesc.resident" },
  { id: "student",     icon: GradCap,     descKey: "auth.roleDesc.student" },
];

export default function LoginPage() {
  const { user, login, register, ready } = useAuth();
  const { t, lang, setLang } = useT();
  const router = useRouter();
  const [step, setStep] = useState("role"); // "role" -> "credentials"
  const [mode, setMode] = useState("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // Roles a visitor may register as themselves; others are created by an admin.
  const [selfRoles, setSelfRoles] = useState(["student"]);

  useEffect(() => {
    fetchWithTimeout(apiUrl("/api/auth/register-policy"), {}, 8000)
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => { if (j && Array.isArray(j.self_register_roles)) setSelfRoles(j.self_register_roles); })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (ready && user) router.replace("/");
  }, [ready, user, router]);

  // Restore last chosen role so returning users skip a click.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const saved = window.localStorage.getItem("asr.role");
    if (saved && ROLE_OPTIONS.some((r) => r.id === saved)) setRole(saved);
  }, []);

  function pickRole(id) {
    setRole(id);
    if (typeof window !== "undefined") window.localStorage.setItem("asr.role", id);
    setStep("credentials");
  }

  async function submit(e) {
    e.preventDefault();
    setErr(""); setBusy(true);
    try {
      if (mode === "login") await login(username, password);
      else await register({ username, password, email: email || null, role: role || selfRoles[0] });
      router.replace("/");
    } catch (e) {
      setErr(e.message || t("auth.error"));
    } finally { setBusy(false); }
  }

  const activeRole = ROLE_OPTIONS.find((r) => r.id === role);
  const canSelfRegister = !role ? selfRoles.length > 0 : selfRoles.includes(role);

  return (
    <>
      <Head>
        <title>aranmed · {t("auth.title")}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
      </Head>
      <div className="auth-shell">
        <div className={`auth-card ${step === "role" ? "wide" : ""}`}>
          <div className="auth-head">
            <div className="brand-mark big">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
                   strokeLinejoin="round">
                <path d="M6 3v6a4 4 0 0 0 8 0V3" />
                <path d="M10 19a4 4 0 0 0 8 0v-3" />
                <circle cx="18" cy="15" r="2" />
              </svg>
            </div>
            <div>
              <h1>aranmed</h1>
              <p className="muted">Bilingual Radiology AI Workbench</p>
            </div>
          </div>

          <div className="auth-lang">
            <button className={`btn-pill ${lang === "en" ? "active" : ""}`}
                    onClick={() => setLang("en")}>EN</button>
            <button className={`btn-pill ${lang === "fa" ? "active" : ""}`}
                    onClick={() => setLang("fa")}>FA</button>
          </div>

          {step === "role" ? (
            <>
              <h2>{t("auth.pickRole")}</h2>
              <p className="muted role-hint">{t("auth.pickRoleSub")}</p>
              <div className="role-grid">
                {ROLE_OPTIONS.map(({ id, icon: Ico, descKey }) => (
                  <button key={id} type="button" data-role={id}
                          className={`role-card ${role === id ? "selected" : ""}`}
                          onClick={() => pickRole(id)}>
                    <span className="role-ico"><Ico size={22} /></span>
                    <span className="role-name">{t(`auth.role.${id}`)}</span>
                    <span className="role-desc">{t(descKey)}</span>
                  </button>
                ))}
              </div>
            </>
          ) : (
            <>
              {activeRole && (
                <button type="button" className="role-chip" onClick={() => setStep("role")}
                        title={t("auth.changeRole")}>
                  <activeRole.icon size={14} />
                  <span>{t(`auth.role.${role}`)}</span>
                  <span className="role-chip-edit">{t("auth.changeRole")}</span>
                </button>
              )}

              <h2>{mode === "login" ? t("auth.signIn") : t("auth.register")}</h2>

              <form onSubmit={submit} className="auth-form">
                <label className="lbl">{t("auth.username")}</label>
                <input className="input" value={username} required minLength={3}
                       onChange={(e) => setUsername(e.target.value)} autoFocus />

                <label className="lbl">{t("auth.password")}</label>
                <input className="input" type="password" value={password}
                       required minLength={mode === "register" ? 6 : 1}
                       onChange={(e) => setPassword(e.target.value)} />

                {mode === "register" && <>
                  <label className="lbl">{t("auth.email")}</label>
                  <input className="input" type="email" value={email}
                         onChange={(e) => setEmail(e.target.value)} />
                </>}

                {err && <div className="err">{err}</div>}

                <button className="btn primary big" type="submit" disabled={busy}>
                  {busy ? <span className="spinner" />
                        : (mode === "login" ? t("auth.signIn") : t("auth.register"))}
                </button>
              </form>

              {canSelfRegister || mode === "register" ? (
                <button className="btn-link"
                        onClick={() => setMode(mode === "login" ? "register" : "login")}>
                  {mode === "login" ? t("auth.toRegister") : t("auth.toLogin")}
                </button>
              ) : (
                <p className="muted role-hint" data-testid="admin-creates">{t("auth.adminCreates")}</p>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}
