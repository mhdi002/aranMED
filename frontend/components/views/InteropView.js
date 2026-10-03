import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { interopApi, fmtDate } from "../../lib/clinical";
import { Badge, Button, Card, CardHead, Label, Modal, Select, TextArea, TextInput } from "../ui";

const BLANK = { oid: "", name: "", kind: "hospital", base_url: "", fhir_base: "", dicomweb_base: "",
                ae_title: "", dicom_host: "", dicom_port: "", mllp_host: "", mllp_port: "",
                secret_env: "", trust_level: "peer" };
const SAMPLE_HL7 = "MSH|^~\\&|HIS|HOSP|ARANMED|AM|20261003120000||ADT^A04|MSG0001|P|2.5\rPID|1||TEST-1^^^&2.25.0.1&ISO^MR||Doe^Jane||19800101|F";

export default function InteropView() {
  const { t } = useT();
  const { token } = useAuth();
  const [status, setStatus] = useState(null);
  const [caps, setCaps] = useState(null);
  const [facs, setFacs] = useState([]);
  const [msgs, setMsgs] = useState([]);
  const [links, setLinks] = useState([]);
  const [proto, setProto] = useState("");
  const [tests, setTests] = useState({});
  const [edit, setEdit] = useState(false);
  const [form, setForm] = useState(BLANK);
  const [raw, setRaw] = useState(null);
  const [hl7, setHl7] = useState(SAMPLE_HL7);
  const [hl7Ack, setHl7Ack] = useState("");
  const [cdaResult, setCdaResult] = useState(null);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    if (!token) return;
    try {
      setStatus(await interopApi.status(token));
      setCaps(await interopApi.capabilities(token));
      setFacs((await interopApi.facilities(token)).facilities);
      setMsgs((await interopApi.messages({ protocol: proto || undefined, limit: 100 }, token)).messages);
      setLinks((await interopApi.mpiLinks(token)).links);
    } catch (e) { setErr(e.message); }
  }, [token, proto]);
  useEffect(() => { load(); }, [load]);

  async function test(oid) {
    setTests((x) => ({ ...x, [oid]: { running: true } }));
    try { const r = await interopApi.testPeer(oid, token); setTests((x) => ({ ...x, [oid]: r })); }
    catch (e) { setTests((x) => ({ ...x, [oid]: { error: e.message } })); }
  }
  async function save(e) {
    e.preventDefault();
    try {
      const body = { ...form };
      ["dicom_port", "mllp_port"].forEach((k) => { body[k] = body[k] ? Number(body[k]) : null; });
      Object.keys(body).forEach((k) => { if (body[k] === "" || body[k] === null) delete body[k]; });
      await interopApi.saveFacility(body, token);
      setEdit(false); setForm(BLANK); load();
    } catch (e2) { setErr(e2.message); }
  }
  async function sendHl7() {
    try { setHl7Ack(await interopApi.hl7(hl7.replace(/\n/g, "\r"), token)); load(); } catch (e) { setHl7Ack(e.message); }
  }
  async function importCda(file) {
    try { setCdaResult(await interopApi.importCda(await file.text(), token)); load(); } catch (e) { setCdaResult({ error: e.message }); }
  }
  async function resolve(id, accept) {
    try { await interopApi.resolveLink(id, accept, token); load(); } catch (e) { setErr(e.message); }
  }
  const f = (k) => (e) => setForm((x) => ({ ...x, [k]: e.target.value }));

  return (
    <div>
      <div className="page-head">
        <h2 className="page-title">{t("io.title")}</h2>
        <p className="page-sub">{t("io.sub")}</p>
      </div>
      {err && <div className="err">{err}</div>}
      {status && (
        <div className="stat-row" data-testid="interop-status">
          <div className="stat"><span>{t("io.facility")}</span><b>{status.facility.name}</b><span className="mono">{status.facility.oid}</span></div>
          <div className="stat"><span>FHIR R4</span><b className="mono small">{status.fhir_base}</b></div>
          <div className="stat"><span>DICOM</span><b><Badge tone={status.dimse.running ? "ok" : "muted"}>{status.dimse.ae_title}:{status.dimse.port}</Badge></b></div>
          <div className="stat"><span>HL7 MLLP</span><b><Badge tone={status.mllp.running ? "ok" : "muted"}>:{status.mllp.port} {status.mllp.running ? "on" : "off"}</Badge></b></div>
          {caps && <div className="stat"><span>Schema</span><b className="small">{Object.entries(caps.schema).map(([k, v]) => `${k} v${v}`).join(" · ")}</b></div>}
        </div>
      )}

      <Card>
        <CardHead title={t("io.peers")} right={<Button onClick={() => { setForm(BLANK); setEdit(true); }} data-testid="peer-add">{t("io.addPeer")}</Button>} />
        <div className="table-scroll"><table className="data-table" data-testid="peers-table">
          <thead><tr><th>Name</th><th>OID</th><th>Kind / trust</th><th>Endpoints</th><th>Secret</th><th /></tr></thead>
          <tbody>{facs.filter((x) => !x.is_local).map((x) => (
            <tr key={x.oid}><td className="bold">{x.name}</td><td className="mono small">{x.oid}</td><td>{x.kind} / {x.trust_level}</td>
              <td className="small">{x.fhir_base && <div>FHIR {x.fhir_base}</div>}{x.dicomweb_base && <div>DICOMweb {x.dicomweb_base}</div>}{x.dicom_host && <div>DIMSE {x.ae_title}@{x.dicom_host}:{x.dicom_port}</div>}{x.mllp_host && <div>MLLP {x.mllp_host}:{x.mllp_port}</div>}</td>
              <td><Badge tone={x.has_secret ? "ok" : "err"}>{x.secret_env || "—"}</Badge></td>
              <td className="nowrap"><Button variant="ghost" onClick={() => test(x.oid)} data-testid={`test-${x.oid}`}>{t("io.test")}</Button>
                <Button variant="ghost" onClick={() => { setForm({ ...BLANK, ...x }); setEdit(true); }}>✎</Button>
                {tests[x.oid] && <div className="small" data-testid={`test-result-${x.oid}`}>
                  {tests[x.oid].error ? <span className="err">{tests[x.oid].error}</span> : <>
                    <Badge tone={tests[x.oid].reachable ? "ok" : "err"}>reachable</Badge>
                    <Badge tone={tests[x.oid].schema_compatible ? "ok" : "err"}>schema</Badge>
                    <Badge tone={tests[x.oid].token_accepted ? "ok" : "err"}>token</Badge></>}</div>}
              </td></tr>))}</tbody></table></div>
      </Card>

      <div className="chart-grid">
        <Card><CardHead title={t("io.mpi")} right={<span className="muted">{links.length}</span>} />
          <ul className="mini-list">{links.map((l) => (
            <li key={l.id}>score {l.score} · <span className="mono small">{l.person_id.slice(0, 8)} ↔ {l.other_id.slice(0, 8)}</span>
              <div className="toolbar"><Button variant="ghost" onClick={() => resolve(l.id, true)}>{t("io.accept")}</Button><Button variant="ghost" onClick={() => resolve(l.id, false)}>{t("io.rejectLink")}</Button></div></li>))}</ul>
        </Card>
        <Card><CardHead title={t("io.hl7")} />
          <TextArea value={hl7} onChange={(e) => setHl7(e.target.value)} className="mono" style={{ minHeight: 110 }} data-testid="hl7-input" />
          <div className="toolbar"><Button onClick={sendHl7} data-testid="hl7-send">{t("pacs.send")}</Button></div>
          {hl7Ack && <pre className="raw" data-testid="hl7-ack">{hl7Ack}</pre>}
        </Card>
        <Card><CardHead title={t("io.cda")} />
          <input type="file" accept=".xml,application/xml" onChange={(e) => e.target.files[0] && importCda(e.target.files[0])} data-testid="cda-input" />
          {cdaResult && <pre className="raw">{JSON.stringify(cdaResult, null, 2)}</pre>}
        </Card>
      </div>

      <Card>
        <CardHead title={t("io.messages")} right={
          <Select value={proto} onChange={(e) => setProto(e.target.value)} aria-label="protocol">
            <option value="">all</option>{["hl7v2", "fhir", "dicom", "dicomweb", "transfer", "ems", "orthanc"].map((p) => <option key={p}>{p}</option>)}
          </Select>} />
        <div className="table-scroll"><table className="data-table" data-testid="messages-table">
          <thead><tr><th>Time</th><th>Dir</th><th>Protocol</th><th>Type</th><th>Peer</th><th>Status</th><th /></tr></thead>
          <tbody>{msgs.map((m) => (
            <tr key={m.id}><td className="nowrap small">{fmtDate(m.created_at)}</td><td>{m.direction}</td><td>{m.protocol}</td><td className="small">{m.message_type}</td><td className="small">{m.peer}</td>
              <td><Badge tone={m.status === "ok" ? "ok" : m.status === "rejected" ? "err" : "warn"}>{m.status}</Badge></td>
              <td>{(m.payload || m.response) && <Button variant="ghost" onClick={() => setRaw(m)}>raw</Button>}</td></tr>))}</tbody></table></div>
      </Card>

      <Modal open={!!raw} title={`${raw?.protocol} ${raw?.message_type || ""}`} onClose={() => setRaw(null)}>
        {raw?.error && <div className="err">{raw.error}</div>}
        <div className="lbl">payload</div><pre className="raw">{(raw?.payload || "").replace(/\r/g, "\n")}</pre>
        {raw?.response && <><div className="lbl">response</div><pre className="raw">{raw.response.replace(/\r/g, "\n")}</pre></>}
      </Modal>
      <Modal open={edit} title={t("io.addPeer")} onClose={() => setEdit(false)}>
        <form className="form-grid" onSubmit={save}>
          <div><Label>OID</Label><TextInput value={form.oid} onChange={f("oid")} required /></div>
          <div><Label>Name</Label><TextInput value={form.name} onChange={f("name")} required /></div>
          <div><Label>Kind</Label><Select value={form.kind} onChange={f("kind")}>{["hospital", "imaging-center", "ems", "clinic"].map((k) => <option key={k}>{k}</option>)}</Select></div>
          <div><Label>Trust</Label><Select value={form.trust_level} onChange={f("trust_level")}>{["peer", "partner", "ems", "imaging-center", "untrusted"].map((k) => <option key={k}>{k}</option>)}</Select></div>
          <div className="span-2"><Label>Base URL</Label><TextInput value={form.base_url || ""} onChange={f("base_url")} placeholder="https://peer.example" /></div>
          <div><Label>FHIR base</Label><TextInput value={form.fhir_base || ""} onChange={f("fhir_base")} /></div>
          <div><Label>DICOMweb base</Label><TextInput value={form.dicomweb_base || ""} onChange={f("dicomweb_base")} /></div>
          <div><Label>AE title</Label><TextInput value={form.ae_title || ""} onChange={f("ae_title")} /></div>
          <div><Label>DICOM host:port</Label><div className="row"><TextInput value={form.dicom_host || ""} onChange={f("dicom_host")} /><TextInput value={form.dicom_port || ""} onChange={f("dicom_port")} type="number" /></div></div>
          <div><Label>MLLP host:port</Label><div className="row"><TextInput value={form.mllp_host || ""} onChange={f("mllp_host")} /><TextInput value={form.mllp_port || ""} onChange={f("mllp_port")} type="number" /></div></div>
          <div><Label>Secret env var</Label><TextInput value={form.secret_env || ""} onChange={f("secret_env")} placeholder="PEER_X_SECRET" /></div>
          <div className="span-2 toolbar"><Button type="submit">{t("ch.save")}</Button></div>
        </form>
      </Modal>
    </div>
  );
}
