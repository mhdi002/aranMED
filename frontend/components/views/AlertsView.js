import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../lib/auth";
import { apiFetch } from "../../lib/api";
import { useT } from "../../lib/i18n";

const STATUS_CLASS = {
  due: "due", scheduled: "ok",
  never_administered: "warn", schedule_unknown: "muted",
};

function fmtTs(t) {
  if (!t) return "—";
  try { return new Date(t * 1000).toLocaleString(); } catch (_) { return String(t); }
}

export default function AlertsView() {
  const { t, lang } = useT();
  const { token } = useAuth();
  const [records, setRecords] = useState([]);
  const [pid, setPid] = useState("");
  const [check, setCheck] = useState(null);
  const [channel, setChannel] = useState("email");
  const [to, setTo] = useState("");
  const [history, setHistory] = useState([]);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [sent, setSent] = useState(null);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const [r1, r2] = await Promise.all([
        apiFetch("/api/ehr", {}, token),
        apiFetch("/api/alerts", {}, token),
      ]);
      setRecords(r1.records || []);
      setHistory(r2.alerts || []);
      if (!pid && r1.records?.[0]) setPid(r1.records[0].id);
    } catch (e) { setErr(e.message); }
  }, [token, pid]);

  useEffect(() => { load(); }, [load]);

  async function doCheck() {
    if (!pid) return;
    setBusy("check"); setErr(""); setCheck(null);
    try {
      const d = await apiFetch("/api/alerts/check", {
        method: "POST",
        body: JSON.stringify({ patient_id: pid, language: lang }),
      }, token);
      setCheck(d);
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  async function doSend() {
    if (!pid || !to) return;
    setBusy("send"); setErr(""); setSent(null);
    try {
      const d = await apiFetch("/api/alerts/send", {
        method: "POST",
        body: JSON.stringify({
          patient_id: pid, channel, to, language: lang, only_if_due: false,
        }),
      }, token);
      setSent(d);
      load();
    } catch (e) { setErr(e.message); } finally { setBusy(""); }
  }

  return (
    <div className="alerts-bg">
      <div className="page-head">
        <h2 className="page-title">{t("alerts.title")}</h2>
        <p className="page-sub">{t("alerts.sub")}</p>
      </div>

      <div className="grid">
        <section className="card">
          <div className="card-head">
            <span className="step">1</span>
            <h2>{t("alerts.check")}</h2>
          </div>
          <label className="lbl">{t("alerts.patient")}</label>
          <select className="select" value={pid}
                  onChange={(e) => setPid(e.target.value)}>
            <option value="">—</option>
            {records.map((r) => (
              <option key={r.id} value={r.id}>{r.name || r.id}</option>
            ))}
          </select>
          <div className="toolbar">
            <button className="btn" disabled={!pid || busy === "check"}
                    onClick={doCheck}>
              {busy === "check" ? <span className="spinner" /> : null}
              {t("alerts.check")}
            </button>
          </div>
          {check && (
            <>
              <div className="kpis">
                <div className="kpi"><span>due</span><b>{check.due_count}</b></div>
                <div className="kpi"><span>total</span>
                  <b>{check.medications.length}</b></div>
              </div>
              <ul className="list">
                {check.medications.map((m, i) => (
                  <li key={i} className="list-row">
                    <div>
                      <div className="bold">{m.name}</div>
                      <div className="muted small">
                        {m.dose || "—"} · every {m.frequency_hours || "?"} h ·
                        last {fmtTs(m.last_dose_at)}
                      </div>
                    </div>
                    <span className={`pill ${STATUS_CLASS[m.status] || ""}`}>
                      {t("alerts." + (
                        m.status === "due" ? "due" :
                        m.status === "scheduled" ? "scheduled" :
                        m.status === "never_administered" ? "never" : "unknown"
                      ))}
                      {m.overdue_hours ? ` +${m.overdue_hours}h` : ""}
                    </span>
                  </li>
                ))}
              </ul>
              <pre className="report">{check.summary}</pre>
            </>
          )}
        </section>

        <section className="card">
          <div className="card-head">
            <span className="step">2</span>
            <h2>{t("alerts.send")}</h2>
          </div>
          <label className="lbl">{t("alerts.channel")}</label>
          <select className="select" value={channel}
                  onChange={(e) => setChannel(e.target.value)}>
            <option value="email">Email (SMTP)</option>
            <option value="sms">SMS (Twilio)</option>
          </select>
          <label className="lbl">{t("alerts.to")}</label>
          <input className="input" value={to} onChange={(e) => setTo(e.target.value)}
                 placeholder={channel === "email" ? "doctor@hospital.org" : "+15551234567"} />
          <div className="toolbar">
            <button className="btn primary" disabled={!pid || !to || busy === "send"}
                    onClick={doSend}>
              {busy === "send" ? <span className="spinner" /> : null}
              {t("alerts.send")}
            </button>
          </div>
          {sent && (
            <div className="muted small" style={{ marginTop: 10 }}>
              {sent.delivery?.dry_run
                ? "Dry-run (no SMTP/Twilio creds configured). Body preview ↓"
                : "Sent ✓"}
              <pre className="report">{sent.body}</pre>
            </div>
          )}
          {err && <div className="err">{err}</div>}
        </section>
      </div>

      <section className="card" style={{ marginTop: 16 }}>
        <div className="card-head">
          <span className="step">3</span>
          <h2>{t("alerts.history")}</h2>
          <span className="muted">{history.length}</span>
        </div>
        <ul className="list">
          {history.map((a) => (
            <li key={a.id} className="list-row">
              <div>
                <div className="bold">{a.channel} → {a.recipient}</div>
                <div className="muted small">
                  {fmtTs(a.sent_at)} · {a.dry_run ? "dry-run" : "delivered"}
                </div>
              </div>
              <span className="pill muted">{a.patient_id}</span>
            </li>
          ))}
          {history.length === 0 && <div className="muted">—</div>}
        </ul>
      </section>
    </div>
  );
}