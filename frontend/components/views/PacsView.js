import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/router";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import {
  pacsApi, dicomwebObjectUrl, fmtDicomDate, fmtBytes, STATUS_TONE,
} from "../../lib/pacs";
import { Badge, Button, Card, CardHead, EmptyState, Select, TextInput, Label } from "../ui";
import StudyPanel from "../pacs/StudyPanel";

const MODALITIES = ["CT", "MR", "CR", "DX", "US", "MG", "NM", "PT", "XA", "RF", "OT"];
const STATUSES = ["received", "in_review", "read", "reported", "final", "archived"];
const EMPTY = { q: "", patient_id: "", accession: "", modality: "", date_from: "", date_to: "", status: "", scope: "local" };

function Thumb({ study, token }) {
  const [src, setSrc] = useState(null);
  const local = (study.locations || [{ kind: "local" }]).some((l) => l.kind === "local") || !study.locations;
  useEffect(() => {
    let url;
    let live = true;
    if (local && token) {
      dicomwebObjectUrl(`/studies/${study.StudyInstanceUID}/thumbnail?size=96`, token)
        .then((u) => { url = u; if (live) setSrc(u); }).catch(() => {});
    }
    return () => { live = false; if (url) URL.revokeObjectURL(url); };
  }, [study.StudyInstanceUID, token, local]);
  return (
    <div className="thumb">
      {src ? <img src={src} alt="" /> : <span>{(study.ModalitiesInStudy || []).join("/") || "—"}</span>}
    </div>
  );
}

export default function PacsView() {
  const { t } = useT();
  const { token, user } = useAuth();
  const router = useRouter();
  const [filters, setFilters] = useState(EMPTY);
  const [data, setData] = useState({ studies: [], total: 0, errors: [] });
  const [stats, setStats] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [selected, setSelected] = useState(null);
  const [retrieving, setRetrieving] = useState({});

  const search = useCallback(async (f = filters) => {
    if (!token) return;
    setBusy(true); setErr("");
    try {
      setData(await pacsApi.studies({ ...f, limit: 100 }, token));
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }, [token, filters]);

  useEffect(() => { search(EMPTY); }, [token]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (token) pacsApi.stats(token).then(setStats).catch(() => {}); }, [token, data]);

  const set = (k) => (e) => setFilters((f) => ({ ...f, [k]: e.target.value }));
  const onSubmit = (e) => { e.preventDefault(); search(); };

  async function retrieve(study) {
    const loc = (study.locations || []).find((l) => l.kind !== "local");
    if (!loc) return;
    setRetrieving((r) => ({ ...r, [study.StudyInstanceUID]: true }));
    try {
      const job = await pacsApi.retrieve({ study_uid: study.StudyInstanceUID, node_id: loc.id, wait: true }, token);
      if (job.status !== "done") throw new Error(job.last_error || "retrieve failed");
      await search();
    } catch (e) { setErr(e.message); } finally {
      setRetrieving((r) => ({ ...r, [study.StudyInstanceUID]: false }));
    }
  }

  const openViewer = (uid) => router.push(`/pacs/viewer?study=${encodeURIComponent(uid)}`);

  return (
    <div>
      <div className="page-head">
        <h2 className="page-title">{t("pacs.title")}</h2>
        <p className="page-sub">{t("pacs.sub")}</p>
      </div>

      {stats && (
        <div className="stat-row" data-testid="pacs-stats">
          <div className="stat"><span>{t("pacs.studies")}</span><b>{stats.studies}</b></div>
          <div className="stat"><span>{t("pacs.instances")}</span><b>{stats.instances}</b></div>
          <div className="stat"><span>{t("pacs.storage")}</span><b>{fmtBytes(stats.bytes)}</b></div>
          <div className="stat"><span>{t("nav.pacsWorklist")}</span><b>{(stats.worklist?.scheduled || 0) + (stats.worklist?.in_progress || 0)}</b></div>
          <div className="stat"><span>{t("pacs.scp")}</span>
            <b><Badge tone={stats.dimse?.running ? "ok" : "muted"}>
              {stats.dimse?.ae_title}:{stats.dimse?.port} · {stats.dimse?.running ? t("pacs.running") : t("pacs.stopped")}
            </Badge></b></div>
        </div>
      )}

      <Card>
        <form className="filter-grid" onSubmit={onSubmit} aria-label="Study search">
          <div><Label>{t("pacs.patient")}</Label><TextInput value={filters.q} onChange={set("q")} dir="auto" name="q" /></div>
          <div><Label>{t("pacs.patientId")}</Label><TextInput value={filters.patient_id} onChange={set("patient_id")} name="patient_id" /></div>
          <div><Label>{t("pacs.accession")}</Label><TextInput value={filters.accession} onChange={set("accession")} /></div>
          <div><Label>{t("pacs.modality")}</Label>
            <Select value={filters.modality} onChange={set("modality")} name="modality">
              <option value="">{t("pacs.any")}</option>
              {MODALITIES.map((m) => <option key={m} value={m}>{m}</option>)}
            </Select></div>
          <div><Label>{t("pacs.dateFrom")}</Label><TextInput type="date" value={filters.date_from} onChange={set("date_from")} /></div>
          <div><Label>{t("pacs.dateTo")}</Label><TextInput type="date" value={filters.date_to} onChange={set("date_to")} /></div>
          <div><Label>{t("pacs.status")}</Label>
            <Select value={filters.status} onChange={set("status")}>
              <option value="">{t("pacs.any")}</option>
              {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </Select></div>
          <div><Label>{t("pacs.scope")}</Label>
            <Select value={filters.scope} onChange={set("scope")} name="scope">
              <option value="local">{t("pacs.scope.local")}</option>
              <option value="all">{t("pacs.scope.all")}</option>
              <option value="remote">{t("pacs.scope.remote")}</option>
            </Select></div>
          <div className="filter-actions">
            <Button type="submit" loading={busy}>{t("pacs.search")}</Button>
            <Button type="button" variant="ghost" onClick={() => { setFilters(EMPTY); search(EMPTY); }}>{t("pacs.reset")}</Button>
          </div>
        </form>
      </Card>

      {err && <div className="err" role="alert">{err}</div>}
      {data.errors?.length > 0 && (
        <div className="warn-banner">{t("pacs.unreachable")}: {data.errors.map((e) => e.node).join(", ")}</div>
      )}

      <div className={`pacs-layout ${selected ? "with-panel" : ""}`}>
        <Card className="table-card">
          <CardHead title={t("pacs.studies")} right={<span className="muted">{data.total}</span>} />
          {data.studies.length === 0 ? (
            <EmptyState>{t("pacs.noStudies")}</EmptyState>
          ) : (
            <div className="table-scroll">
              <table className="data-table" data-testid="study-table">
                <thead><tr>
                  <th />
                  <th>{t("pacs.col.date")}</th>
                  <th>{t("pacs.col.patient")}</th>
                  <th>{t("pacs.col.study")}</th>
                  <th>{t("pacs.col.images")}</th>
                  <th>{t("pacs.col.status")}</th>
                  <th>{t("pacs.col.location")}</th>
                  <th />
                </tr></thead>
                <tbody>
                  {data.studies.map((s) => {
                    const ext = s._ext || {};
                    const locs = s.locations || [{ kind: "local", name: "local" }];
                    const isLocal = locs.some((l) => l.kind === "local");
                    return (
                      <tr key={s.StudyInstanceUID}
                          className={selected === s.StudyInstanceUID ? "selected" : ""}
                          onClick={() => isLocal && setSelected(s.StudyInstanceUID)}
                          onDoubleClick={() => isLocal && openViewer(s.StudyInstanceUID)}>
                        <td><Thumb study={s} token={token} /></td>
                        <td className="nowrap">{fmtDicomDate(s.StudyDate)}</td>
                        <td className="col-wide">
                          <div className="bold" dir="auto">{(s.PatientName || "—").replace(/\^/g, " ")}</div>
                          <div className="muted small">{s.PatientID || "—"} · {s.PatientSex || "?"} · {fmtDicomDate(s.PatientBirthDate)}</div>
                        </td>
                        <td className="col-wide">
                          <div dir="auto">{s.StudyDescription || "—"}</div>
                          <div className="muted small">{(s.ModalitiesInStudy || []).join(", ")} · {s.AccessionNumber || "—"}</div>
                        </td>
                        <td className="nowrap">{s.NumberOfStudyRelatedSeries ?? "?"} / {s.NumberOfStudyRelatedInstances ?? "?"}</td>
                        <td>
                          {ext.status && <Badge tone={STATUS_TONE[ext.status] || "muted"}>{ext.status}</Badge>}
                          {ext.priority && <Badge tone="err">{ext.priority}</Badge>}
                        </td>
                        <td className="small">{locs.map((l) => l.name).join(", ")}</td>
                        <td className="nowrap" onClick={(e) => e.stopPropagation()}>
                          {isLocal ? (
                            <Button variant="ghost" onClick={() => openViewer(s.StudyInstanceUID)} data-testid="open-viewer">
                              {t("pacs.open")}
                            </Button>
                          ) : (
                            <Button variant="ghost" loading={retrieving[s.StudyInstanceUID]} onClick={() => retrieve(s)}>
                              {t("pacs.retrieve")}
                            </Button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        {selected && (
          <StudyPanel studyUid={selected} token={token} user={user} onClose={() => setSelected(null)}
                      onChanged={() => search()} onOpen={openViewer} />
        )}
      </div>
    </div>
  );
}
