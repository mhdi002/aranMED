import { useEffect, useRef, useState } from "react";
import {
  Mic, Stop, Upload, Sparkle, Copy, Download, Document,
} from "../icons";
import { useT } from "../../lib/i18n";
import { fetchWithTimeout } from "../../lib/api";
import { apiUrl } from "../../lib/config";
import CriticalAlertsBanner, { TemplateMismatchBanner } from "../CriticalAlertsBanner";
import { Button, Card, CardHead, Field, PageHeader, Toolbar } from "../ui";

function fmtTime(s) {
  const m = Math.floor(s / 60), r = s % 60;
  return `${m}:${r.toString().padStart(2, "0")}`;
}

/**
 * Flagship reference implementation for the ui/ component library — the
 * record -> transcribe -> report request flow below is unchanged from the
 * pre-redesign implementation (backend/app.py's ASR + report-generation
 * pipeline is out of scope for this pass); only the markup/styling moved
 * to shared components.
 */
export default function DictateView({ initialTemplates, initialHealth, userRole }) {
  const { t } = useT();
  const [templates, setTemplates] = useState(initialTemplates || []);
  const [health, setHealth] = useState(initialHealth);
  const [templateId, setTemplateId] = useState("");
  const [autoSelected, setAutoSelected] = useState(false);
  const [selectionConfidence, setSelectionConfidence] = useState(null);
  const [selectionReason, setSelectionReason] = useState("");
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

  const mediaRef = useRef(null);
  const chunksRef = useRef([]);
  const timerRef = useRef(null);

  useEffect(() => {
    fetchWithTimeout(apiUrl("/api/templates"), {}, 30000)
      .then((r) => r.json())
      .then((d) => setTemplates(d.templates || []))
      .catch(() => {});
    fetchWithTimeout(apiUrl("/api/health"), {}, 30000)
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
        stream.getTracks().forEach((tr) => tr.stop());
      };
      mr.start();
      mediaRef.current = mr;
      setRecording(true);
    } catch (e) {
      setErr("Microphone access denied: " + e.message);
    }
  }
  function stopRec() { mediaRef.current?.stop(); setRecording(false); }

  async function suggestTemplate(text) {
    if (!text.trim()) return;
    try {
      const r = await fetchWithTimeout(apiUrl("/api/templates/suggest"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transcript: text, top_k: 1 }),
      }, 30000);
      if (!r.ok) return;
      const d = await r.json();
      const top = (d.suggestions || [])[0];
      if (top) {
        setTemplateId(top.template_id);
        setAutoSelected(true);
        setSelectionConfidence(top.confidence);
        setSelectionReason(top.detail || top.matched_on || "");
      }
    } catch (_) {}
  }

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
      await suggestTemplate(d.text);
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  async function doReport() {
    if (!transcript.trim()) { setErr("No transcript yet."); return; }
    setErr(""); setBusy("Generating report…");
    try {
      const r = await fetchWithTimeout(apiUrl("/api/report"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          transcript, template_id: templateId || null,
          extra_context: extraContext || null,
        }),
      }, 120000);
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      setReport(d.report);
      setCriticalAlerts(d.critical_alerts || []);
      setTemplateMismatch(d.template_mismatch || null);
      setTemplateId(d.template_id);
      setAutoSelected(!!d.auto_selected_template);
      setSelectionConfidence(d.template_selection_confidence ?? null);
      setSelectionReason(d.template_selection_reason || "");
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  async function doDictate() {
    if (!audioBlob) { setErr("Record or upload audio first."); return; }
    setErr(""); setBusy("ASR + LLM pipeline…");
    try {
      const fd = new FormData();
      fd.append("file", audioBlob, "dictation.webm");
      if (templateId)   fd.append("template_id", templateId);
      if (language)     fd.append("language", language);
      if (extraContext) fd.append("extra_context", extraContext);
      const r = await fetchWithTimeout(apiUrl("/api/dictate"), { method: "POST", body: fd }, 300000);
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      setReport(d.report);
      setCriticalAlerts(d.critical_alerts || []);
      setTemplateMismatch(d.template_mismatch || null);
      setTemplateId(d.template_id);
      setAutoSelected(!!d.auto_selected_template);
      setSelectionConfidence(d.template_selection_confidence ?? null);
      setSelectionReason(d.template_selection_reason || "");
      try {
        const fd2 = new FormData();
        fd2.append("file", audioBlob, "dictation.webm");
        if (language) fd2.append("language", language);
        const tr = await fetchWithTimeout(apiUrl("/api/transcribe"), { method: "POST", body: fd2 }, 300000);
        if (tr.ok) setTranscript((await tr.json()).text);
      } catch (_) {}
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  return (
    <>
      <PageHeader
        title={t("dictate.title")}
        subtitle={userRole === "radiologist" ? t("dictate.sub.radiologist") : t("dictate.sub")}
      />

      <div className="grid">
        <Card>
          <CardHead step={1} title="Capture" hint="audio in → transcript" />

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
              <input type="file" accept="audio/*" onChange={pickFile} style={{ display: "none" }} />
            </label>
          </div>

          {audioUrl && <audio controls src={audioUrl} />}

          <Field label="Language hint" hint="optional">
            <input className="input" type="text" value={language}
                   onChange={(e) => setLanguage(e.target.value)}
                   placeholder="e.g. fa, en — leave blank to auto-detect" />
          </Field>

          <Field
            label="Report template"
            hint={
              templateId
                ? autoSelected
                  ? `auto-selected${selectionConfidence != null ? ` · ${Math.round(selectionConfidence * 100)}% confidence` : ""}`
                  : "manually selected"
                : "auto-selected from dictation if left blank"
            }
          >
            <select
              className="select"
              value={templateId}
              onChange={(e) => { setTemplateId(e.target.value); setAutoSelected(false); }}
            >
              <option value="">— auto-select from dictation —</option>
              {templates.map((tpl) => (
                <option key={tpl.id} value={tpl.id}>{tpl.name}</option>
              ))}
            </select>
          </Field>

          <Field label="Clinical context" hint="optional">
            <textarea className="textarea" value={extraContext}
                      onChange={(e) => setExtraContext(e.target.value)}
                      style={{ minHeight: 90 }}
                      placeholder="Prior comparisons, indication, allergies…" />
          </Field>

          <Toolbar>
            <Button variant="ghost" onClick={doTranscribe} disabled={!!busy}>
              <Sparkle size={14} /> Transcribe only
            </Button>
            <Button onClick={doDictate} loading={!!busy}>
              <Sparkle size={14} /> Dictate → Report
            </Button>
          </Toolbar>

          {busy && (
            <div className="muted" style={{ marginTop: 10, display: "flex", alignItems: "center", gap: 8 }}>
              <span className="spinner dark" /> {busy}
            </div>
          )}
          {err && <div className="err">{err}</div>}

          <div className="kpis">
            <div className="kpi"><span>ASR model</span><b>{health?.asr_model?.split("/").pop() || "—"}</b></div>
            <div className="kpi"><span>LLM model</span><b>{health?.core_model || health?.ollama_model || "—"}</b></div>
          </div>
        </Card>

        <Card>
          <CardHead step={2} title="Transcript & Report" hint="edit · review · sign" />

          <Field label="Transcript" hint="editable">
            <textarea className="textarea" dir="auto" value={transcript}
                      onChange={(e) => setTranscript(e.target.value)}
                      placeholder="ASR output appears here. Mixed Persian + English is supported." />
          </Field>
          <Toolbar>
            <Button onClick={doReport} disabled={!!busy} data-testid="dictate-generate">
              <Document size={14} /> Generate report from transcript
            </Button>
          </Toolbar>

          <CriticalAlertsBanner alerts={criticalAlerts} />
          <TemplateMismatchBanner mismatch={templateMismatch} />

          <hr className="hr" />

          <CardHead
            step={3}
            title="Structured report"
            right={report && (
              <div className="row" style={{ marginLeft: "auto" }}>
                <Button variant="ghost" iconOnly title="Save to Reports" data-testid="save-report"
                        onClick={() => {
                          const list = JSON.parse(localStorage.getItem("asr.reports") || "[]");
                          list.unshift({ ts: Date.now(), template_id: templateId, report });
                          localStorage.setItem("asr.reports", JSON.stringify(list.slice(0, 50)));
                        }}>
                  <Document size={14} />
                </Button>
                <Button variant="ghost" iconOnly title="Copy" onClick={() => navigator.clipboard.writeText(report)}>
                  <Copy size={14} />
                </Button>
                <Button variant="ghost" iconOnly title="Download .txt"
                        onClick={() => {
                          const blob = new Blob([report], { type: "text/plain" });
                          const a = document.createElement("a");
                          a.href = URL.createObjectURL(blob);
                          a.download = `${templateId}-report.txt`;
                          a.click();
                        }}>
                  <Download size={14} />
                </Button>
              </div>
            )}
          />

          <div className={`report ${report ? "" : "empty"}`}>
            {report || "The signed-ready report will appear here."}
          </div>
        </Card>
      </div>
    </>
  );
}
