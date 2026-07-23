import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../lib/auth";
import { apiFetch } from "../../lib/api";
import { useT } from "../../lib/i18n";
import CriticalAlertsBanner from "../CriticalAlertsBanner";

function fmtTs(t) {
  if (!t) return "—";
  try { return new Date(t * 1000).toLocaleString(); } catch (_) { return String(t); }
}

export default function EhrView() {
  const { t, lang } = useT();
  const { token } = useAuth();
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [records, setRecords] = useState([]);
  const [active, setActive] = useState(null);
  const [askQ, setAskQ] = useState("");
  const [askAnswer, setAskAnswer] = useState("");
  const [criticalAlerts, setCriticalAlerts] = useState([]);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const d = await apiFetch("/api/ehr", {}, token);
      setRecords(d.records || []);
    } catch (e) { setErr(e.message); }
  }, [token]);

  useEffect(() => { load(); }, [load]);

  async function build() {
    if (!info.trim()) return;
    setErr(""); setBusy(true);
    try {
      const d = await apiFetch("/api/ehr/build", {
        method: "POST",
        body: JSON.stringify({ patient_info: info, language: lang }),
      }, token);
      setActive(d.record);
      setInfo("");
      setCriticalAlerts([]);
      setAskAnswer("");
      load();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  async function open(id) {
    setErr("");
    try {
      const d = await apiFetch(`/api/ehr/${id}`, {}, token);
      setActive(d.record);
      setCriticalAlerts([]);
      setAskAnswer("");
    } catch (e) { setErr(e.message); }
  }

  async function remove(id) {
    if (!confirm(`Delete ${id}?`)) return;
    try {
      await apiFetch(`/api/ehr/${id}`, { method: "DELETE" }, token);
      if (active?.id === id) setActive(null);
      load();
    } catch (e) { setErr(e.message); }
  }

  async function recordDose(medName) {
    if (!active) return;
    try {
      await apiFetch(`/api/ehr/${active.id}/dose`, {
        method: "POST",
        body: JSON.stringify({ medication: medName }),
      }, token);
      open(active.id);
    } catch (e) { setErr(e.message); }
  }

  async function askEhr() {
    if (!active || !askQ.trim()) return;
    setErr(""); setBusy(true);
    try {
      const d = await apiFetch(`/api/ehr/${active.id}/ask`, {
        method: "POST",
        body: JSON.stringify({ question: askQ }),
      }, token);
      setAskAnswer(d.answer || "");
      setCriticalAlerts(d.critical_alerts || []);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  return (
    <div className="ehr-bg">
      <div className="page-head">
        <h2 className="page-title">{t("ehr.title")}</h2>
        <p className="page-sub">{t("ehr.sub")}</p>
      </div>

      <div className="grid">
        <section className="card">
          <div className="card-head">
            <span className="step">1</span>
            <h2>{t("ehr.input")}</h2>
          </div>
          <textarea className="textarea" dir="auto"
                    value={info} onChange={(e) => setInfo(e.target.value)}
                    style={{ minHeight: 180 }}
                    placeholder={
                      lang === "fa"
                        ? "مرد ۶۲ ساله با سرفه و تنگی نفس؛ سابقه COPD؛ آسپرین ۸۱ میلی‌گرم روزانه..."
                        : "62-year-old male with cough and dyspnea; COPD history; aspirin 81 mg daily..."
                    } />
          <div className="toolbar">
            <button className="btn" onClick={build} disabled={busy || !token}>
              {busy ? <span className="spinner" /> : null} {t("ehr.build")}
            </button>
          </div>
          {err && <div className="err">{err}</div>}
        </section>

        <section className="card">
          <div className="card-head">
            <span className="step">2</span>
            <h2>{t("ehr.savedTitle")}</h2>
            <span className="muted">{records.length}</span>
          </div>
          {records.length === 0 && <div className="muted">{t("ehr.empty")}</div>}
          <ul className="list">
            {records.map((r) => (
              <li key={r.id} className="list-row">
                <div>
                  <div className="bold">{r.name || r.id}</div>
                  <div className="muted small">
                    {r.medications} {t("ehr.medications")} · {fmtTs(r.updated_at)}
                  </div>
                </div>
                <div className="row">
                  <button className="btn ghost" onClick={() => open(r.id)}>
                    {t("ehr.open")}
                  </button>
                  <button className="btn ghost danger" onClick={() => remove(r.id)}>
                    {t("ehr.delete")}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      </div>

      {active && (
        <section className="card" style={{ marginTop: 16 }}>
          <div className="card-head">
            <span className="step">3</span>
            <h2>{active.patient?.name || active.id}</h2>
            <span className="muted">{active.id}</span>
          </div>
          <CriticalAlertsBanner alerts={criticalAlerts} />
          <div className="ehr-grid">
            <div>
              <div className="lbl">{t("ehr.problems")}</div>
              <ul>{(active.problems || []).map((p, i) => (
                <li key={i}>{p.name} <span className="muted">({p.status || "—"})</span></li>
              ))}</ul>

              <div className="lbl">{t("ehr.allergies")}</div>
              <ul>{(active.allergies || []).map((a, i) => (
                <li key={i}>{a.substance}{a.reaction ? ` — ${a.reaction}` : ""}</li>
              ))}</ul>

              <div className="lbl">{t("ehr.vitals")}</div>
              <div className="kpis">
                {Object.entries(active.vitals || {}).map(([k, v]) => (
                  <div key={k} className="kpi"><span>{k}</span><b>{v ?? "—"}</b></div>
                ))}
              </div>
            </div>
            <div>
              <div className="lbl">{t("ehr.medications")}</div>
              <table className="meds">
                <thead><tr>
                  <th>name</th><th>dose</th><th>every</th><th>last</th><th /></tr></thead>
                <tbody>
                  {(active.medications || []).map((m, i) => (
                    <tr key={i}>
                      <td>{m.name}</td>
                      <td>{m.dose || "—"}</td>
                      <td>{m.frequency_hours ? `${m.frequency_hours} h` : (m.frequency || "—")}</td>
                      <td>{fmtTs(m.last_dose_at)}</td>
                      <td>
                        <button className="btn ghost small"
                                onClick={() => recordDose(m.name)}>
                          {t("ehr.recordDose")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <div className="lbl" style={{ marginTop: 16 }}>Ask about this EHR</div>
              <textarea
                className="textarea"
                value={askQ}
                onChange={(e) => setAskQ(e.target.value)}
                style={{ minHeight: 70 }}
                placeholder="e.g. Any life-threatening findings in this record?"
              />
              <div className="toolbar">
                <button className="btn" onClick={askEhr} disabled={busy || !askQ.trim()}>
                  Ask MedicalRAG
                </button>
              </div>
              {askAnswer && (
                <pre className="report" style={{ marginTop: 10, whiteSpace: "pre-wrap" }}>
                  {askAnswer}
                </pre>
              )}
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
