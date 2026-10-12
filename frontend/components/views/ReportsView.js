import { useEffect, useState } from "react";
import { ConfirmModal, EmptyState } from "../ui";
import { useT } from "../../lib/i18n";
import { clearSavedReports, loadSavedReports } from "../../lib/reports";

export default function ReportsView() {
  const { t, lang } = useT();
  const [items, setItems] = useState([]);
  const [confirmClear, setConfirmClear] = useState(false);

  useEffect(() => { setItems(loadSavedReports()); }, []);

  function clearAll() {
    clearSavedReports();
    setItems([]);
    setConfirmClear(false);
  }

  return (
    <div className="card" style={{ width: "100%" }}>
      <div className="card-head">
        <span className="step" aria-hidden="true">📁</span>
        <h2>{t("reports.title")}</h2>
        <span className="muted">{t("reports.sub")}</span>
      </div>

      {items.length === 0 ? (
        <EmptyState>{t("reports.none")}</EmptyState>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {items.map((r, i) => (
            <article key={i} className="kpi" style={{ flexDirection: "column", alignItems: "stretch", gap: 8 }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <b>{r.template_id || "—"}</b>
                <span className="muted">{new Date(r.ts).toLocaleString(lang === "fa" ? "fa-IR" : undefined)}</span>
              </div>
              <pre style={{ margin: 0, fontSize: 12, whiteSpace: "pre-wrap" }} dir="auto">
                {r.report.slice(0, 240)}{r.report.length > 240 ? "…" : ""}
              </pre>
            </article>
          ))}
          <button className="btn ghost" onClick={() => setConfirmClear(true)}>{t("reports.clear")}</button>
        </div>
      )}

      <ConfirmModal
        open={confirmClear}
        title={t("reports.clearTitle")}
        danger
        confirmLabel={t("reports.clearConfirm")}
        onCancel={() => setConfirmClear(false)}
        onConfirm={clearAll}
      >
        {t("reports.clearText", { n: items.length })}
      </ConfirmModal>
    </div>
  );
}
