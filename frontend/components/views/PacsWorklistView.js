import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { pacsApi, STATUS_TONE, fmtDicomDate } from "../../lib/pacs";
import { Badge, Button, Card, CardHead, EmptyState, Label, Modal, Select, TextInput } from "../ui";

const MODALITIES = ["CT", "MR", "CR", "DX", "US", "MG", "NM", "PT", "XA", "RF"];
const BLANK = { patient_name: "", patient_id: "", patient_birth_date: "", patient_sex: "",
                modality: "CT", procedure_description: "", procedure_code: "", station_ae: "",
                scheduled_start: "", priority: "", reason: "", referring_physician: "" };

function fmtStart(s) {
  if (!s) return "—";
  const d = s.replace(/[-:T]/g, "");
  return `${fmtDicomDate(d.slice(0, 8))} ${d.slice(8, 10) ? `${d.slice(8, 10)}:${d.slice(10, 12)}` : ""}`;
}

export default function PacsWorklistView() {
  const { t } = useT();
  const { token } = useAuth();
  const [items, setItems] = useState([]);
  const [mpps, setMpps] = useState([]);
  const [filter, setFilter] = useState({ status: "", modality: "", date: "" });
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(BLANK);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    if (!token) return;
    try {
      setItems((await pacsApi.worklist(filter, token)).items);
      setMpps((await pacsApi.mpps(token)).items);
    } catch (e) { setErr(e.message); }
  }, [token, filter]);
  useEffect(() => { load(); }, [load]);

  async function create(e) {
    e.preventDefault();
    try {
      const body = { ...form };
      if (body.scheduled_start) body.scheduled_start = body.scheduled_start.replace(/[-:]/g, "");
      Object.keys(body).forEach((k) => { if (!body[k]) delete body[k]; });
      await pacsApi.createWorklist(body, token);
      setOpen(false); setForm(BLANK); load();
    } catch (e2) { setErr(e2.message); }
  }
  async function setStatus(id, status) {
    try { await pacsApi.patchWorklist(id, { status }, token); load(); } catch (e) { setErr(e.message); }
  }
  const f = (k) => (e) => setForm((x) => ({ ...x, [k]: e.target.value }));

  return (
    <div>
      <div className="page-head">
        <h2 className="page-title">{t("wl.title")}</h2>
        <p className="page-sub">{t("wl.sub")}</p>
      </div>
      <Card>
        <div className="filter-grid">
          <div><Label>{t("pacs.status")}</Label>
            <Select value={filter.status} onChange={(e) => setFilter({ ...filter, status: e.target.value })}>
              <option value="">{t("pacs.any")}</option>
              {["scheduled", "in_progress", "completed", "discontinued", "cancelled"].map((s) => <option key={s}>{s}</option>)}
            </Select></div>
          <div><Label>{t("pacs.modality")}</Label>
            <Select value={filter.modality} onChange={(e) => setFilter({ ...filter, modality: e.target.value })}>
              <option value="">{t("pacs.any")}</option>
              {MODALITIES.map((m) => <option key={m}>{m}</option>)}
            </Select></div>
          <div><Label>{t("pacs.col.date")}</Label>
            <TextInput type="date" value={filter.date} onChange={(e) => setFilter({ ...filter, date: e.target.value })} /></div>
          <div className="filter-actions">
            <Button onClick={() => setOpen(true)} data-testid="wl-new">{t("wl.new")}</Button>
          </div>
        </div>
      </Card>
      {err && <div className="err">{err}</div>}
      <Card>
        <CardHead title={t("wl.title")} right={<span className="muted">{items.length}</span>} />
        {items.length === 0 ? <EmptyState>{t("wl.empty")}</EmptyState> : (
          <div className="table-scroll">
            <table className="data-table" data-testid="worklist-table">
              <thead><tr><th>{t("wl.scheduled")}</th><th>{t("pacs.col.patient")}</th><th>{t("wl.procedure")}</th>
                <th>{t("wl.station")}</th><th>{t("pacs.accession")}</th><th>{t("wl.priority")}</th><th>{t("pacs.status")}</th><th /></tr></thead>
              <tbody>
                {items.map((w) => (
                  <tr key={w.id}>
                    <td className="nowrap">{fmtStart(w.scheduled_start)}</td>
                    <td><div className="bold" dir="auto">{(w.patient_name || "—").replace(/\^/g, " ")}</div>
                      <div className="muted small">{w.patient_id || "—"}</div></td>
                    <td><div dir="auto">{w.procedure_description || "—"}</div><div className="muted small">{w.modality} · {w.reason || ""}</div></td>
                    <td>{w.station_ae || "—"}</td>
                    <td className="mono small">{w.accession}</td>
                    <td>{w.priority && <Badge tone="err">{w.priority}</Badge>}</td>
                    <td><Badge tone={STATUS_TONE[w.status] || "muted"}>{w.status}</Badge></td>
                    <td className="nowrap">
                      {w.status === "scheduled" && <Button variant="ghost" onClick={() => setStatus(w.id, "in_progress")} data-testid="wl-start">{t("wl.start")}</Button>}
                      {w.status === "in_progress" && <Button variant="ghost" onClick={() => setStatus(w.id, "completed")}>{t("wl.complete")}</Button>}
                      {["scheduled", "in_progress"].includes(w.status) &&
                        <Button variant="ghost" onClick={() => setStatus(w.id, "cancelled")}>{t("wl.cancel")}</Button>}
                      {w.study_uid && w.status !== "scheduled" &&
                        <a className="btn ghost" href={`/pacs/viewer?study=${w.study_uid}`}>{t("pacs.open")}</a>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Card>
        <CardHead title={t("wl.mpps")} />
        <ul className="mini-list">
          {mpps.slice(0, 20).map((m) => (
            <li key={m.sop_uid}><Badge tone={m.status === "COMPLETED" ? "ok" : m.status === "DISCONTINUED" ? "err" : "warn"}>{m.status}</Badge>
              {" "}{m.modality} · {m.accession || "—"} · {m.station_ae || "—"}</li>
          ))}
        </ul>
      </Card>

      <Modal open={open} title={t("wl.new")} onClose={() => setOpen(false)}>
        <form className="form-grid" onSubmit={create}>
          <div><Label>{t("pacs.patient")}</Label><TextInput value={form.patient_name} onChange={f("patient_name")} placeholder="Family^Given" dir="auto" required /></div>
          <div><Label>{t("pacs.patientId")}</Label><TextInput value={form.patient_id} onChange={f("patient_id")} /></div>
          <div><Label>DOB</Label><TextInput type="date" value={form.patient_birth_date} onChange={f("patient_birth_date")} /></div>
          <div><Label>Sex</Label><Select value={form.patient_sex} onChange={f("patient_sex")}><option value="" /><option>M</option><option>F</option><option>O</option></Select></div>
          <div><Label>{t("pacs.modality")}</Label><Select value={form.modality} onChange={f("modality")}>{MODALITIES.map((m) => <option key={m}>{m}</option>)}</Select></div>
          <div><Label>{t("wl.procedure")}</Label><TextInput value={form.procedure_description} onChange={f("procedure_description")} dir="auto" required /></div>
          <div><Label>Code</Label><TextInput value={form.procedure_code} onChange={f("procedure_code")} /></div>
          <div><Label>{t("wl.station")}</Label><TextInput value={form.station_ae} onChange={f("station_ae")} /></div>
          <div><Label>{t("wl.scheduled")}</Label><TextInput type="datetime-local" value={form.scheduled_start} onChange={f("scheduled_start")} /></div>
          <div><Label>{t("wl.priority")}</Label><Select value={form.priority} onChange={f("priority")}><option value="">ROUTINE</option><option>STAT</option><option>HIGH</option></Select></div>
          <div className="span-2"><Label>{t("wl.reason")}</Label><TextInput value={form.reason} onChange={f("reason")} dir="auto" /></div>
          <div className="span-2"><Label>{t("wl.referrer")}</Label><TextInput value={form.referring_physician} onChange={f("referring_physician")} dir="auto" /></div>
          <div className="span-2 toolbar"><Button type="submit">{t("wl.create")}</Button></div>
        </form>
      </Modal>
    </div>
  );
}
