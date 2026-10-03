import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/router";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { clinicalApi, interopApi, transferApi, ageFrom, fmtDate, personLabel, TRANSFER_STEPS } from "../../lib/clinical";
import { pacsApi, fmtDicomDate } from "../../lib/pacs";
import { Badge, Button, Card, CardHead, EmptyState, Label, Modal, Select, TextArea, TextInput } from "../ui";
import CriticalAlertsBanner from "../CriticalAlertsBanner";

const TABS = ["summary", "encounters", "results", "medications", "documents", "imaging", "orders", "timeline", "access"];

function Src({ s, t }) {
  if (!s) return null;
  if (!s.remote) return <span className="src-badge">{t("ch.local")}</span>;
  return <span className={`src-badge ${s.held === "remote" ? "remote" : ""}`} title={s.facility_oid}>
    {s.held === "remote" ? "⇄ " : ""}{s.facility}</span>;
}

function Spark({ points }) {
  if (points.length < 2) return null;
  const w = 120; const h = 28;
  const vals = points.map((p) => p.v);
  const lo = Math.min(...vals); const hi = Math.max(...vals);
  const span = hi - lo || 1;
  const d = points.map((p, i) => `${i ? "L" : "M"}${(i / (points.length - 1)) * w},${h - ((p.v - lo) / span) * (h - 4) - 2}`).join(" ");
  return <svg className="spark" width={w} height={h} aria-hidden><path d={d} fill="none" stroke="var(--accent)" strokeWidth="1.6" /></svg>;
}

const ADD_FIELDS = {
  allergies: [["display", "Substance"], ["reaction", "Reaction"], ["criticality", "Criticality", ["", "low", "high"]]],
  conditions: [["display", "Problem"], ["clinical_status", "Status", ["active", "resolved", "inactive"]], ["onset", "Onset (YYYY-MM-DD)"]],
  medications: [["display", "Medication"], ["dose", "Dose"], ["route", "Route"], ["frequency", "Frequency"], ["status", "Status", ["active", "completed", "stopped"]]],
  observations: [["display", "Test / vital"], ["code", "LOINC code"], ["value_num", "Value"], ["unit", "Unit"], ["ref_low", "Ref low"], ["ref_high", "Ref high"], ["category", "Category", ["laboratory", "vital-signs"]], ["effective", "Date"]],
  documents: [["title", "Title"], ["doc_type", "Type", ["progress-note", "discharge-summary", "consult-note", "referral-note"]], ["content", "Text", "textarea"]],
  encounters: [["class", "Class", ["AMB", "EMER", "IMP"]], ["status", "Status", ["planned", "arrived", "in-progress", "finished"]], ["reason", "Reason"], ["start_at", "Start (YYYY-MM-DD)"], ["department", "Department"]],
  "service-requests": [["display", "Procedure"], ["category", "Category", ["imaging", "laboratory", "procedure"]], ["priority", "Priority", ["routine", "urgent", "stat"]], ["reason", "Reason"], ["modality", "Modality (imaging)", ["", "CT", "MR", "US", "CR", "DX", "MG", "NM"]]],
  consents: [["category", "Category", ["deny-sharing", "permit-sharing", "restricted"]], ["grantee", "Facility OID or *"], ["text", "Note"]],
};

function defaultsFor(kind) {
  const out = {};
  (ADD_FIELDS[kind] || []).forEach(([k, , opts]) => { if (Array.isArray(opts) && opts[0]) out[k] = opts[0]; });
  return out;
}

function AddForm({ kind, onSave, t }) {
  const fields = ADD_FIELDS[kind];
  const [v, setV] = useState(() => defaultsFor(kind));
  if (!fields) return null;
  async function submit(e) {
    e.preventDefault();
    const body = { ...v };
    ["value_num", "ref_low", "ref_high"].forEach((k) => { if (body[k] !== undefined && body[k] !== "") body[k] = parseFloat(body[k]); });
    if (kind === "service-requests") { body.status = "active"; body.intent = "order"; body.data = { modality: body.modality }; delete body.modality; }
    if (kind === "consents") { body.status = "active"; body.scope = "patient-privacy"; }
    Object.keys(body).forEach((k) => { if (body[k] === "") delete body[k]; });
    await onSave(kind, body);
    setV(defaultsFor(kind));
  }
  return (
    <form className="form-grid add-form" onSubmit={submit} data-testid={`add-${kind}`}>
      {fields.map(([k, label, opts]) => (
        <div key={k} className={opts === "textarea" ? "span-2" : ""}>
          <Label>{label}</Label>
          {Array.isArray(opts) ? (
            <Select value={v[k] ?? opts[0]} onChange={(e) => setV({ ...v, [k]: e.target.value })}>
              {opts.map((o) => <option key={o} value={o}>{o || "—"}</option>)}
            </Select>
          ) : opts === "textarea" ? (
            <TextArea value={v[k] || ""} onChange={(e) => setV({ ...v, [k]: e.target.value })} dir="auto" />
          ) : (
            <TextInput value={v[k] || ""} onChange={(e) => setV({ ...v, [k]: e.target.value })} dir="auto" name={k} />
          )}
        </div>
      ))}
      <div className="span-2 toolbar"><Button type="submit">{t("ch.save")}</Button></div>
    </form>
  );
}

export default function ChartView() {
  const { t } = useT();
  const { token, user } = useAuth();
  const router = useRouter();
  const id = router.query.id;
  const [chart, setChart] = useState(null);
  const [err, setErr] = useState("");
  const [restricted, setRestricted] = useState(false);
  const [tab, setTab] = useState("summary");
  const [remote, setRemote] = useState(false);
  const [emergency, setEmergency] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [docOpen, setDocOpen] = useState(null);
  const [adding, setAdding] = useState(null);
  const [askQ, setAskQ] = useState("");
  const [answer, setAnswer] = useState(null);
  const [transferOpen, setTransferOpen] = useState(false);
  const [peers, setPeers] = useState([]);
  const [tf, setTf] = useState({ to_facility: "", urgency: "urgent", reason: "", clinical_summary: "", transport_mode: "", include_imaging: true });

  const load = useCallback(async () => {
    if (!id || !token) return;
    setBusy(true); setErr("");
    try {
      setChart(await clinicalApi.chart(id, { include_remote: remote, purpose: emergency ? "ETREAT" : "TREAT" }, token));
      setRestricted(false);
    } catch (e) {
      if (String(e.message).includes("break-the-glass")) setRestricted(true);
      else setErr(e.message);
    } finally { setBusy(false); }
  }, [id, token, remote, emergency]);
  useEffect(() => { load(); }, [load]);

  async function breakGlass(e) {
    e.preventDefault();
    try { await clinicalApi.breakGlass(id, reason, token); setReason(""); await load(); } catch (e2) { setErr(e2.message); }
  }
  async function add(kind, body) {
    try { await clinicalApi.add(id, kind, body, token); setAdding(null); await load(); } catch (e) { setErr(e.message); }
  }
  async function viewDoc(d) {
    if (d.source?.held === "remote") { setDocOpen({ ...d, content: "(held at the source hospital — import via transfer to view locally)" }); return; }
    try { setDocOpen(await clinicalApi.document(d.id, token)); } catch (e) { setErr(e.message); }
  }
  async function exportCda() {
    const blob = await interopApi.exportCda(id, token);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `ccd-${id}.xml`; a.click();
  }
  async function ask(e) {
    e.preventDefault();
    setBusy(true);
    try { setAnswer(await clinicalApi.ask(id, askQ, remote, token)); } catch (e2) { setErr(e2.message); } finally { setBusy(false); }
  }
  async function retrieve(study) {
    const loc = study.locations?.[0];
    if (!loc) return;
    setBusy(true);
    try { await pacsApi.retrieve({ study_uid: study.StudyInstanceUID, node_id: loc.id, wait: true }, token); await load(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  }
  async function openTransfer() {
    try { setPeers((await transferApi.list({}, token)).peers); } catch (_) { /* no permission */ }
    setTransferOpen(true);
  }
  async function sendTransfer(e) {
    e.preventDefault();
    try { await transferApi.create({ person_id: id, ...tf }, token); setTransferOpen(false); await load(); setTab("access"); }
    catch (e2) { setErr(e2.message); }
  }

  const sections = chart?.sections || {};
  const labsByCode = useMemo(() => {
    const m = {};
    (sections.observation || []).forEach((o) => {
      const k = o.code || o.display;
      if (!k) return;
      (m[k] = m[k] || []).push(o);
    });
    Object.values(m).forEach((arr) => arr.sort((a, b) => String(a.effective || "").localeCompare(String(b.effective || ""))));
    return m;
  }, [sections.observation]);

  if (!id) return <div className="muted">No patient selected.</div>;
  if (restricted) {
    return (
      <Card data-testid="restricted">
        <CardHead title={t("ch.restrictedTitle")} />
        <p>{t("ch.restrictedBody")}</p>
        <form onSubmit={breakGlass} className="form-grid">
          <div className="span-2"><Label>{t("ch.reason")}</Label><TextInput value={reason} onChange={(e) => setReason(e.target.value)} minLength={10} required /></div>
          <div className="span-2 toolbar"><Button variant="danger" type="submit">{t("ch.breakGlass")}</Button></div>
        </form>
        {err && <div className="err">{err}</div>}
      </Card>
    );
  }
  if (!chart) return <div>{err ? <div className="err">{err}</div> : <span className="spinner" />}</div>;
  const p = chart.person;
  const d = p.demographics;
  const sm = chart.summary;
  const canWrite = user && ["doctor", "admin"].includes(user.role);

  return (
    <div data-testid="chart">
      <Card>
        <div className="chart-head">
          <div className="avatar">{(d.given || "?")[0]}{(d.family || "?")[0]}</div>
          <div>
            <h2 className="page-title" dir="auto" data-testid="chart-name">{personLabel(p)}</h2>
            <div className="muted small">
              {d.sex || "?"} · {d.birth_date || "?"}{ageFrom(d.birth_date) ? ` (${ageFrom(d.birth_date)}y)` : ""}
              {d.phone ? ` · ${d.phone}` : ""}
            </div>
            <div className="pills" style={{ marginTop: 6 }}>
              {p.identifiers.slice(0, 6).map((i) => <span key={i.system + i.value} className="pill" title={i.system}>{i.type} {i.value}</span>)}
              {chart.access?.restricted && <Badge tone="err">{t("cl.restricted")} · {chart.access.basis}</Badge>}
            </div>
          </div>
          <div className="topbar-spacer" />
          <div className="checks">
            <label><input type="checkbox" checked={remote} onChange={(e) => setRemote(e.target.checked)} data-testid="toggle-remote" /> {t("ch.includeRemote")}</label>
            {remote && <label title={t("ch.emergencyHint")}><input type="checkbox" checked={emergency} onChange={(e) => setEmergency(e.target.checked)} data-testid="toggle-emergency" /> {t("ch.emergency")}</label>}
          </div>
          <div className="toolbar" style={{ marginTop: 0 }}>
            <Button variant="ghost" onClick={exportCda}>{t("ch.exportCda")}</Button>
            {canWrite && <Button variant="ghost" onClick={openTransfer} data-testid="transfer-open">{t("ch.transfer")}</Button>}
          </div>
        </div>
        {busy && <div className="progress"><div style={{ width: "60%" }} /></div>}
        {chart.errors?.length > 0 && (
          <div className="warn-banner" data-testid="chart-errors">{t("ch.errors")}: {chart.errors.map((e) => `${e.facility || e.source}: ${e.error}`).join(" · ")}</div>
        )}
        {chart.facilities?.length > 1 && <div className="muted small">Sources: {chart.facilities.map((o) => chart.facility_names?.[o] || o).join(", ")}</div>}
      </Card>

      <div className="chart-tabs" role="tablist">
        {TABS.map((x) => (
          <button key={x} type="button" role="tab" aria-selected={tab === x}
                  className={`tab ${tab === x ? "active" : ""}`} onClick={() => setTab(x)} data-tab={x}>
            {t(`ch.tab.${x}`)}
          </button>
        ))}
      </div>
      {err && <div className="err">{err}</div>}

      {tab === "summary" && (
        <div className="chart-grid" data-testid="summary">
          <Card><CardHead title={t("ch.allergies")} right={canWrite && <Button variant="ghost" onClick={() => setAdding("allergies")}>+ {t("ch.add")}</Button>} />
            {sm.allergies.length === 0 ? <EmptyState>NKA</EmptyState> : <ul className="mini-list">
              {sm.allergies.map((a) => <li key={a.id}><b className="danger-text" dir="auto">{a.display || a.text}</b> — {a.reaction || "?"} {a.criticality === "high" && <Badge tone="err">high</Badge>} <Src s={a.source} t={t} /></li>)}</ul>}
            {adding === "allergies" && <AddForm kind="allergies" onSave={add} t={t} />}
          </Card>
          <Card><CardHead title={t("ch.problems")} right={canWrite && <Button variant="ghost" onClick={() => setAdding("conditions")}>+ {t("ch.add")}</Button>} />
            <ul className="mini-list">{sm.active_problems.map((c) => <li key={c.id} dir="auto">{c.display || c.text} <span className="muted small">{c.onset || ""}</span> <Src s={c.source} t={t} /></li>)}</ul>
            {adding === "conditions" && <AddForm kind="conditions" onSave={add} t={t} />}
          </Card>
          <Card><CardHead title={t("ch.meds")} right={canWrite && <Button variant="ghost" onClick={() => setAdding("medications")}>+ {t("ch.add")}</Button>} />
            <ul className="mini-list">{sm.active_medications.map((m) => <li key={m.id} dir="auto"><b>{m.display || m.text}</b> {[m.dose, m.route, m.frequency].filter(Boolean).join(" ")} <Src s={m.source} t={t} /></li>)}</ul>
            {adding === "medications" && <AddForm kind="medications" onSave={add} t={t} />}
          </Card>
          <Card><CardHead title={t("ch.vitals")} />
            <div className="vital-chips">{sm.latest_vitals.map((o) => <span key={o.id}>{o.display}: <b>{o.value_num ?? o.value_text}</b> {o.unit || ""}</span>)}</div>
            <div className="lbl">{t("ch.abnormal")}</div>
            <div className="vital-chips">{sm.abnormal_labs.map((o) => <span key={o.id} className="abn">{o.display}: {o.value_num} {o.unit || ""} ({o.interpretation})</span>)}</div>
          </Card>
          <Card><CardHead title={t("ch.lastEnc")} />
            {sm.last_encounter ? <div dir="auto">{sm.last_encounter.class} · {sm.last_encounter.reason || sm.last_encounter.type_text} · {fmtDate(sm.last_encounter.start_at)} <Badge>{sm.last_encounter.status}</Badge> <Src s={sm.last_encounter.source} t={t} /></div> : <EmptyState>—</EmptyState>}
            <div className="lbl">{t("ch.tab.imaging")}</div><div>{sm.imaging_count}</div>
          </Card>
          <Card><CardHead title={t("ch.ask")} />
            <form onSubmit={ask} className="ai-chat">
              <TextArea value={askQ} onChange={(e) => setAskQ(e.target.value)} dir="auto" style={{ minHeight: 60 }} />
              <Button type="submit" loading={busy} disabled={!askQ.trim()}>{t("pacs.send")}</Button>
            </form>
            {answer && <><CriticalAlertsBanner alerts={answer.critical_alerts || []} /><div className="chat-msg assistant" data-testid="chart-answer">{answer.answer}</div></>}
          </Card>
        </div>
      )}

      {tab === "encounters" && (
        <Card><CardHead title={t("ch.tab.encounters")} right={canWrite && <Button variant="ghost" onClick={() => setAdding("encounters")}>+ {t("ch.add")}</Button>} />
          {adding === "encounters" && <AddForm kind="encounters" onSave={add} t={t} />}
          <table className="data-table"><tbody>{(sections.encounter || []).map((e) => (
            <tr key={e.id}><td className="nowrap">{fmtDate(e.start_at)}</td><td><Badge tone="info">{e.class}</Badge></td><td dir="auto">{e.reason || e.type_text || "—"}<div className="muted small">{e.department || e.location || ""} {e.attending || ""}</div></td><td><Badge>{e.status}</Badge></td><td><Src s={e.source} t={t} /></td></tr>))}</tbody></table>
        </Card>
      )}

      {tab === "results" && (
        <Card><CardHead title={t("ch.tab.results")} right={canWrite && <Button variant="ghost" onClick={() => setAdding("observations")}>+ {t("ch.add")}</Button>} />
          {adding === "observations" && <AddForm kind="observations" onSave={add} t={t} />}
          <table className="data-table" data-testid="results-table"><thead><tr><th>Test</th><th>Latest</th><th>Range</th><th>Trend</th><th>Date</th><th /></tr></thead><tbody>
            {Object.entries(labsByCode).map(([k, arr]) => {
              const last = arr[arr.length - 1];
              const pts = arr.filter((o) => typeof o.value_num === "number").map((o) => ({ v: o.value_num }));
              return (<tr key={k}><td dir="auto"><b>{last.display || last.text}</b><div className="muted small">{last.category}</div></td>
                <td className={last.interpretation && last.interpretation !== "N" ? "danger-text" : ""}>{last.value_num ?? last.value_text} {last.unit || ""} {last.interpretation && last.interpretation !== "N" && <Badge tone="err">{last.interpretation}</Badge>}</td>
                <td className="small">{last.ref_low ?? ""}–{last.ref_high ?? ""}</td><td><Spark points={pts} /></td><td className="nowrap small">{fmtDate(last.effective)}</td><td><Src s={last.source} t={t} /></td></tr>);
            })}</tbody></table>
        </Card>
      )}

      {tab === "medications" && (
        <Card><CardHead title={t("ch.tab.medications")} right={canWrite && <Button variant="ghost" onClick={() => setAdding("medications")}>+ {t("ch.add")}</Button>} />
          {adding === "medications" && <AddForm kind="medications" onSave={add} t={t} />}
          <table className="data-table"><tbody>{(sections.medication || []).map((m) => (
            <tr key={m.id}><td dir="auto"><b>{m.display || m.text}</b><div className="muted small">{m.note || ""}</div></td><td>{[m.dose, m.route, m.frequency].filter(Boolean).join(" ")}</td><td><Badge tone={m.status === "active" ? "ok" : "muted"}>{m.status || "active"}</Badge></td><td>{m.kind}</td><td><Src s={m.source} t={t} /></td></tr>))}</tbody></table>
        </Card>
      )}

      {tab === "documents" && (
        <Card><CardHead title={t("ch.tab.documents")} right={canWrite && <Button variant="ghost" onClick={() => setAdding("documents")}>+ {t("ch.add")}</Button>} />
          {adding === "documents" && <AddForm kind="documents" onSave={add} t={t} />}
          <ul className="mini-list" data-testid="documents">{(sections.document || []).map((dd) => (
            <li key={dd.id}><b dir="auto">{dd.title || dd.doc_type}</b> <span className="muted small">{dd.doc_type} · {fmtDate(dd.effective || dd.created_at)} · {dd.author || ""}</span> <Src s={dd.source} t={t} />
              <Button variant="ghost" onClick={() => viewDoc(dd)}>{t("ch.view")}</Button></li>))}</ul>
          {(sections.diagnostic_report || []).length > 0 && (<><div className="lbl">Reports</div><ul className="mini-list">
            {sections.diagnostic_report.map((r) => <li key={r.id}><Badge tone="info">{r.category}</Badge> <b>{r.display}</b> <Badge>{r.status}</Badge> <span dir="auto">{r.conclusion}</span> <Src s={r.source} t={t} /></li>)}</ul></>)}
        </Card>
      )}

      {tab === "imaging" && (
        <Card><CardHead title={t("ch.tab.imaging")} />
          <table className="data-table" data-testid="imaging-table"><tbody>{(chart.imaging || []).map((s) => {
            const isLocal = s.source?.held !== "remote";
            return (<tr key={s.StudyInstanceUID}><td className="nowrap">{fmtDicomDate(s.StudyDate)}</td><td>{(s.ModalitiesInStudy || []).join(", ")}</td><td dir="auto">{s.StudyDescription || "—"}</td><td>{s.NumberOfStudyRelatedInstances ?? "?"} img</td><td><Src s={s.source} t={t} /></td>
              <td>{isLocal ? <Button variant="ghost" onClick={() => router.push(`/pacs/viewer?study=${s.StudyInstanceUID}`)}>{t("pacs.open")}</Button>
                : <Button variant="ghost" onClick={() => retrieve(s)} data-testid="retrieve-remote">{t("ch.retrieve")}</Button>}</td></tr>);
          })}</tbody></table>
        </Card>
      )}

      {tab === "orders" && (
        <Card><CardHead title={t("ch.tab.orders")} right={canWrite && <Button variant="ghost" onClick={() => setAdding("service-requests")}>+ {t("ch.order")}</Button>} />
          {adding === "service-requests" && <AddForm kind="service-requests" onSave={add} t={t} />}
          <ul className="mini-list" data-testid="orders">{(sections.service_request || []).map((o) => (
            <li key={o.id}><Badge tone="info">{o.category}</Badge> <b dir="auto">{o.display || o.text}</b> <Badge tone={o.priority === "stat" ? "err" : "muted"}>{o.priority || "routine"}</Badge> <Badge>{o.status}</Badge> {o.accession && <span className="mono small">ACC {o.accession}</span>} <Src s={o.source} t={t} /></li>))}</ul>
        </Card>
      )}

      {tab === "timeline" && (
        <Card><CardHead title={t("ch.tab.timeline")} />
          <ul className="timeline">{(chart.timeline || buildTimeline(chart)).map((e, i) => (
            <li key={i} className={e.source?.remote ? "remote" : ""}><span className="muted small">{e.date || "—"}</span> <Badge>{e.kind}</Badge> <span dir="auto">{e.label}</span> <Src s={e.source} t={t} /></li>))}</ul>
        </Card>
      )}

      {tab === "access" && (
        <div className="chart-grid">
          <Card><CardHead title={t("ch.tab.access")} right={canWrite && <Button variant="ghost" onClick={() => setAdding("consents")}>+ {t("ch.consent")}</Button>} />
            <div className="small">Access basis: <b>{chart.access?.basis}</b></div>
            {adding === "consents" && <AddForm kind="consents" onSave={add} t={t} />}
            <ul className="mini-list">{(sections.consent || []).map((c) => <li key={c.id}><Badge tone={c.category === "permit-sharing" ? "ok" : "err"}>{c.category}</Badge> {c.grantee || "*"} <Badge>{c.status}</Badge> <span className="muted small">{c.text || ""}</span></li>)}</ul>
          </Card>
          <Card><CardHead title={t("nav.transfers")} />
            <ul className="mini-list" data-testid="chart-transfers">{(chart.transfers || []).map((tr) => (
              <li key={tr.id}><Badge tone="info">{tr.direction}</Badge> {tr.from_facility_name || tr.from_facility} → {tr.to_facility_name || tr.to_facility}
                <div className="status-steps">{TRANSFER_STEPS.map((st) => <span key={st} className={`step-item ${tr.status === st ? "current" : TRANSFER_STEPS.indexOf(st) < TRANSFER_STEPS.indexOf(tr.status) ? "done" : ""}`}>{st}</span>)}</div></li>))}</ul>
          </Card>
        </div>
      )}

      <Modal open={!!docOpen} title={docOpen?.title || "Document"} onClose={() => setDocOpen(null)}>
        <pre className="raw" dir="auto" data-testid="doc-content">{docOpen?.content}</pre>
      </Modal>
      <Modal open={transferOpen} title={t("ch.transfer")} onClose={() => setTransferOpen(false)}>
        <form className="form-grid" onSubmit={sendTransfer}>
          <div className="span-2"><Label>{t("tr.to")}</Label>
            <Select value={tf.to_facility} onChange={(e) => setTf({ ...tf, to_facility: e.target.value })} required>
              <option value="">—</option>{peers.map((x) => <option key={x.oid} value={x.oid}>{x.name}</option>)}</Select></div>
          <div><Label>{t("tr.urgency")}</Label><Select value={tf.urgency} onChange={(e) => setTf({ ...tf, urgency: e.target.value })}><option>routine</option><option>urgent</option><option>emergent</option></Select></div>
          <div><Label>{t("tr.transport")}</Label><TextInput value={tf.transport_mode} onChange={(e) => setTf({ ...tf, transport_mode: e.target.value })} /></div>
          <div className="span-2"><Label>{t("tr.reason")}</Label><TextInput value={tf.reason} onChange={(e) => setTf({ ...tf, reason: e.target.value })} required dir="auto" /></div>
          <div className="span-2"><Label>{t("tr.summary")}</Label><TextArea value={tf.clinical_summary} onChange={(e) => setTf({ ...tf, clinical_summary: e.target.value })} dir="auto" /></div>
          <div className="span-2 checks"><label><input type="checkbox" checked={tf.include_imaging} onChange={(e) => setTf({ ...tf, include_imaging: e.target.checked })} /> {t("tr.imaging")}</label></div>
          <div className="span-2 toolbar"><Button type="submit">{t("tr.send")}</Button></div>
        </form>
      </Modal>
    </div>
  );
}

function buildTimeline(chart) {
  const ev = [];
  Object.entries(chart.sections || {}).forEach(([sec, items]) => {
    if (sec === "consent") return;
    items.forEach((it) => ev.push({ date: String(it.effective || it.start_at || it.onset || it.performed || it.occurrence ||
        (it.created_at ? new Date(it.created_at * 1000).toISOString() : "")).slice(0, 10),
      kind: sec, label: it.display || it.text || it.title || it.reason || sec, source: it.source }));
  });
  (chart.imaging || []).forEach((s) => ev.push({ date: fmtDicomDate(s.StudyDate), kind: "imaging",
    label: s.StudyDescription || (s.ModalitiesInStudy || []).join(","), source: s.source }));
  return ev.sort((a, b) => (b.date || "").localeCompare(a.date || ""));
}
