// Study detail side panel: series, priors, order, reports (with editor), AI.
import { useCallback, useEffect, useState } from "react";
import { useT } from "../../lib/i18n";
import { pacsApi, fmtDicomDate, fmtBytes, STATUS_TONE } from "../../lib/pacs";
import { Badge, Button, Card, CardHead, ConfirmModal, Select } from "../ui";
import ReportEditor from "./ReportEditor";

export default function StudyPanel({ studyUid, token, user, onClose, onChanged, onOpen, compact = false }) {
  const { t } = useT();
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [nodes, setNodes] = useState([]);
  const [target, setTarget] = useState("");
  const [msg, setMsg] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const isAdmin = user?.role === "admin";

  const load = useCallback(async () => {
    try { setD(await pacsApi.study(studyUid, token)); setErr(""); } catch (e) { setErr(e.message); }
  }, [studyUid, token]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (isAdmin) pacsApi.nodes(token).then((n) => setNodes(n.federation_targets || [])).catch(() => {});
  }, [isAdmin, token]);

  if (err) return <Card className="study-panel"><div className="err">{err}</div></Card>;
  if (!d) return <Card className="study-panel"><span className="spinner" /></Card>;
  const s = d.study;
  const ext = s._ext || {};

  async function verify() {
    const r = await pacsApi.verify(studyUid, token);
    setMsg(`✓ ${r.ok} ok · ${r.corrupt.length} corrupt · ${r.missing.length} missing`);
  }
  async function send() {
    if (!target) return;
    setMsg("…");
    try {
      const job = await pacsApi.send({ study_uid: studyUid, node_id: target, wait: true }, token);
      setMsg(job.status === "done" ? `✓ sent ${job.result?.sent}` : `✗ ${job.last_error}`);
    } catch (e) { setMsg(e.message); }
  }
  async function del() {
    await pacsApi.deleteStudy(studyUid, token);
    setConfirmDelete(false);
    onClose?.(); onChanged?.();
  }

  return (
    <Card className="study-panel" data-testid="study-panel">
      <CardHead title={(s.PatientName || "—").replace(/\^/g, " ")}
                right={onClose && <Button variant="ghost" iconOnly onClick={onClose} aria-label="Close">✕</Button>} />
      <div className="kv">
        <span>{t("pacs.col.study")}</span><b dir="auto">{s.StudyDescription || "—"}</b>
        <span>{t("pacs.col.date")}</span><b>{fmtDicomDate(s.StudyDate)}</b>
        <span>{t("pacs.patientId")}</span><b>{s.PatientID || "—"}</b>
        <span>{t("pacs.accession")}</span><b>{s.AccessionNumber || "—"}</b>
        <span>{t("pacs.status")}</span><b><Badge tone={STATUS_TONE[ext.status] || "muted"}>{ext.status}</Badge></b>
        <span>{t("pacs.storage")}</span><b>{fmtBytes(ext.size_bytes)}</b>
      </div>
      {onOpen && <div className="toolbar"><Button onClick={() => onOpen(studyUid)}>{t("pacs.open")}</Button></div>}

      <div className="lbl">{t("pacs.series")}</div>
      <ul className="mini-list">
        {d.series.map((se) => (
          <li key={se.SeriesInstanceUID}>
            <b>{se.Modality} #{se.SeriesNumber ?? "?"}</b> <span dir="auto">{se.SeriesDescription || "—"}</span>
            <span className="muted"> · {se.NumberOfSeriesRelatedInstances} img</span>
          </li>
        ))}
      </ul>

      {d.worklist && (
        <>
          <div className="lbl">{t("pacs.order")}</div>
          <div className="small">{d.worklist.procedure_description || "—"} · {d.worklist.reason || ""}
            {d.worklist.priority && <Badge tone="err">{d.worklist.priority}</Badge>}</div>
        </>
      )}

      {!compact && (
        <>
          <div className="lbl">{t("pacs.reports")}</div>
          <ReportEditor studyUid={studyUid} reports={d.reports} token={token} user={user}
                        onSaved={() => { load(); onChanged?.(); }} />
        </>
      )}

      {d.priors?.length > 0 && (
        <>
          <div className="lbl">{t("pacs.priors")}</div>
          <ul className="mini-list">
            {d.priors.map((p) => (
              <li key={p.StudyInstanceUID}>
                {fmtDicomDate(p.StudyDate)} · {(p.ModalitiesInStudy || []).join("/")} · <span dir="auto">{p.StudyDescription || "—"}</span>
                {onOpen && <Button variant="ghost" className="small" onClick={() => onOpen(p.StudyInstanceUID)}>{t("pacs.open")}</Button>}
              </li>
            ))}
          </ul>
        </>
      )}

      <div className="toolbar">
        <Button variant="ghost" onClick={verify}>{t("pacs.verify")}</Button>
        {isAdmin && (
          <>
            <Select value={target} onChange={(e) => setTarget(e.target.value)} aria-label={t("pacs.sendTo")}>
              <option value="">{t("pacs.sendTo")}…</option>
              {nodes.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
            </Select>
            <Button variant="ghost" onClick={send} disabled={!target}>→</Button>
            <Button variant="danger" onClick={() => setConfirmDelete(true)}>{t("pacs.delete")}</Button>
          </>
        )}
      </div>
      {msg && <div className="muted small">{msg}</div>}
      <ConfirmModal open={confirmDelete} title={t("pacs.delete")} danger confirmLabel={t("pacs.delete")}
                    onCancel={() => setConfirmDelete(false)} onConfirm={del}>
        {s.StudyDescription} — {(s.PatientName || "").replace(/\^/g, " ")}
      </ConfirmModal>
    </Card>
  );
}
