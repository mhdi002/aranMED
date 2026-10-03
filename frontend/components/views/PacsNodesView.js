import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { pacsApi, STATUS_TONE, fmtDicomDate } from "../../lib/pacs";
import { Badge, Button, Card, CardHead, Label, Modal, Select, TextInput } from "../ui";

const BLANK = { name: "", kind: "dimse", ae_title: "", host: "", port: 104, base_url: "", auth_env: "",
                facility_oid: "", allow_store: true, allow_query: true, allow_retrieve: true,
                is_move_destination: true, federate: false, active: true };

export default function PacsNodesView() {
  const { t } = useT();
  const { token } = useAuth();
  const [data, setData] = useState({ nodes: [], local: null, federation_targets: [] });
  const [jobs, setJobs] = useState([]);
  const [echo, setEcho] = useState({});
  const [edit, setEdit] = useState(null);
  const [form, setForm] = useState(BLANK);
  const [err, setErr] = useState("");
  const [q, setQ] = useState({ node: "", PatientID: "", PatientName: "", StudyDate: "" });
  const [remote, setRemote] = useState([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      setData(await pacsApi.nodes(token));
      setJobs((await pacsApi.jobs(token)).jobs);
    } catch (e) { setErr(e.message); }
  }, [token]);
  useEffect(() => { load(); }, [load]);

  async function doEcho(id) {
    setEcho((x) => ({ ...x, [id]: "…" }));
    try {
      const r = await pacsApi.echo(id, token);
      setEcho((x) => ({ ...x, [id]: r.ok ? "ok" : "fail" }));
    } catch (_) { setEcho((x) => ({ ...x, [id]: "fail" })); }
  }
  function startEdit(n) { setEdit(n ? n.id : "new"); setForm(n ? { ...BLANK, ...n } : BLANK); }
  async function save(e) {
    e.preventDefault();
    try {
      const body = { ...form, port: form.port ? Number(form.port) : null };
      ["id", "created_at", "updated_at", "last_echo_at", "last_echo_ok", "has_credentials"].forEach((k) => delete body[k]);
      await pacsApi.saveNode(body, edit === "new" ? null : edit, token);
      setEdit(null); load();
    } catch (e2) { setErr(e2.message); }
  }
  async function remove(id) {
    try { await pacsApi.deleteNode(id, token); load(); } catch (e) { setErr(e.message); }
  }
  async function query(e) {
    e.preventDefault();
    if (!q.node) return;
    setBusy(true); setErr("");
    try {
      const filters = {};
      ["PatientID", "PatientName", "StudyDate"].forEach((k) => { if (q[k]) filters[k] = q[k]; });
      setRemote((await pacsApi.queryNode(q.node, filters, token)).studies);
    } catch (e2) { setErr(e2.message); } finally { setBusy(false); }
  }
  async function retrieve(uid) {
    try {
      await pacsApi.retrieve({ study_uid: uid, node_id: q.node }, token);
      setTimeout(load, 500);
    } catch (e) { setErr(e.message); }
  }
  const f = (k) => (e) => setForm((x) => ({ ...x, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
  const L = data.local;

  return (
    <div>
      <div className="page-head">
        <h2 className="page-title">{t("nodes.title")}</h2>
        <p className="page-sub">{t("nodes.sub")}</p>
      </div>
      {err && <div className="err">{err}</div>}
      {L && (
        <Card>
          <CardHead title={t("nodes.local")} />
          <div className="kv">
            <span>{t("nodes.ae")}</span><b className="mono">{L.ae_title}</b>
            <span>{t("nodes.port")}</span><b>{L.port}</b>
            <span>{t("pacs.scp")}</span><b><Badge tone={L.running ? "ok" : "muted"}>{L.running ? t("pacs.running") : t("pacs.stopped")}</Badge></b>
            <span>Allow-list</span><b>{L.require_known_peers ? "registered AEs only" : "open"}</b>
          </div>
        </Card>
      )}
      <Card>
        <CardHead title={`${t("nav.pacsNodes")} (${data.nodes.length})`}
                  right={<Button onClick={() => startEdit(null)} data-testid="node-add">{t("nodes.add")}</Button>} />
        <div className="table-scroll">
          <table className="data-table" data-testid="nodes-table">
            <thead><tr><th>{t("nodes.name")}</th><th>{t("nodes.kind")}</th><th>{t("nodes.ae")} / {t("nodes.url")}</th>
              <th>{t("nodes.perms")}</th><th /></tr></thead>
            <tbody>
              {data.nodes.map((n) => (
                <tr key={n.id}>
                  <td className="bold">{n.name} {!n.active && <Badge>inactive</Badge>}</td>
                  <td><Badge tone="info">{n.kind}</Badge></td>
                  <td className="mono small">{n.kind === "dimse" ? `${n.ae_title}@${n.host}:${n.port}` : n.base_url}</td>
                  <td className="small">
                    {n.allow_store && "store "}{n.allow_query && "query "}{n.allow_retrieve && "retrieve "}
                    {n.is_move_destination && "move-dest "}{n.federate && <Badge tone="ok">network</Badge>}
                  </td>
                  <td className="nowrap">
                    <Button variant="ghost" onClick={() => doEcho(n.id)}>{t("nodes.echo")}</Button>
                    {echo[n.id] && <Badge tone={echo[n.id] === "ok" ? "ok" : echo[n.id] === "fail" ? "err" : "muted"}>{echo[n.id]}</Badge>}
                    <Button variant="ghost" onClick={() => startEdit(n)}>✎</Button>
                    <Button variant="ghost" onClick={() => remove(n.id)}>✕</Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card>
        <CardHead title={`${t("nodes.query")} — remote`} />
        <form className="filter-grid" onSubmit={query}>
          <div><Label>Node</Label>
            <Select value={q.node} onChange={(e) => setQ({ ...q, node: e.target.value })}>
              <option value="">—</option>
              {data.federation_targets.concat(data.nodes.filter((n) => !n.federate).map((n) => ({ id: n.id, name: n.name })))
                .map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
            </Select></div>
          <div><Label>{t("pacs.patientId")}</Label><TextInput value={q.PatientID} onChange={(e) => setQ({ ...q, PatientID: e.target.value })} /></div>
          <div><Label>{t("pacs.patient")}</Label><TextInput value={q.PatientName} onChange={(e) => setQ({ ...q, PatientName: e.target.value })} placeholder="SMITH*" /></div>
          <div><Label>{t("pacs.col.date")}</Label><TextInput value={q.StudyDate} onChange={(e) => setQ({ ...q, StudyDate: e.target.value })} placeholder="20260101-20261231" /></div>
          <div className="filter-actions"><Button type="submit" loading={busy}>{t("nodes.query")}</Button></div>
        </form>
        <ul className="mini-list">
          {remote.map((s) => (
            <li key={s.StudyInstanceUID}>{fmtDicomDate(s.StudyDate)} · <b>{(s.PatientName || "").replace(/\^/g, " ")}</b> · {s.PatientID} · {s.StudyDescription || "—"} · {(s.ModalitiesInStudy || []).join("/")}
              <Button variant="ghost" onClick={() => retrieve(s.StudyInstanceUID)}>{t("pacs.retrieve")}</Button></li>
          ))}
        </ul>
      </Card>

      <Card>
        <CardHead title={t("nodes.jobs")} right={<Button variant="ghost" onClick={load}>↻</Button>} />
        <ul className="mini-list" data-testid="jobs">
          {jobs.slice(0, 30).map((j) => (
            <li key={j.id}><Badge tone={STATUS_TONE[j.status] || "muted"}>{j.status}</Badge> {j.kind} · {j.target} · <span className="mono small">{j.study_uid}</span>
              {j.result && <span className="muted"> · {JSON.stringify(j.result)}</span>}
              {j.last_error && <span className="err small"> · {j.last_error}</span>}</li>
          ))}
        </ul>
      </Card>

      <Modal open={!!edit} title={edit === "new" ? t("nodes.add") : form.name} onClose={() => setEdit(null)}>
        <form className="form-grid" onSubmit={save}>
          <div><Label>{t("nodes.name")}</Label><TextInput value={form.name} onChange={f("name")} required /></div>
          <div><Label>{t("nodes.kind")}</Label>
            <Select value={form.kind} onChange={f("kind")}><option value="dimse">DICOM (DIMSE)</option><option value="dicomweb">DICOMweb</option><option value="orthanc">Orthanc REST</option></Select></div>
          {form.kind === "dimse" ? (
            <>
              <div><Label>{t("nodes.ae")}</Label><TextInput value={form.ae_title} onChange={f("ae_title")} maxLength={16} required /></div>
              <div><Label>{t("nodes.host")}</Label><TextInput value={form.host} onChange={f("host")} required /></div>
              <div><Label>{t("nodes.port")}</Label><TextInput type="number" value={form.port || ""} onChange={f("port")} required /></div>
            </>
          ) : (
            <div className="span-2"><Label>{t("nodes.url")}</Label><TextInput value={form.base_url} onChange={f("base_url")} placeholder="https://pacs.example/dicom-web" required /></div>
          )}
          <div><Label>{t("nodes.cred")}</Label><TextInput value={form.auth_env || ""} onChange={f("auth_env")} placeholder="ORTHANC_CREDENTIALS" /></div>
          <div><Label>Facility OID</Label><TextInput value={form.facility_oid || ""} onChange={f("facility_oid")} /></div>
          <div className="span-2 checks">
            {["allow_store", "allow_query", "allow_retrieve", "is_move_destination", "federate", "active"].map((k) => (
              <label key={k}><input type="checkbox" checked={!!form[k]} onChange={f(k)} /> {k === "federate" ? t("nodes.federate") : k.replace(/_/g, " ")}</label>
            ))}
          </div>
          <div className="span-2 toolbar"><Button type="submit">{t("nodes.save")}</Button></div>
        </form>
      </Modal>
    </div>
  );
}
