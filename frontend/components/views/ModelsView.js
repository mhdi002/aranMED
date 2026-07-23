import { useEffect, useState } from "react";

export default function ModelsView({ kind = "asr" }) {
  const [health, setHealth] = useState(null);
  useEffect(() => {
    fetch("/api/health").then((r) => r.json()).then(setHealth).catch(() => {});
  }, []);

  const isAsr = kind === "asr";
  return (
    <div className="card" style={{ width: "100%" }}>
      <div className="card-head">
        <span className="step">{isAsr ? "🧠" : "✦"}</span>
        <h2>{isAsr ? "OmniASR-LLM-7B" : "Qwen LLM"}</h2>
        <span className="muted">model status</span>
      </div>

      <div className="kpis" style={{ gridTemplateColumns: "1fr 1fr 1fr" }}>
        <div className="kpi"><span>{isAsr ? "ASR model" : "LLM model"}</span>
          <b>{isAsr ? (health?.asr_model || "facebook/omniASR-LLM-7B") : (health?.ollama_model || "qwen3.5:9b")}</b></div>
        <div className="kpi"><span>Service</span>
          <b style={{ color: (isAsr ? health?.ok : health?.ollama_available) ? "#10b981" : "#ef4444" }}>
            {(isAsr ? health?.ok : health?.ollama_available) ? "Online" : "Offline"}
          </b></div>
        <div className="kpi"><span>Endpoint</span>
          <b>{isAsr ? "/api/transcribe" : "/api/report"}</b></div>
      </div>

      <p className="muted" style={{ marginTop: 14, lineHeight: 1.6 }}>
        {isAsr
          ? "Multilingual code-switching ASR. Loads lazily on the first /api/transcribe call. Persian + English in a single utterance is supported out of the box."
          : "The local Ollama daemon hosts the Qwen draft model that fills radreport.org templates with your dictation. Switch the tag in backend/.env (OLLAMA_MODEL)."}
      </p>

      {!isAsr && health?.ollama_models?.length > 0 && (
        <>
          <label className="lbl">Pulled models</label>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {health.ollama_models.map((m) => (
              <span key={m} className="kpi" style={{ padding: "5px 10px", fontSize: 11 }}>{m}</span>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
