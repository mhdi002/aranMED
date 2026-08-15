import { useEffect, useState } from "react";
import { fetchWithTimeout } from "../../lib/api";
import { apiUrl } from "../../lib/config";
import { Badge, Card, CardHead, EmptyState } from "../ui";

/**
 * Model status view — 100% driven by the live backend registry
 * (`GET /api/health` + `GET /api/models`). No model name, provider name,
 * endpoint or port is hardcoded here: whatever `backend/models.yaml`
 * declares is what renders. See docs/core/CONFIGURATION.md.
 */
const ROLE_META = {
  asr: { role: "asr", healthKey: "asr_model", endpoint: "/api/transcribe" },
  llm: { role: "core", healthKey: "core_model", endpoint: "/api/report" },
  vision: { role: "vision", healthKey: "vision_model", endpoint: "/api/vision" },
};

export default function ModelsView({ kind = "asr" }) {
  const [health, setHealth] = useState(null);
  const [registry, setRegistry] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    fetchWithTimeout(apiUrl("/api/health"), {}, 30000)
      .then((r) => r.json())
      .then((d) => alive && setHealth(d))
      .catch((e) => alive && setErr(String(e.message || e)));
    fetchWithTimeout(apiUrl("/api/models"), {}, 30000)
      .then((r) => r.json())
      .then((d) => alive && setRegistry(d))
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  const meta = ROLE_META[kind] || ROLE_META.asr;
  const providers = registry?.providers || {};
  // The registry is the source of truth for which model serves this role.
  const defaultName = registry?.defaults?.[meta.role] || null;
  const entries = Object.entries(providers).filter(([, p]) => p?.role === meta.role);
  const activeName = defaultName || health?.[meta.healthKey] || null;
  const active = activeName ? providers[activeName] : null;
  const online = active ? active?.health?.ok !== false : Boolean(health?.ok);

  return (
    <Card style={{ width: "100%" }}>
      <CardHead
        title={activeName || "—"}
        hint={`${meta.role} role · model status`}
      />

      {err && <div className="err">{err}</div>}

      <div className="kpis" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))" }}>
        <div className="kpi">
          <span>Model</span>
          <b>{activeName || "not configured"}</b>
        </div>
        <div className="kpi">
          <span>Provider</span>
          <b>{active?.kind || "—"}</b>
        </div>
        <div className="kpi">
          <span>Status</span>
          <b style={{ color: online ? "var(--ok)" : "var(--danger)" }}>
            {online ? "Online" : "Offline"}
          </b>
        </div>
        <div className="kpi">
          <span>Endpoint</span>
          <b>{meta.endpoint}</b>
        </div>
      </div>

      {registry?.vram_used_gb != null && (
        <div className="kpis" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))" }}>
          <div className="kpi"><span>VRAM in use</span><b>{registry.vram_used_gb} GB</b></div>
          <div className="kpi"><span>Warm models</span><b>{(registry.warm || []).length}</b></div>
        </div>
      )}

      <hr className="hr" />

      <label className="lbl">Registered for this role</label>
      {entries.length === 0 ? (
        <EmptyState>
          No provider declares the <code>{meta.role}</code> role in the model registry.
        </EmptyState>
      ) : (
        <div className="list">
          {entries.map(([name, p]) => (
            <div key={name} className="list-item" style={{ cursor: "default" }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <b>{name}</b>
                <div className="row">
                  {name === defaultName && <Badge tone="info">default</Badge>}
                  {(p?.loaded || (registry?.warm || []).includes(name)) && (
                    <Badge tone="ok">warm</Badge>
                  )}
                  <Badge tone={p?.health?.ok === false ? "due" : "ok"}>
                    {p?.health?.ok === false ? "error" : "ready"}
                  </Badge>
                </div>
              </div>
              <div className="meta">
                provider {p?.kind || "—"}
                {p?.health?.detail ? ` · ${p.health.detail}` : ""}
              </div>
            </div>
          ))}
        </div>
      )}

      {kind === "llm" && (health?.ollama_models || []).length > 0 && (
        <>
          <hr className="hr" />
          <label className="lbl">Models pulled on the LLM host</label>
          <div className="row">
            {health.ollama_models.map((m) => (
              <Badge key={m} tone="muted">{m}</Badge>
            ))}
          </div>
        </>
      )}

      <p className="muted" style={{ marginTop: "var(--space-4)", lineHeight: "var(--leading-normal)" }}>
        Models, providers and defaults come from the backend model registry
        (<code>backend/models.yaml</code>, path via <code>ASR_AGENT_REGISTRY_YAML</code>).
        Nothing on this screen is hardcoded — change the registry or the
        corresponding environment variable and reload.
      </p>
    </Card>
  );
}
