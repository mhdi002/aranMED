import { useCallback, useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { useRouter } from "next/router";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { pacsApi, fmtDicomDate, STATUS_TONE } from "../../lib/pacs";
import { Badge, Button, TextArea } from "../ui";
import CriticalAlertsBanner from "../CriticalAlertsBanner";
import ReportEditor from "../pacs/ReportEditor";

// Cornerstone touches window/WebGL at import time: client-side only.
const Viewer = dynamic(() => import("../pacs/Viewer"), {
  ssr: false,
  loading: () => <div className="viewer-boot"><span className="spinner" /></div>,
});

const TABS = ["info", "report", "ai", "measure"];

export default function PacsViewerView() {
  const { t } = useT();
  const { token, user } = useAuth();
  const router = useRouter();
  const studyUid = router.query.study;
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState("info");
  const [measurements, setMeasurements] = useState([]);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiResult, setAiResult] = useState(null);
  const [question, setQuestion] = useState("");
  const [chat, setChat] = useState([]);
  const [alerts, setAlerts] = useState([]);

  const load = useCallback(async () => {
    if (!studyUid || !token) return;
    try { setD(await pacsApi.study(studyUid, token)); } catch (e) { setErr(e.message); }
  }, [studyUid, token]);
  useEffect(() => { load(); }, [load]);

  async function analyze() {
    setAiBusy(true); setErr("");
    try {
      const r = await pacsApi.analyze(studyUid, {}, token);
      setAiResult(r.data);
      setAlerts(r.data?.critical_alerts || []);
      await load();
    } catch (e) { setErr(e.message); } finally { setAiBusy(false); }
  }

  async function ask(e) {
    e?.preventDefault();
    if (!question.trim()) return;
    const q = question;
    setQuestion("");
    setChat((c) => [...c, { role: "user", text: q }]);
    setAiBusy(true);
    try {
      const r = await pacsApi.ask(studyUid, q, token);
      setChat((c) => [...c, { role: "assistant", text: r.answer, tools: (r.tool_calls || []).map((x) => x.name) }]);
      if (r.critical_alerts?.length) setAlerts(r.critical_alerts);
      await load();
    } catch (e2) {
      setChat((c) => [...c, { role: "error", text: e2.message }]);
    } finally { setAiBusy(false); }
  }

  if (!studyUid) return <div className="muted">No study selected.</div>;
  const s = d?.study;

  return (
    <div className="viewer-page">
      <div className="viewer-head">
        <Button variant="ghost" onClick={() => router.push("/pacs")}>← {t("nav.pacs")}</Button>
        {s && (
          <div className="viewer-title" data-testid="viewer-title">
            <b dir="auto">{(s.PatientName || "—").replace(/\^/g, " ")}</b>
            <span className="muted"> · {s.PatientID} · {s.PatientSex || "?"} · {fmtDicomDate(s.PatientBirthDate)}</span>
            <span> · <span dir="auto">{s.StudyDescription || "—"}</span> · {fmtDicomDate(s.StudyDate)}</span>
            {s._ext?.status && <Badge tone={STATUS_TONE[s._ext.status] || "muted"}>{s._ext.status}</Badge>}
          </div>
        )}
      </div>
      {err && <div className="err" role="alert">{err}</div>}

      <div className="viewer-body">
        <div className="viewer-main">
          {d && token && (
            <Viewer studyUid={studyUid} series={d.series} token={token} t={t}
                    initialSeries={router.query.series} onMeasurements={setMeasurements} />
          )}
        </div>

        <aside className="viewer-side">
          <div className="tabs" role="tablist">
            {TABS.map((x) => (
              <button key={x} role="tab" aria-selected={tab === x} type="button"
                      className={`tab ${tab === x ? "active" : ""}`} onClick={() => setTab(x)}
                      data-tab={x}>
                {t({ info: "pacs.info", report: "pacs.report", ai: "pacs.ai", measure: "pacs.measurements" }[x])}
                {x === "measure" && measurements.length > 0 && <span className="tab-count">{measurements.length}</span>}
              </button>
            ))}
          </div>

          {tab === "info" && s && (
            <div className="side-body">
              <div className="kv">
                <span>{t("pacs.accession")}</span><b>{s.AccessionNumber || "—"}</b>
                <span>{t("pacs.modality")}</span><b>{(s.ModalitiesInStudy || []).join(", ")}</b>
                <span>Referrer</span><b>{(s.ReferringPhysicianName || "—").replace(/\^/g, " ")}</b>
                <span>{t("pacs.instances")}</span><b>{s.NumberOfStudyRelatedInstances}</b>
                <span>Origin</span><b>{s._ext?.origin_facility || "—"}</b>
                <span>Study UID</span><b className="mono small">{s.StudyInstanceUID}</b>
              </div>
              {d.worklist && (
                <>
                  <div className="lbl">{t("pacs.order")}</div>
                  <div className="small">{d.worklist.procedure_description} · {d.worklist.reason}</div>
                </>
              )}
              {d.priors?.length > 0 && (
                <>
                  <div className="lbl">{t("pacs.priors")}</div>
                  <ul className="mini-list">
                    {d.priors.map((p) => (
                      <li key={p.StudyInstanceUID}>
                        <a href={`/pacs/viewer?study=${p.StudyInstanceUID}`}>
                          {fmtDicomDate(p.StudyDate)} · {(p.ModalitiesInStudy || []).join("/")} · {p.StudyDescription || "—"}
                        </a>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          )}

          {tab === "report" && d && (
            <div className="side-body">
              <ReportEditor studyUid={studyUid} reports={d.reports} token={token} user={user} onSaved={load} />
            </div>
          )}

          {tab === "ai" && (
            <div className="side-body">
              <p className="muted small">{t("pacs.aiDisclaimer")}</p>
              <CriticalAlertsBanner alerts={alerts} />
              <Button onClick={analyze} loading={aiBusy} data-testid="ai-analyze">
                {aiBusy ? t("pacs.analyzing") : t("pacs.analyze")}
              </Button>
              {aiResult && (
                <div className="ai-result" data-testid="ai-result">
                  <div className="lbl">Draft {aiResult.template_id ? `(${aiResult.template_id})` : ""}</div>
                  <pre dir="auto">{aiResult.report}</pre>
                  <Button variant="ghost" onClick={() => setTab("report")}>{t("pacs.report")} →</Button>
                </div>
              )}
              <form className="ai-chat" onSubmit={ask}>
                <div className="lbl">{t("pacs.ask")}</div>
                <div className="chat-log">
                  {chat.map((m, i) => (
                    <div key={i} className={`chat-msg ${m.role}`}>
                      <div dir="auto">{m.text}</div>
                      {m.tools?.length > 0 && <div className="muted small">tools: {m.tools.join(", ")}</div>}
                    </div>
                  ))}
                </div>
                <TextArea value={question} onChange={(e) => setQuestion(e.target.value)} dir="auto"
                          placeholder={t("pacs.askPh")} style={{ minHeight: 70 }} />
                <Button type="submit" loading={aiBusy} disabled={!question.trim()}>{t("pacs.send")}</Button>
              </form>
            </div>
          )}

          {tab === "measure" && (
            <div className="side-body" data-testid="measurements">
              {measurements.length === 0 ? <div className="muted small">{t("pacs.noMeasurements")}</div> : (
                <ul className="mini-list">
                  {measurements.map((m) => (
                    <li key={m.uid}>
                      <b>{m.tool}</b>{" "}
                      {m.length !== undefined && <span>{Number(m.length).toFixed(1)} {m.unit || "mm"}</span>}
                      {m.angle !== undefined && <span>{Number(m.angle).toFixed(1)}°</span>}
                      {m.area !== undefined && <span>{Number(m.area).toFixed(1)} {m.areaUnit || "mm²"}</span>}
                      {m.mean !== undefined && <span> · mean {Number(m.mean).toFixed(1)} {m.modalityUnit || ""}</span>}
                      {m.value !== undefined && m.mean === undefined && <span>{Number(m.value).toFixed(1)} {m.modalityUnit || ""}</span>}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
