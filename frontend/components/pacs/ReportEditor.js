// Report list + editor for a study (draft -> preliminary -> final -> amended).
import { useEffect, useState } from "react";
import { useT } from "../../lib/i18n";
import { pacsApi, STATUS_TONE } from "../../lib/pacs";
import { Badge, Button, TextArea } from "../ui";

const CAN_SIGN = new Set(["radiologist", "doctor", "admin"]);

export default function ReportEditor({ studyUid, reports, token, user, onSaved }) {
  const { t } = useT();
  const latest = reports?.[0];
  const [text, setText] = useState(latest?.text || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => { setText(reports?.[0]?.text || ""); }, [reports]);

  const isFinal = latest && (latest.status === "final" || latest.status === "amended");
  const canWrite = user && user.role !== "student";
  const canSign = user && CAN_SIGN.has(user.role);

  async function save(status) {
    setBusy(true); setErr("");
    try {
      await pacsApi.saveReport(studyUid, { text, status, report_id: latest?.id }, token);
      onSaved?.();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  return (
    <div className="report-editor" data-testid="report-editor">
      {latest ? (
        <div className="report-meta">
          <Badge tone={STATUS_TONE[latest.status] || "muted"}>{latest.status}</Badge>
          <span className="muted small"> {latest.source} · {latest.author || "—"} · {new Date(latest.updated_at * 1000).toLocaleString()}</span>
        </div>
      ) : <div className="muted small">{t("pacs.noReport")}</div>}
      <TextArea value={text} onChange={(e) => setText(e.target.value)} dir="auto"
                aria-label={t("pacs.reportText")} style={{ minHeight: 160 }} readOnly={!canWrite} />
      {canWrite && (
        <div className="toolbar">
          {!isFinal && <Button variant="ghost" loading={busy} disabled={!text.trim()} onClick={() => save("draft")}>{t("pacs.saveDraft")}</Button>}
          {!isFinal && <Button variant="ghost" loading={busy} disabled={!text.trim()} onClick={() => save("preliminary")}>{t("pacs.savePrelim")}</Button>}
          {canSign && (
            <Button loading={busy} disabled={!text.trim()} onClick={() => save(isFinal ? "amended" : "final")}
                    data-testid="sign-report">
              {isFinal ? t("pacs.amend") : t("pacs.sign")}
            </Button>
          )}
        </div>
      )}
      {err && <div className="err">{err}</div>}
      {reports?.length > 1 && (
        <details className="small"><summary>{reports.length - 1} earlier version(s)</summary>
          {reports.slice(1).map((r) => (
            <div key={r.id} className="report-old">
              <Badge tone={STATUS_TONE[r.status] || "muted"}>{r.status}</Badge> {r.author}
              <pre dir="auto">{r.text}</pre>
            </div>
          ))}
        </details>
      )}
    </div>
  );
}
