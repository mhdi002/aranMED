import { useEffect, useState } from "react";
import { Button, ConfirmModal, Field, useToast } from "../ui";
import { useAuth } from "../../lib/auth";
import { apiFetch } from "../../lib/api";
import { useT } from "../../lib/i18n";
import { useTheme } from "../../lib/theme";
import { clearSavedReports } from "../../lib/reports";
import UsersAdmin from "../settings/UsersAdmin";

function getLocal(key) {
  try { return localStorage.getItem(key) || ""; } catch (_) { return ""; }
}
function setLocal(key, value) {
  try {
    if (value) localStorage.setItem(key, value); else localStorage.removeItem(key);
    return true;
  } catch (_) { return false; }
}

export default function SettingsView() {
  const { user, token, setSession } = useAuth();
  const { t } = useT();
  const { theme, setTheme } = useTheme();
  const toast = useToast();
  const [asrLang, setAsrLang] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [frontBase, setFrontBase] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);
  const [pw, setPw] = useState({ current: "", next: "", again: "" });
  const [pwBusy, setPwBusy] = useState(false);
  const [pwErr, setPwErr] = useState("");

  useEffect(() => {
    setAsrLang(getLocal("asr.language"));
    setFrontBase(window.location.origin);
    setApiBase(process.env.NEXT_PUBLIC_BACKEND_URL || t("settings.sameOrigin"));
  }, [t]);

  function save() {
    if (setLocal("asr.language", asrLang.trim())) toast.success(t("common.saved"));
    else toast.error(t("common.storageUnavailable"));
  }
  function reset() {
    clearSavedReports();
    setLocal("asr.language", "");
    setTheme("light");
    setAsrLang("");
    setConfirmReset(false);
    toast.success(t("settings.resetDone"));
  }

  async function changePassword(e) {
    e.preventDefault();
    setPwErr("");
    if (pw.next !== pw.again) { setPwErr(t("settings.pwMismatch")); return; }
    setPwBusy(true);
    try {
      const d = await apiFetch("/api/auth/password", {
        method: "POST", body: JSON.stringify({ current_password: pw.current, new_password: pw.next }),
      }, token);
      setSession(d);  // the old token ended with the change; keep working with the new one
      setPw({ current: "", next: "", again: "" });
      toast.success(t("settings.pwChanged"));
    } catch (err) { setPwErr(err.message); } finally { setPwBusy(false); }
  }

  return (
    <>
      <div className="card" style={{ width: "100%" }}>
        <div className="card-head">
          <span className="step" aria-hidden="true">⚙</span>
          <h2>{t("settings.title")}</h2>
          <span className="muted">{t("settings.sub")}</span>
        </div>

        <div className="lbl" id="theme-label">{t("settings.theme")}</div>
        <div className="row" role="group" aria-labelledby="theme-label">
          <button className={`btn ${theme === "light" ? "" : "ghost"}`} aria-pressed={theme === "light"}
                  onClick={() => setTheme("light")}>{t("topbar.light")}</button>
          <button className={`btn ${theme === "dark" ? "" : "ghost"}`} aria-pressed={theme === "dark"}
                  onClick={() => setTheme("dark")}>{t("topbar.dark")}</button>
        </div>

        <Field label={t("settings.asrLang")} hint={t("common.optional")}>
          <input className="input" type="text" value={asrLang}
                 onChange={(e) => setAsrLang(e.target.value)}
                 placeholder={t("dictate.langPlaceholder")} />
        </Field>

        <div className="toolbar">
          <button className="btn" onClick={save}>{t("common.save")}</button>
          <button className="btn ghost" onClick={() => setConfirmReset(true)}>{t("settings.reset")}</button>
        </div>

        <hr className="hr" />

        <form onSubmit={changePassword} aria-labelledby="pw-title">
          <h3 id="pw-title" className="lbl" style={{ fontSize: "var(--text-md)" }}>{t("settings.pwTitle")}</h3>
          <Field label={t("settings.pwCurrent")}>
            <input className="input" type="password" autoComplete="current-password" required
                   value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} />
          </Field>
          <Field label={t("settings.pwNew")} hint={t("settings.pwHint")}>
            <input className="input" type="password" autoComplete="new-password" required minLength={6}
                   value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} />
          </Field>
          <Field label={t("settings.pwAgain")}>
            <input className="input" type="password" autoComplete="new-password" required
                   value={pw.again} onChange={(e) => setPw({ ...pw, again: e.target.value })} />
          </Field>
          {pwErr && <div className="err" role="alert">{pwErr}</div>}
          <div className="toolbar">
            <Button type="submit" loading={pwBusy}>{t("settings.pwChange")}</Button>
          </div>
          <p className="muted small">{t("settings.pwNote")}</p>
        </form>

        <hr className="hr" />

        <div className="lbl">{t("settings.backend")}</div>
        <div className="kpis">
          <div className="kpi"><span>API</span><b>{apiBase}</b></div>
          <div className="kpi"><span>{t("settings.frontend")}</span><b>{frontBase}</b></div>
        </div>
        <p className="muted" style={{ marginTop: 10, lineHeight: 1.6 }}>
          {t("settings.envNote")} <code>.env.example</code>
        </p>

        <ConfirmModal
          open={confirmReset}
          title={t("settings.resetTitle")}
          danger
          confirmLabel={t("settings.resetConfirm")}
          onCancel={() => setConfirmReset(false)}
          onConfirm={reset}
        >
          {t("settings.resetText")}
        </ConfirmModal>
      </div>
      {user && user.role === "admin" && <UsersAdmin />}
    </>
  );
}
