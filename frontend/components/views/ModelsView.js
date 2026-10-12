import { useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
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
  const { t } = useT();
  const { token } = useAuth();
  const [health, setHealth] = useState(null);
  const [registry, setRegistry] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    apiFetch("/api/health", { timeoutMs: 30000 }, token)
      .then((d) => alive && setHealth(d))
      .catch((e) => alive && setErr(String(e.message || e)));
    apiFetch("/api/models", { timeoutMs: 30000 }, token)
      .then((d) => alive && setRegistry(d))
      .catch((e) => alive && setErr((prev) => prev || String(e.message || e)));
    return () => { alive = false; };
  }, [token]);

  const meta = ROLE_META[kind] || ROLE_META.asr;
  const providers = registry?.providers || {};
  // The registry is the source of truth for which model serves this role.
  const defaultName = registry?.defaults?.[meta.role] || null;
  const entries = Object.entries(providers).filter(([, p]) => p?.role === meta.role);
  const activeName = defaultName || health?.[meta.healthKey] || null;
  const active = activeName ? providers[activeName] : null;
  const online = active ? active?.health?.ok !== false : Boolean(health?.ok);
  // `activeName` is the registry *slot key* (e.g. "ollama-core") -- a label,
  // not necessarily the model it resolves to (config.model can be templated
  // from an env var). Prefer what the provider actually reports/resolved to
  // at runtime, so a stale or generic slot label can never misrepresent
  // which model is really running. See docs/core/CONFIGURATION.md.
  const resolvedModel =
    active?.health?.model || active?.config?.model || activeName;

  return (
    <Card style={{ width: "100%" }}>
      <CardHead
        title={resolvedModel || "—"}
        hint={t("models.hint", { role: meta.role })}
      />

      {err && <div className="err" role="alert">{err}</div>}

      <div className="kpis" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))" }}>
        <div className="kpi">
          <span>{t("models.model")}</span>
          <b>{resolvedModel || t("common.notConfigured")}</b>
        </div>
        <div className="kpi">
          <span>{t("models.slot")}</span>
          <b>{activeName || "—"}</b>
        </div>
        <div className="kpi">
          <span>{t("models.provider")}</span>
          <b>{active?.kind || "—"}</b>
        </div>
        <div className="kpi">
          <span>{t("models.status")}</span>
          <b style={{ color: online ? "var(--ok)" : "var(--danger)" }}>
            {online ? t("models.online") : t("models.offline")}
          </b>
        </div>
        <div className="kpi">
          <span>{t("models.endpoint")}</span>
          <b>{meta.endpoint}</b>
        </div>
      </div>

      {registry?.vram_used_gb != null && (
        <div className="kpis" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))" }}>
          <div className="kpi"><span>{t("models.vram")}</span><b>{registry.vram_used_gb} GB</b></div>
          <div className="kpi"><span>{t("models.warm")}</span><b>{(registry.warm || []).length}</b></div>
        </div>
      )}

      <hr className="hr" />

      <div className="lbl">{t("models.registered")}</div>
      {entries.length === 0 ? (
        <EmptyState>
          {t("models.noneForRole", { role: meta.role })}
        </EmptyState>
      ) : (
        <div className="list">
          {entries.map(([name, p]) => {
            const entryModel = p?.health?.model || p?.config?.model || name;
            return (
              <div key={name} className="list-item" style={{ cursor: "default" }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <b>{entryModel}</b>
                  <div className="row">
                    {name === defaultName && <Badge tone="info">{t("models.default")}</Badge>}
                    {(p?.loaded || (registry?.warm || []).includes(name)) && (
                      <Badge tone="ok">{t("models.warmBadge")}</Badge>
                    )}
                    <Badge tone={p?.health?.ok === false ? "due" : "ok"}>
                      {p?.health?.ok === false ? t("models.error") : t("models.ready")}
                    </Badge>
                  </div>
                </div>
                <div className="meta">
                  {t("models.slotProvider", { slot: name, provider: p?.kind || "—" })}
                  {p?.health?.detail ? ` · ${p.health.detail}` : ""}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {kind === "llm" && (health?.ollama_models || []).length > 0 && (
        <>
          <hr className="hr" />
          <div className="lbl">{t("models.pulled")}</div>
          <div className="row">
            {health.ollama_models.map((m) => (
              <Badge key={m} tone="muted">{m}</Badge>
            ))}
          </div>
        </>
      )}

      <p className="muted" style={{ marginTop: "var(--space-4)", lineHeight: "var(--leading-normal)" }}>
        {t("models.note")} <code>backend/models.yaml</code> (<code>ASR_AGENT_REGISTRY_YAML</code>).
      </p>
    </Card>
  );
}
