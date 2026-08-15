import { useEffect, useState } from "react";
import { ConfirmModal } from "../ui";

export default function ReportsView() {
  const [items, setItems] = useState([]);
  const [confirmClear, setConfirmClear] = useState(false);

  useEffect(() => {
    try {
      const raw = localStorage.getItem("asr.reports");
      setItems(raw ? JSON.parse(raw) : []);
    } catch (_) { setItems([]); }
  }, []);

  function clearAll() {
    localStorage.removeItem("asr.reports");
    setItems([]);
    setConfirmClear(false);
  }

  return (
    <div className="card" style={{ width: "100%" }}>
      <div className="card-head">
        <span className="step">📁</span>
        <h2>Saved reports</h2>
        <span className="muted">stored locally in your browser</span>
      </div>

      {items.length === 0 ? (
        <div className="report empty">No reports saved yet. Generate one in <b>Dictate</b> and click <b>Save</b>.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {items.map((r, i) => (
            <div key={i} className="kpi" style={{ flexDirection: "column", alignItems: "stretch", gap: 8 }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <b>{r.template_id}</b>
                <span className="muted">{new Date(r.ts).toLocaleString()}</span>
              </div>
              <pre style={{ margin: 0, fontSize: 12, whiteSpace: "pre-wrap" }}>
                {r.report.slice(0, 240)}{r.report.length > 240 ? "…" : ""}
              </pre>
            </div>
          ))}
          <button className="btn ghost" onClick={() => setConfirmClear(true)}>Clear all</button>
        </div>
      )}

      <ConfirmModal
        open={confirmClear}
        title="Delete all saved reports?"
        danger
        confirmLabel="Delete all"
        onCancel={() => setConfirmClear(false)}
        onConfirm={clearAll}
      >
        This removes all {items.length} report(s) saved locally in this browser. This cannot be undone.
      </ConfirmModal>
    </div>
  );
}
