import Head from "next/head";
import { useEffect, useRef, useState } from "react";
import Sidebar from "../components/Sidebar";
import ThemeToggle from "../components/ThemeToggle";
import ReportsView from "../components/views/ReportsView";
import TemplatesView from "../components/views/TemplatesView";
import ModelsView from "../components/views/ModelsView";
import SettingsView from "../components/views/SettingsView";
import RadiologyView from "../components/views/RadiologyView";
import EhrView from "../components/views/EhrView";
import AlertsView from "../components/views/AlertsView";
import EducationView from "../components/views/EducationView";
import {
  Mic, Stop, Upload, Sparkle, Copy, Download, Document, Globe, User, LogOut,
} from "../components/icons";
import { useAuth } from "../lib/auth";
import { useT } from "../lib/i18n";
import { fetchWithTimeout } from "../lib/api";
import { apiUrl, SERVER_BACKEND } from "../lib/config";
import { canAccessView, defaultViewForRole, ROLE_LABELS } from "../lib/roles";
import CriticalAlertsBanner, {
  TemplateMismatchBanner,
} from "../components/CriticalAlertsBanner";

// ------------------------------------------------------------------
// Server‑side data fetching (still uses environment variable)
// ------------------------------------------------------------------
export async function getServerSideProps() {
  const base = SERVER_BACKEND;
  let templates = [], health = null;
  try {
    const [tr, hr] = await Promise.all([
      fetch(`${base}/api/templates`).then((r) => r.json()),
      fetch(`${base}/api/health`).then((r) => r.json()),
    ]);
    templates = tr.templates || [];
    health = hr;
  } catch (_) {}
  return { props: { initialTemplates: templates, initialHealth: health } };
}

// ------------------------------------------------------------------
// Helper
// ------------------------------------------------------------------
function fmtTime(s) {
  const m = Math.floor(s / 60), r = s % 60;
  return `${m}:${r.toString().padStart(2, "0")}`;
}

// ------------------------------------------------------------------
// Main component
// ------------------------------------------------------------------
export default function Home({ initialTemplates, initialHealth }) {
  const { user, logout } = useAuth();
  const { t, lang, setLang } = useT();
  const [templates, setTemplates] = useState(initialTemplates || []);
  const [health, setHealth] = useState(initialHealth);
  const [templateId, setTemplateId] = useState(initialTemplates?.[0]?.id || "");
  const [transcript, setTranscript] = useState("");
  const [report, setReport] = useState("");
  const [criticalAlerts, setCriticalAlerts] = useState([]);
  const [templateMismatch, setTemplateMismatch] = useState(null);
  const [language, setLanguage] = useState("");
  const [extraContext, setExtraContext] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [audioUrl, setAudioUrl] = useState("");
  const [audioBlob, setAudioBlob] = useState(null);
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [view, setView] = useState("dictate");
  const [menuOpen, setMenuOpen] = useState(false);

  const userRole = user?.role || "doctor";

  // Keep the active view inside what this role is allowed to see.
  useEffect(() => {
    if (!user) return;
    if (!canAccessView(userRole, view)) {
      setView(defaultViewForRole(userRole));
    }
  }, [user, userRole, view]);

  const mediaRef = useRef(null);
  const chunksRef = useRef([]);
  const timerRef = useRef(null);

  // ------------------------------------------------------------------
  // Refresh templates & health on client (short timeout, direct URL)
  // ------------------------------------------------------------------
  useEffect(() => {
    fetchWithTimeout(apiUrl("/api/templates"), {}, 30000)
      .then((r) => r.json())
      .then((d) => {
        const t = d.templates || [];
        setTemplates(t);
        if (!templateId && t[0]) setTemplateId(t[0].id);
      })
      .catch(() => {});
    fetchWithTimeout(apiUrl("/api/health"), {}, 30000)
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => {});
  }, []);

  // Recorder timer
  useEffect(() => {
    if (recording) {
      setElapsed(0);
      timerRef.current = setInterval(() => setElapsed((s) => s + 1), 1000);
    } else {
      clearInterval(timerRef.current);
    }
    return () => clearInterval(timerRef.current);
  }, [recording]);

  function pickFile(e) {
    const f = e.target.files?.[0];
    if (!f) return;
    setAudioBlob(f);
    setAudioUrl(URL.createObjectURL(f));
  }

  async function startRec() {
    setErr("");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream);
      chunksRef.current = [];
      mr.ondataavailable = (e) => e.data.size && chunksRef.current.push(e.data);
      mr.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: "audio/webm" });
        setAudioBlob(blob);
        setAudioUrl(URL.createObjectURL(blob));
        stream.getTracks().forEach((t) => t.stop());
      };
      mr.start();
      mediaRef.current = mr;
      setRecording(true);
    } catch (e) {
      setErr("Microphone access denied: " + e.message);
    }
  }
  function stopRec() { mediaRef.current?.stop(); setRecording(false); }

  // ------------------------------------------------------------------
  // Transcribe – direct backend call with 120s timeout
  // ------------------------------------------------------------------
  async function doTranscribe() {
    if (!audioBlob) { setErr("Record or upload audio first."); return; }
    setErr(""); setBusy("Transcribing…");
    try {
      const fd = new FormData();
      fd.append("file", audioBlob, "dictation.webm");
      if (language) fd.append("language", language);
      const r = await fetchWithTimeout(apiUrl("/api/transcribe"), { method: "POST", body: fd }, 300000);
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      setTranscript(d.text);
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  // ------------------------------------------------------------------
  // Report from transcript – direct backend call
  // ------------------------------------------------------------------
  async function doReport() {
    if (!transcript.trim()) { setErr("No transcript yet."); return; }
    if (!templateId)        { setErr("Pick a template.");   return; }
    setErr(""); setBusy("Generating report…");
    try {
      const r = await fetchWithTimeout(apiUrl("/api/report"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          transcript, template_id: templateId,
          extra_context: extraContext || null,
        }),
      }, 120000);
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      setReport(d.report);
      setCriticalAlerts(d.critical_alerts || []);
      setTemplateMismatch(d.template_mismatch || null);
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  // ------------------------------------------------------------------
  // Dictate (ASR + report) – direct backend call
  // ------------------------------------------------------------------
  async function doDictate() {
    if (!audioBlob) { setErr("Record or upload audio first."); return; }
    if (!templateId){ setErr("Pick a template.");             return; }
    setErr(""); setBusy("ASR + LLM pipeline…");
    try {
      const fd = new FormData();
      fd.append("file", audioBlob, "dictation.webm");
      fd.append("template_id", templateId);
      if (language)     fd.append("language", language);
      if (extraContext) fd.append("extra_context", extraContext);
      const r = await fetchWithTimeout(apiUrl("/api/dictate"), { method: "POST", body: fd }, 300000);
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      setReport(d.report);
      setCriticalAlerts(d.critical_alerts || []);
      setTemplateMismatch(d.template_mismatch || null);
      // Back‑fill transcript (non‑blocking)
      try {
        const fd2 = new FormData();
        fd2.append("file", audioBlob, "dictation.webm");
        if (language) fd2.append("language", language);
        const tr = await fetchWithTimeout(apiUrl("/api/transcribe"), { method: "POST", body: fd2 }, 300000);
        if (tr.ok) setTranscript((await tr.json()).text);
      } catch (_) {}
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------
  return (
    <>
      <Head>
        <title>aranmed · Bilingual Radiology Reporter</title>
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
      </Head>

      <div className="app">
        <div className={`scrim ${menuOpen ? "show" : ""}`} onClick={() => setMenuOpen(false)} />
        <Sidebar active={view} open={menuOpen} role={userRole}
                 onSelect={(id) => { setView(id); setMenuOpen(false); }} />

        <div className="main">
          <header className="topbar">
            <button className="menu-btn" onClick={() => setMenuOpen(true)} aria-label="Open menu">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
            </button>
            <h1>{view === "dictate" ? "Dictation Workbench"
                : view === "radiology" ? "Radiology Vision Chat"
                : view === "reports" ? "Saved Reports"
                : view === "templates" ? "Report Templates"
                : view === "ehr" ? "Electronic Health Record"
                : view === "alerts" ? "Medication Alerts"
                : view === "education" ? "Education Tutor"
                : view === "asr" ? "OmniASR Model"
                : view === "llm" ? "Qwen LLM"
                : view === "vision" ? "Radiology-Infer-Mini"
                : "Settings"}</h1>
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

          <main className="content">
            {view === "reports"   && <ReportsView />}
            {view === "templates" && <TemplatesView />}
            {view === "radiology" && <RadiologyView />}
            {view === "ehr"       && <EhrView />}
            {view === "alerts"    && <AlertsView />}
            {view === "education" && <EducationView />}
            {view === "asr"       && <ModelsView kind="asr" />}
            {view === "llm"       && <ModelsView kind="llm" />}
            {view === "vision"    && <ModelsView kind="vision" />}
            {view === "settings"  && <SettingsView />}

            {view === "dictate" && <>
            <div className="page-head">
              <h2 className="page-title">{t("dictate.title")}</h2>
              <p className="page-sub">
                {userRole === "radiologist" ? t("dictate.sub.radiologist") : t("dictate.sub")}
              </p>
            </div>

            <div className="grid">

              {/* Capture card */}
              <section className="card">
                <div className="card-head">
                  <span className="step">1</span>
                  <h2>Capture</h2>
                  <span className="muted">audio in → transcript</span>
                </div>

                <div className="recorder">
                  <button
                    className={`mic-btn ${recording ? "recording" : ""}`}
                    onClick={recording ? stopRec : startRec}
                    aria-label={recording ? "Stop recording" : "Start recording"}
                  >
                    {recording ? <Stop size={22} /> : <Mic size={22} />}
                  </button>
                  <div className="recorder-meta">
                    <div className="t">
                      {recording ? "Recording…" : audioBlob ? "Audio ready" : "Tap mic to start"}
                    </div>
                    <div className="s">
                      {recording ? fmtTime(elapsed) : audioBlob ? "Edit context, then run pipeline." : "Or upload an existing file."}
                    </div>
                  </div>
                  <div className="grow" />
                  <label className="upload-btn">
                    <Upload size={14} /> Upload
                    <input type="file" accept="audio/*"
                           onChange={pickFile} style={{ display: "none" }} />
                  </label>
                </div>

                {audioUrl && <audio controls src={audioUrl} />}

                <label className="lbl">Language hint <span className="muted">(optional)</span></label>
                <input className="input" type="text" value={language}
                       onChange={(e) => setLanguage(e.target.value)}
                       placeholder="e.g. fa, en — leave blank to auto-detect" />

                <label className="lbl">Report template</label>
                <select className="select" value={templateId}
                        onChange={(e) => setTemplateId(e.target.value)}>
                  {templates.map((t) => (
                    <option key={t.id} value={t.id}>{t.name}</option>
                  ))}
                </select>

                <label className="lbl">Clinical context <span className="muted">(optional)</span></label>
                <textarea className="textarea" value={extraContext}
                          onChange={(e) => setExtraContext(e.target.value)}
                          style={{ minHeight: 90 }}
                          placeholder="Prior comparisons, indication, allergies…" />

                <div className="toolbar">
                  <button className="btn ghost" onClick={doTranscribe} disabled={!!busy}>
                    <Sparkle size={14} /> Transcribe only
                  </button>
                  <button className="btn" onClick={doDictate} disabled={!!busy}>
                    {busy ? <span className="spinner" /> : <Sparkle size={14} />}
                    Dictate → Report
                  </button>
                </div>

                {busy && <div className="muted" style={{ marginTop: 10 }}>
                  <span className="spinner dark" style={{ display: "inline-block", marginRight: 8, verticalAlign: "middle" }} />
                  {busy}
                </div>}
                {err && <div className="err">{err}</div>}

                <div className="kpis">
                  <div className="kpi"><span>ASR model</span><b>{health?.asr_model?.split("/").pop() || "—"}</b></div>
                  <div className="kpi"><span>LLM model</span><b>{health?.core_model || health?.ollama_model || "—"}</b></div>
                </div>
              </section>

              {/* Output card */}
              <section className="card">
                <div className="card-head">
                  <span className="step">2</span>
                  <h2>Transcript &amp; Report</h2>
                  <span className="muted">edit · review · sign</span>
                </div>

                <label className="lbl">Transcript <span className="muted">(editable)</span></label>
                <textarea className="textarea" dir="auto" value={transcript}
                          onChange={(e) => setTranscript(e.target.value)}
                          placeholder="ASR output appears here. Mixed Persian + English is supported." />
                <div className="toolbar">
                  <button className="btn" onClick={doReport} disabled={!!busy}>
                    <Document size={14} /> Generate report from transcript
                  </button>
                </div>

                <CriticalAlertsBanner alerts={criticalAlerts} />
                <TemplateMismatchBanner mismatch={templateMismatch} />

                <hr className="hr" />

                <div className="card-head" style={{ marginBottom: 10 }}>
                  <span className="step">3</span>
                  <h2>Structured report</h2>
                  {report && (
                    <div className="row" style={{ marginLeft: "auto" }}>
                      <button className="btn ghost icon-only"
                              title="Save to Reports"
                              onClick={() => {
                                const list = JSON.parse(localStorage.getItem("asr.reports") || "[]");
                                list.unshift({ ts: Date.now(), template_id: templateId, report });
                                localStorage.setItem("asr.reports", JSON.stringify(list.slice(0, 50)));
                              }}>
                        <Document size={14} />
                      </button>
                      <button className="btn ghost icon-only"
                              title="Copy"
                              onClick={() => navigator.clipboard.writeText(report)}>
                        <Copy size={14} />
                      </button>
                      <button className="btn ghost icon-only" title="Download .txt"
                              onClick={() => {
                                const blob = new Blob([report], { type: "text/plain" });
                                const a = document.createElement("a");
                                a.href = URL.createObjectURL(blob);
                                a.download = `${templateId}-report.txt`;
                                a.click();
                              }}>
                        <Download size={14} />
                      </button>
                    </div>
                  )}
                </div>

                <div className={`report ${report ? "" : "empty"}`}>
                  {report || "The signed-ready report will appear here."}
                </div>
              </section>
            </div>
            </>}
          </main>
        </div>
      </div>
    </>
  );
}