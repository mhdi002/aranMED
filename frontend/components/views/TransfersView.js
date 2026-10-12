import { useCallback, useState } from "react";
import { useRouter } from "next/router";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { transferApi, fmtDate, TRANSFER_STEPS } from "../../lib/clinical";
import usePolling from "../../lib/usePolling";
import { Badge, Button, Card, CardHead, ConfirmModal, EmptyState, Spinner, Tabs, TextInput, useToast } from "../ui";

// Steps that end the transfer for the other hospital too: ask first.
const CONFIRM = new Set(["reject", "cancel"]);

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
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(null); // {tr, action} awaiting confirmation
  const [busy, setBusy] = useState("");
  const toast = useToast();
  const canWrite = user && ["doctor", "admin"].includes(user.role);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      setItems((await transferApi.list({ direction: dir }, token)).transfers);
      setErr("");
    } catch (e) { setErr(e.message); } finally { setLoading(false); }
  }, [token, dir]);
  usePolling(load, 10000, !!token);

  async function act(tr, action) {
    setErr(""); setBusy(`${tr.id}:${action}`);
    try {
      await transferApi.step(tr.id, action, { note: note[tr.id] || "", reason: note[tr.id] || "" }, token);
      toast.success(t(`tr.${action}`) + " ✓");
      await load();
    } catch (e) { setErr(e.message); } finally { setBusy(""); setPending(null); }
  }
  const request = (tr, action) => (CONFIRM.has(action) ? setPending({ tr, action }) : act(tr, action));

  return (
    <div>
      <div className="page-head">
        <h2 className="page-title">{t("tr.title")}</h2>
        <p className="page-sub">{t("tr.sub")}</p>
      </div>
      <Tabs className="chart-tabs" label={t("tr.title")} value={dir}
            onChange={(d) => { setLoading(true); setDir(d); }} tabAttr="data-dir"
            tabs={["incoming", "outgoing"].map((d) => ({ id: d, label: t(`tr.${d}`) }))}>
      {err && <div className="err" role="alert">{err}</div>}
      {loading && items.length === 0 ? <Spinner /> : items.length === 0 ? <EmptyState>{t("tr.none")}</EmptyState> : (
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
                  <TextInput placeholder={t("tr.notePlaceholder")} aria-label={t("tr.notePlaceholder")}
                             value={note[tr.id] || ""} onChange={(e) => setNote({ ...note, [tr.id]: e.target.value })} />
                  <div className="toolbar">
                    {(ACTIONS[tr.direction][tr.status] || []).map((a) => (
                      <Button key={a} variant={CONFIRM.has(a) ? "danger" : "solid"} onClick={() => request(tr, a)}
                              loading={busy === `${tr.id}:${a}`} disabled={!!busy} data-action={a}>{t(`tr.${a}`)}</Button>
                    ))}
                  </div>
                </>
              )}
              <div className="toolbar"><Button variant="ghost" onClick={() => router.push(`/ehr/chart?id=${tr.person_id}`)}>{t("cl.open")}</Button></div>
              <details className="small"><summary>{t("tr.history")}</summary>
                <ul className="mini-list">{(tr.history || []).map((h, i) => <li key={i}>{fmtDate(h.at)} · {h.status || "note"} · {h.by || ""} {h.note ? `— ${h.note}` : ""}</li>)}</ul>
              </details>
            </Card>
          ))}
        </div>
      )}
      </Tabs>
      <ConfirmModal open={!!pending} danger busy={!!busy}
                    title={pending ? t(`tr.${pending.action}`) : ""}
                    confirmLabel={pending ? t(`tr.${pending.action}`) : ""}
                    onCancel={() => setPending(null)} onConfirm={() => act(pending.tr, pending.action)}>
        <p>{t("tr.confirmText")}</p>
        {pending && <p className="muted small" dir="auto">{pending.tr.patient?.name || pending.tr.person_id}</p>}
      </ConfirmModal>
    </div>
  );
}
