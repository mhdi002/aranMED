import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/router";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { emsApi, fmtDate } from "../../lib/clinical";
import { Badge, Button, Card, CardHead, EmptyState } from "../ui";

const NEXT = { inbound: ["acknowledged", "arrived"], acknowledged: ["arrived"], arrived: ["handed_over"] };
const LABEL = { acknowledged: "ems.ack", arrived: "ems.arrived", handed_over: "ems.handover" };
const ABN = { sbp: (v) => v < 90 || v > 180, hr: (v) => v < 50 || v > 120, spo2: (v) => v < 92, rr: (v) => v < 10 || v > 28, gcs: (v) => v < 13 };

export default function EmsView() {
  const { t } = useT();
  const { token, user } = useAuth();
  const router = useRouter();
  const [items, setItems] = useState([]);
  const [err, setErr] = useState("");
  const load = useCallback(async () => {
    if (!token) return;
    try { setItems((await emsApi.board({}, token)).notifications); } catch (e) { setErr(e.message); }
  }, [token]);
  useEffect(() => { load(); const h = setInterval(load, 10000); return () => clearInterval(h); }, [load]);
  async function set(n, st) {
    try { await emsApi.status(n.id, st, token); await load(); } catch (e) { setErr(e.message); }
  }
  const canWrite = user && ["doctor", "admin"].includes(user.role);

  return (
    <div>
      <div className="page-head">
        <h2 className="page-title">{t("ems.title")}</h2>
        <p className="page-sub">{t("ems.sub")}</p>
      </div>
      {err && <div className="err">{err}</div>}
      {items.length === 0 ? <EmptyState>{t("ems.none")}</EmptyState> : (
        <div className="board" data-testid="ems-board">
          {items.map((n) => {
            const v = n.data?.latest_vitals || {};
            return (
              <Card key={n.id} className={`ems-card ${n.triage === "critical" ? "critical" : ""}`}>
                <CardHead title={n.patient?.name || "Unknown"} right={<Badge tone={n.status === "inbound" ? "err" : "info"}>{n.status}</Badge>} />
                <div className="small"><b>{n.chief_complaint || "—"}</b> · {n.triage && <Badge tone={n.triage === "critical" ? "err" : "warn"}>{n.triage}</Badge>}</div>
                <div className="muted small">{t("ems.unit")} {n.unit || "?"} · {t("ems.eta")} {fmtDate(n.eta)} · MRN {n.patient?.mrn || "—"}</div>
                <div className="vital-chips" style={{ margin: "8px 0" }}>
                  {v.sbp !== undefined && <span className={ABN.sbp(v.sbp) ? "abn" : ""}>BP {v.sbp}/{v.dbp ?? "?"}</span>}
                  {["hr", "rr", "spo2", "gcs", "temp", "glucose"].filter((k) => v[k] !== undefined).map((k) => (
                    <span key={k} className={ABN[k] && ABN[k](v[k]) ? "abn" : ""}>{k.toUpperCase()} {v[k]}</span>))}
                </div>
                <pre className="raw" dir="auto">{n.summary}</pre>
                <div className="toolbar">
                  {canWrite && (NEXT[n.status] || []).map((st) => (
                    <Button key={st} onClick={() => set(n, st)} data-action={st}>{t(LABEL[st])}</Button>))}
                  {n.person_id && <Button variant="ghost" onClick={() => router.push(`/ehr/chart?id=${n.person_id}`)}>{t("cl.open")}</Button>}
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
