import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/router";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { transferApi, fmtDate, TRANSFER_STEPS } from "../../lib/clinical";
import { Badge, Button, Card, CardHead, EmptyState, TextInput } from "../ui";

const ACTIONS = {
  incoming: { requested: ["accept", "reject"], accepted: ["arrive"], in_transit: ["arrive"], arrived: ["complete"] },
  outgoing: { requested: ["cancel"], accepted: ["depart", "cancel"] },
};

export default function TransfersView() {
  const { t } = useT();
  const { token, user } = useAuth();
  const router = useRouter();
  const [dir, setDir] = useState("incoming");
  const [items, setItems] = useState([]);
  const [err, setErr] = useState("");
  const [note, setNote] = useState({});
  const canWrite = user && ["doctor", "admin"].includes(user.role);

  const load = useCallback(async () => {
    if (!token) return;
    try { setItems((await transferApi.list({ direction: dir }, token)).transfers); } catch (e) { setErr(e.message); }
  }, [token, dir]);
  useEffect(() => { load(); const h = setInterval(load, 10000); return () => clearInterval(h); }, [load]);

  async function act(tr, action) {
    setErr("");
    try {
      await transferApi.step(tr.id, action, { note: note[tr.id] || "", reason: note[tr.id] || "" }, token);
      await load();
    } catch (e) { setErr(e.message); }
  }

  return (
    <div>
      <div className="page-head">
        <h2 className="page-title">{t("tr.title")}</h2>
        <p className="page-sub">{t("tr.sub")}</p>
      </div>
      <div className="chart-tabs" role="tablist">
        {["incoming", "outgoing"].map((d) => (
          <button key={d} type="button" role="tab" className={`tab ${dir === d ? "active" : ""}`}
                  aria-selected={dir === d} onClick={() => setDir(d)} data-dir={d}>{t(`tr.${d}`)}</button>
        ))}
      </div>
      {err && <div className="err">{err}</div>}
      {items.length === 0 ? <EmptyState>{t("tr.none")}</EmptyState> : (
        <div className="board" data-testid="transfers">
          {items.map((tr) => (
            <Card key={tr.id} data-testid={`transfer-${tr.status}`}>
              <CardHead title={tr.patient ? tr.patient.name : tr.person_id}
                        right={<Badge tone={tr.urgency === "routine" ? "muted" : "err"}>{tr.urgency}</Badge>} />
              <div className="small"><b>{tr.from_facility_name}</b> → <b>{tr.to_facility_name}</b></div>
              <div className="status-steps" style={{ margin: "8px 0" }}>
                {tr.status === "rejected" || tr.status === "cancelled"
                  ? <span className="step-item current">{tr.status}</span>
                  : TRANSFER_STEPS.map((st) => <span key={st} className={`step-item ${tr.status === st ? "current" : TRANSFER_STEPS.indexOf(st) < TRANSFER_STEPS.indexOf(tr.status) ? "done" : ""}`}>{st}</span>)}
              </div>
              <div className="kv">
                <span>{t("tr.reason")}</span><b dir="auto">{tr.reason || "—"}</b>
                <span>{t("tr.summary")}</span><b dir="auto">{tr.clinical_summary || "—"}</b>
                <span>{t("tr.transport")}</span><b>{tr.transport_mode || "—"} {tr.eta ? `· ETA ${fmtDate(tr.eta)}` : ""}</b>
                <span>{t("tr.package")}</span><b>{tr.package_status || "—"}
                  {tr.package_manifest?.imported && <span className="muted small"> · {Object.entries(tr.package_manifest.imported).map(([k, v]) => `${k} ${v}`).join(", ")}</span>}
                  {tr.package_manifest?.studies?.length > 0 && <span className="muted small"> · {tr.package_manifest.studies.length} studies</span>}</b>
              </div>
              {canWrite && (ACTIONS[tr.direction]?.[tr.status] || []).length > 0 && (
                <>
                  <TextInput placeholder="note / reason" value={note[tr.id] || ""} onChange={(e) => setNote({ ...note, [tr.id]: e.target.value })} />
                  <div className="toolbar">
                    {(ACTIONS[tr.direction][tr.status] || []).map((a) => (
                      <Button key={a} variant={a === "reject" || a === "cancel" ? "danger" : "solid"} onClick={() => act(tr, a)} data-action={a}>{t(`tr.${a}`)}</Button>
                    ))}
                  </div>
                </>
              )}
              <div className="toolbar"><Button variant="ghost" onClick={() => router.push(`/clinical/chart?id=${tr.person_id}`)}>{t("cl.open")}</Button></div>
              <details className="small"><summary>history</summary>
                <ul className="mini-list">{(tr.history || []).map((h, i) => <li key={i}>{fmtDate(h.at)} · {h.status || "note"} · {h.by || ""} {h.note ? `— ${h.note}` : ""}</li>)}</ul>
              </details>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
