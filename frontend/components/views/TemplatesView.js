import { useEffect, useState } from "react";

export default function TemplatesView() {
  const [items, setItems] = useState([]);
  const [active, setActive] = useState(null);
  const [body, setBody] = useState("");

  useEffect(() => {
    fetch("/api/templates").then((r) => r.json())
      .then((d) => setItems(d.templates || []))
      .catch(() => {});
  }, []);

  function open(id) {
    setActive(id); setBody("Loading…");
    fetch(`/api/templates/${id}`).then((r) => r.json())
      .then((d) => setBody(d.body || ""))
      .catch((e) => setBody(String(e)));
  }

  return (
    <div className="card" style={{ width: "100%" }}>
      <div className="card-head">
        <span className="step">📑</span>
        <h2>Report templates</h2>
        <span className="muted">{items.length} available</span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 240px) 1fr", gap: 14 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {items.map((t) => (
            <button key={t.id} className={`nav-item ${active === t.id ? "active" : ""}`}
                    onClick={() => open(t.id)} style={{ width: "100%" }}>
              {t.name}
            </button>
          ))}
        </div>
        <div className={`report ${body ? "" : "empty"}`}>
          {body || "Select a template to preview."}
        </div>
      </div>
    </div>
  );
}
