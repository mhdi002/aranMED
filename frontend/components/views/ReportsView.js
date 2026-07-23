import { useEffect, useState } from "react";

export default function ReportsView() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    try {
      const raw = localStorage.getItem("asr.reports");
      setItems(raw ? JSON.parse(raw) : []);
    } catch (_) { setItems([]); }
  }, []);

  function clearAll() {
    if (!confirm("Delete all saved reports?")) return;
    localStorage.removeItem("asr.reports");
    setItems([]);
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
          <button className="btn ghost" onClick={clearAll}>Clear all</button>
        </div>
      )}
    </div>
  );
}
