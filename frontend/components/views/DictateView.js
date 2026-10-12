import { useEffect, useRef, useState } from "react";
import {
  Mic, Stop, Upload, Sparkle, Copy, Download, Document,
} from "../icons";
import { useT } from "../../lib/i18n";
import { useAuth } from "../../lib/auth";
import { apiFetch } from "../../lib/api";
import { loadSavedReports, saveReports } from "../../lib/reports";
import CriticalAlertsBanner, { TemplateMismatchBanner } from "../CriticalAlertsBanner";
import { Button, Card, CardHead, Field, PageHeader, Toolbar, useToast } from "../ui";

function fmtTime(s) {
  const m = Math.floor(s / 60), r = s % 60;
  return `${m}:${r.toString().padStart(2, "0")}`;
}

function readAsrLanguage() {
  try { return localStorage.getItem("asr.language") || ""; } catch (_) { return ""; }
}

/**
 * Dictation workbench: record or upload audio -> transcript -> structured
 * report in the chosen (or auto-selected) template.
 */
export default function DictateView({ userRole }) {
  const { t } = useT();
  const { token } = useAuth();
  const toast = useToast();
  const [templates, setTemplates] = useState([]);
  const [templatesErr, setTemplatesErr] = useState("");
  const [health, setHealth] = useState(null);
  const [templateId, setTemplateId] = useState("");
  const [autoSelected, setAutoSelected] = useState(false);
  const [selectionConfidence, setSelectionConfidence] = useState(null);
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
  const fileRef = useRef(null);

  // The default language hint from Settings.
  useEffect(() => { setLanguage(readAsrLanguage()); }, []);

  useEffect(() => {
    if (!token) return;
    apiFetch("/api/templates", { timeoutMs: 30000 }, token)
      .then((d) => { setTemplates(d.templates || []); setTemplatesErr(""); })
      .catch((e) => setTemplatesErr(e.message));
    apiFetch("/api/health", { timeoutMs: 30000 }, token).then(setHealth).catch(() => setHealth(null));
  }, [token]);

  useEffect(() => {
    if (recording) {
      setElapsed(0);
      timerRef.current = setInterval(() => setElapsed((s) => s + 1), 1000);
    } else {
      clearInterval(timerRef.current);
    }
    return () => clearInterval(timerRef.current);
  }, [recording]);

  // Release the previous recording's object URL when it is replaced.
  useEffect(() => () => { if (audioUrl) URL.revokeObjectURL(audioUrl); }, [audioUrl]);

  function setAudio(blob) {
    setAudioBlob(blob);
    setAudioUrl(blob ? URL.createObjectURL(blob) : "");
  }

  function pickFile(e) {
    const f = e.target.files?.[0];
    if (f) setAudio(f);
  }

  async function startRec() {
    setErr("");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream);
      chunksRef.current = [];
      mr.ondataavailable = (e) => e.data.size && chunksRef.current.push(e.data);
      mr.onstop = () => {
        setAudio(new Blob(chunksRef.current, { type: "audio/webm" }));
        stream.getTracks().forEach((tr) => tr.stop());
      };
      mr.start();
      mediaRef.current = mr;
      setRecording(true);
    } catch (e) {
      setErr(t("dictate.micDenied", { error: e.message }));
    }
  }
  function stopRec() { mediaRef.current?.stop(); setRecording(false); }

  function applyReport(d) {
    setReport(d.report);
    setCriticalAlerts(d.critical_alerts || []);
    setTemplateMismatch(d.template_mismatch || null);
    setTemplateId(d.template_id);
    setAutoSelected(!!d.auto_selected_template);
    setSelectionConfidence(d.template_selection_confidence ?? null);
  }

  async function suggestTemplate(text) {
    if (!text.trim() || templateId) return;
    try {
      const d = await apiFetch("/api/templates/suggest", {
        method: "POST", body: JSON.stringify({ transcript: text, top_k: 1 }), timeoutMs: 30000,
      }, token);
      const top = (d.suggestions || [])[0];
      if (top) {
        setTemplateId(top.template_id);
        setAutoSelected(true);
        setSelectionConfidence(top.confidence);
      }
    } catch (_) { /* a suggestion is optional */ }
  }

  function audioForm() {
    const fd = new FormData();
    fd.append("file", audioBlob, audioBlob.name || "dictation.webm");
    if (language) fd.append("language", language);
    return fd;
  }

  async function doTranscribe() {
    if (!audioBlob) { setErr(t("dictate.needAudio")); return; }
    setErr(""); setBusy(t("dictate.busyTranscribe"));
    try {
      const d = await apiFetch("/api/transcribe", { method: "POST", body: audioForm(), timeoutMs: 300000 }, token);
      setTranscript(d.text);
      if (d.translation_degraded) toast.info(t("dictate.translationDegraded"));
      await suggestTemplate(d.text);
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  async function doReport() {
    if (!transcript.trim()) { setErr(t("dictate.needTranscript")); return; }
    setErr(""); setBusy(t("dictate.busyReport"));
    try {
      applyReport(await apiFetch("/api/report", {
        method: "POST",
        body: JSON.stringify({ transcript, template_id: templateId || null, extra_context: extraContext || null }),
        timeoutMs: 120000,
      }, token));
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  async function doDictate() {
    if (!audioBlob) { setErr(t("dictate.needAudio")); return; }
    setErr(""); setBusy(t("dictate.busyPipeline"));
    try {
      const fd = audioForm();
      if (templateId) fd.append("template_id", templateId);
      if (extraContext) fd.append("extra_context", extraContext);
      // One upload: the response carries the transcript as well as the report.
      const d = await apiFetch("/api/dictate", { method: "POST", body: fd, timeoutMs: 300000 }, token);
      applyReport(d);
      if (d.transcript) setTranscript(d.transcript);
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  function saveReport() {
    const list = loadSavedReports();
    list.unshift({ ts: Date.now(), template_id: templateId, report });
    if (saveReports(list.slice(0, 50))) toast.success(t("dictate.saved"));
    else toast.error(t("dictate.saveFailed"));
  }

  async function copyReport() {
    try {
      await navigator.clipboard.writeText(report);
      toast.success(t("common.copied"));
    } catch (_) { toast.error(t("common.copyFailed")); }
  }

  function downloadReport() {
    const url = URL.createObjectURL(new Blob([report], { type: "text/plain;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${templateId || "report"}-${new Date().toISOString().slice(0, 10)}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const templateHint = templateId
    ? autoSelected
      ? t("dictate.autoSelected") + (selectionConfidence != null
        ? ` · ${t("dictate.confidence", { n: Math.round(selectionConfidence * 100) })}` : "")
      : t("dictate.manual")
    : t("dictate.autoIfBlank");

  return (
    <>
      <PageHeader
        title={t("dictate.title")}
        subtitle={userRole === "radiologist" ? t("dictate.sub.radiologist") : t("dictate.sub")}
      />

      <div className="grid">
        <Card>
          <CardHead step={1} title={t("dictate.capture")} hint={t("dictate.captureHint")} />

          <div className="recorder">
            <button
              className={`mic-btn ${recording ? "recording" : ""}`}
              onClick={recording ? stopRec : startRec}
              aria-label={recording ? t("dictate.stopRec") : t("dictate.startRec")}
              aria-pressed={recording}
            >
              {recording ? <Stop size={22} /> : <Mic size={22} />}
            </button>
            <div className="recorder-meta" aria-live="polite">
              <div className="t">
                {recording ? t("dictate.recording") : audioBlob ? t("dictate.audioReady") : t("dictate.tapMic")}
              </div>
              <div className="s">
                {recording ? fmtTime(elapsed) : audioBlob ? t("dictate.editThenRun") : t("dictate.orUpload")}
              </div>
            </div>
            <div className="grow" />
            <button type="button" className="upload-btn" onClick={() => fileRef.current?.click()}>
              <Upload size={14} /> {t("common.upload")}
            </button>
            <input ref={fileRef} type="file" accept="audio/*" onChange={pickFile} className="sr-only"
                   tabIndex={-1} aria-label={t("dictate.uploadAudio")} />
          </div>

          {/* Playback of the user's own dictation; there is no caption track. */}
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          {audioUrl && <audio controls src={audioUrl} aria-label={t("dictate.audioReady")} />}

          <Field label={t("dictate.langHint")} hint={t("common.optional")}>
            <input className="input" type="text" value={language}
                   onChange={(e) => setLanguage(e.target.value)}
                   placeholder={t("dictate.langPlaceholder")} />
          </Field>

          <Field label={t("dictate.template")} hint={templateHint}>
            <select
              className="select"
              value={templateId}
              onChange={(e) => { setTemplateId(e.target.value); setAutoSelected(false); }}
            >
              <option value="">{t("dictate.autoOption")}</option>
              {templates.map((tpl) => (
                <option key={tpl.id} value={tpl.id}>{tpl.name}</option>
              ))}
            </select>
          </Field>
          {templatesErr && <div className="err small">{t("dictate.templatesFailed", { error: templatesErr })}</div>}

          <Field label={t("dictate.context")} hint={t("common.optional")}>
            <textarea className="textarea" value={extraContext}
                      onChange={(e) => setExtraContext(e.target.value)}
                      style={{ minHeight: 90 }}
                      placeholder={t("dictate.contextPlaceholder")} />
          </Field>

          <Toolbar>
            <Button variant="ghost" onClick={doTranscribe} disabled={!!busy}>
              <Sparkle size={14} /> {t("dictate.transcribeOnly")}
            </Button>
            <Button onClick={doDictate} loading={!!busy}>
              <Sparkle size={14} /> {t("dictate.run")}
            </Button>
          </Toolbar>

          {busy && (
            <div className="muted" role="status" style={{ marginTop: 10, display: "flex", alignItems: "center", gap: 8 }}>
              <span className="spinner dark" /> {busy}
            </div>
          )}
          {err && <div className="err" role="alert">{err}</div>}

          <div className="kpis">
            <div className="kpi"><span>{t("dictate.asrModel")}</span><b>{health?.asr_model?.split("/").pop() || "—"}</b></div>
            <div className="kpi"><span>{t("dictate.llmModel")}</span><b>{health?.core_model || health?.ollama_model || "—"}</b></div>
          </div>
        </Card>

        <Card>
          <CardHead step={2} title={t("dictate.transcriptReport")} hint={t("dictate.reviewHint")} />

          <Field label={t("dictate.transcript")} hint={t("common.editable")}>
            <textarea className="textarea" dir="auto" value={transcript}
                      onChange={(e) => setTranscript(e.target.value)}
                      placeholder={t("dictate.transcriptPlaceholder")} />
          </Field>
          <Toolbar>
            <Button onClick={doReport} disabled={!!busy} data-testid="dictate-generate">
              <Document size={14} /> {t("dictate.generate")}
            </Button>
          </Toolbar>

          <CriticalAlertsBanner alerts={criticalAlerts} />
          <TemplateMismatchBanner mismatch={templateMismatch} />

          <hr className="hr" />

          <CardHead
            step={3}
            title={t("dictate.structured")}
            right={report && (
              <div className="row" style={{ marginInlineStart: "auto" }}>
                <Button variant="ghost" iconOnly title={t("dictate.saveToReports")} aria-label={t("dictate.saveToReports")}
                        data-testid="save-report" onClick={saveReport}>
                  <Document size={14} />
                </Button>
                <Button variant="ghost" iconOnly title={t("common.copy")} aria-label={t("common.copy")} onClick={copyReport}>
                  <Copy size={14} />
                </Button>
                <Button variant="ghost" iconOnly title={t("dictate.download")} aria-label={t("dictate.download")}
                        onClick={downloadReport}>
                  <Download size={14} />
                </Button>
              </div>
            )}
          />

          <div className={`report ${report ? "" : "empty"}`} aria-live="polite">
            {report || t("dictate.reportPlaceholder")}
          </div>
        </Card>
      </div>
    </>
  );
}
