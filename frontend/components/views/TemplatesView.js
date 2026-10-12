import { useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { EmptyState, Spinner } from "../ui";

export default function TemplatesView() {
  const { t } = useT();
  const { token } = useAuth();
  const [items, setItems] = useState(null);
  const [active, setActive] = useState(null);
  const [body, setBody] = useState("");
  const [loadingBody, setLoadingBody] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    apiFetch("/api/templates", { timeoutMs: 30000 }, token)
      .then((d) => setItems(d.templates || []))
      .catch((e) => { setItems([]); setErr(e.message); });
  }, [token]);

  async function open(id) {
    setActive(id); setLoadingBody(true); setErr("");
    try {
      const d = await apiFetch(`/api/templates/${encodeURIComponent(id)}`, { timeoutMs: 30000 }, token);
      setBody(d.body || "");
    } catch (e) { setBody(""); setErr(e.message); } finally { setLoadingBody(false); }
  }

  return (
    <div className="card" style={{ width: "100%" }}>
      <div className="card-head">
        <span className="step" aria-hidden="true">📑</span>
        <h2>{t("templates.title")}</h2>
        <span className="muted">{items ? t("templates.count", { n: items.length }) : t("common.loading")}</span>
      </div>
      {err && <div className="err" role="alert">{err}</div>}

      {items === null ? <Spinner /> : items.length === 0 ? <EmptyState>{t("templates.none")}</EmptyState> : (
        <div className="tpl-grid" style={{ display: "grid", gridTemplateColumns: "minmax(0, 240px) 1fr", gap: 14 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }} role="list" aria-label={t("templates.title")}>
            {items.map((tpl) => (
              <button key={tpl.id} role="listitem" className={`nav-item ${active === tpl.id ? "active" : ""}`}
                      aria-current={active === tpl.id ? "true" : undefined}
                      onClick={() => open(tpl.id)} style={{ width: "100%" }}>
                {tpl.name}
              </button>
            ))}
          </div>
          <div className={`report ${body ? "" : "empty"}`} aria-live="polite" aria-busy={loadingBody}>
            {loadingBody ? <Spinner /> : (body || t("templates.pick"))}
          </div>
        </div>
      )}
    </div>
  );
}
