import { useEffect, useState } from "react";

export default function SettingsView() {
  const [theme, setTheme] = useState("light");
  const [lang, setLang] = useState("");
  const [savedAt, setSavedAt] = useState(0);
  const [apiBase, setApiBase] = useState("/api (same origin)");
  const [frontBase, setFrontBase] = useState("");

  useEffect(() => {
    setTheme(localStorage.getItem("theme") || "light");
    setLang(localStorage.getItem("asr.language") || "");
    setFrontBase(window.location.origin);
    if (process.env.NEXT_PUBLIC_BACKEND_URL) {
      setApiBase(process.env.NEXT_PUBLIC_BACKEND_URL);
    }
  }, []);

  function applyTheme(t) {
    setTheme(t);
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem("theme", t);
  }
  function save() {
    localStorage.setItem("asr.language", lang);
    setSavedAt(Date.now());
  }
  function reset() {
    if (!confirm("Clear all local app data (saved reports, theme, preferences)?")) return;
    localStorage.removeItem("asr.reports");
    localStorage.removeItem("asr.language");
    localStorage.removeItem("theme");
    applyTheme("light");
    setLang("");
  }

  return (
    <div className="card" style={{ width: "100%" }}>
      <div className="card-head">
        <span className="step">⚙</span>
        <h2>Settings</h2>
        <span className="muted">workspace preferences</span>
      </div>

      <label className="lbl">Theme</label>
      <div className="row">
        <button className={`btn ${theme === "light" ? "" : "ghost"}`}
                onClick={() => applyTheme("light")}>Light</button>
        <button className={`btn ${theme === "dark" ? "" : "ghost"}`}
                onClick={() => applyTheme("dark")}>Dark</button>
      </div>

      <label className="lbl">Default ASR language hint</label>
      <input className="input" type="text" value={lang}
             onChange={(e) => setLang(e.target.value)}
             placeholder="e.g. fa, en — leave blank to auto-detect" />

      <div className="toolbar">
        <button className="btn" onClick={save}>Save</button>
        <button className="btn ghost" onClick={reset}>Reset workspace</button>
      </div>
      {savedAt > 0 && (
        <p className="muted" style={{ marginTop: 10 }}>Saved.</p>
      )}

      <hr className="hr" />

      <label className="lbl">Backend</label>
      <div className="kpis">
        <div className="kpi"><span>API</span><b>{apiBase}</b></div>
        <div className="kpi"><span>Front-end</span><b>{frontBase}</b></div>
      </div>
      <p className="muted" style={{ marginTop: 10, lineHeight: 1.6 }}>
        Endpoints and model tags are configured via environment variables — see{" "}
        <code>.env.example</code> in the project root. Restart the backend after editing.
      </p>
    </div>
  );
}
