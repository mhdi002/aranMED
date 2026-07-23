import { useEffect, useRef, useState } from "react";
import { fetchWithTimeout } from "../../lib/api";
import { apiUrl } from "../../lib/config";
import { Image, Send, Upload, Trash, XRay, Sparkle, Copy } from "../icons";
import CriticalAlertsBanner from "../CriticalAlertsBanner";

const SUGGESTIONS = [
  "Briefly describe the findings on this chest X-ray.",
  "Is there evidence of consolidation or pleural effusion?",
  "Compare the cardiac silhouette and mediastinal width to normal.",
  "List any acute findings that require urgent attention.",
];

export default function RadiologyView() {
  const [sessionId, setSessionId] = useState("");
  const [messages, setMessages] = useState([
    {
      role: "assistant",
        content:
          "Hello — I'm the aranmed radiology assistant. Ask a knowledge question " +
          "(MedicalRAG) or upload an image for local vision analysis.",
    },
  ]);
  const [input, setInput] = useState("");
  const [pendingImages, setPendingImages] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [criticalAlerts, setCriticalAlerts] = useState([]);
  const fileRef = useRef(null);
  const endRef = useRef(null);

  useEffect(() => {
    let id = localStorage.getItem("asr.radiology.sid");
    if (!id) {
      id = Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem("asr.radiology.sid", id);
    }
    setSessionId(id);
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, busy]);

  function pickFiles(e) {
    const files = [...(e.target.files || [])];
    if (!files.length) return;
    setPendingImages((prev) => [
      ...prev,
      ...files.map((f) => ({ file: f, url: URL.createObjectURL(f) })),
    ]);
    if (fileRef.current) fileRef.current.value = "";
  }

  function dropFiles(e) {
    e.preventDefault();
    const files = [...(e.dataTransfer.files || [])].filter((f) =>
      f.type.startsWith("image/")
    );
    if (files.length) {
      setPendingImages((prev) => [
        ...prev,
        ...files.map((f) => ({ file: f, url: URL.createObjectURL(f) })),
      ]);
    }
  }

  function removePending(idx) {
    setPendingImages((prev) => {
      const next = [...prev];
      const [gone] = next.splice(idx, 1);
      if (gone) URL.revokeObjectURL(gone.url);
      return next;
    });
  }

  async function send(text) {
    const prompt = (text ?? input).trim();
    if (!prompt && pendingImages.length === 0) return;
    setErr("");

    const userMsg = {
      role: "user",
      content: prompt || "(image only)",
      images: pendingImages.map((p) => p.url),
    };
    setMessages((m) => [...m, userMsg]);
    setInput("");
    setBusy(true);

    try {
      const fd = new FormData();
      fd.append("text", prompt || "Analyse the attached medical image.");
      if (sessionId) fd.append("session_id", sessionId);
      pendingImages.forEach((p, i) =>
        fd.append("images", p.file, p.file.name || `image-${i}.png`)
      );

      const r = await fetchWithTimeout(apiUrl("/api/chat"), { method: "POST", body: fd }, 120000);
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();

      pendingImages.forEach((p) => URL.revokeObjectURL(p.url));
      setPendingImages([]);
      setCriticalAlerts(d.critical_alerts || []);

      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: d.answer || "(no response)",
          model: d.model,
          toolCalls: d.tool_calls,
          criticalAlerts: d.critical_alerts || [],
        },
      ]);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  function newChat() {
    setMessages([
      {
        role: "assistant",
        content:
          "New session started. Upload an image and ask me about it.",
      },
    ]);
    pendingImages.forEach((p) => URL.revokeObjectURL(p.url));
    setPendingImages([]);
    const id = Math.random().toString(36).slice(2) + Date.now().toString(36);
    localStorage.setItem("asr.radiology.sid", id);
    setSessionId(id);
    fetch(apiUrl(`/api/sessions/${sessionId}/reset`), { method: "POST" }).catch(() => {});
  }

  return (
    <div className="rad-chat">
      <div className="page-head">
        <h2 className="page-title">
          <span className="rad-badge">
            <XRay size={18} />
          </span>
          Radiology Knowledge & Vision
        </h2>
        <p className="page-sub">
          Text questions go to <b>MedicalRAG</b> (specialty=radiology).
          Attached images use the local vision provider when configured —
          never share PHI.
        </p>
      </div>

      <div className="rad-grid">
        <section
          className="card chat-card"
          onDragOver={(e) => e.preventDefault()}
          onDrop={dropFiles}
        >
          <div className="chat-head">
            <div className="row">
              <span className="step">VL</span>
              <h2>Conversation</h2>
              <span className="muted">
                session {sessionId.slice(0, 6) || "…"}
              </span>
            </div>
            <button
              className="btn ghost"
              onClick={newChat}
              title="Start a new session"
            >
              <Trash size={14} /> New chat
            </button>
          </div>

          <div className="chat-stream">
            {messages.map((m, i) => (
              <Bubble key={i} m={m} />
            ))}
            {busy && (
              <div className="msg assistant">
                <div className="bubble thinking">
                  <span className="dot" />
                  <span className="dot" />
                  <span className="dot" />
                </div>
              </div>
            )}
            <div ref={endRef} />
          </div>

          {err && <div className="err">{err}</div>}
          <CriticalAlertsBanner alerts={criticalAlerts} />

          {pendingImages.length > 0 && (
            <div className="pending-thumbs">
              {pendingImages.map((p, i) => (
                <div className="thumb" key={i}>
                  <img src={p.url} alt="upload preview" />
                  <button
                    className="rm"
                    title="Remove"
                    onClick={() => removePending(i)}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          <div className="composer">
            <label className="upload-btn" title="Attach images">
              <Image size={16} />
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                multiple
                onChange={pickFiles}
                style={{ display: "none" }}
              />
            </label>
            <textarea
              className="textarea composer-input"
              rows={1}
              dir="auto"
              placeholder="Ask about the attached image(s) — Persian or English…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              disabled={busy}
            />
            <button
              className="btn"
              onClick={() => send()}
              disabled={busy || (!input.trim() && pendingImages.length === 0)}
            >
              {busy ? <span className="spinner" /> : <Send size={14} />}
              Send
            </button>
          </div>
        </section>

        <aside className="card side-card">
          <div className="card-head">
            <span className="step">?</span>
            <h2>Quick prompts</h2>
          </div>
          <div className="suggestions">
            {SUGGESTIONS.map((s, i) => (
              <button
                key={i}
                className="sg"
                onClick={() => send(s)}
                disabled={busy}
              >
                <Sparkle size={12} /> {s}
              </button>
            ))}
          </div>

          <hr className="hr" />

          <div className="card-head">
            <span className="step">!</span>
            <h2>Tips</h2>
          </div>
          <ul className="tips">
            <li>Upload one modality at a time for best focus.</li>
            <li>Ask <b>specific</b> questions ("describe the costophrenic angles") for sharper answers.</li>
            <li>The model is a <b>draft assistant</b> — never use it as a sole diagnostic source.</li>
            <li>Drag &amp; drop images directly into the chat panel.</li>
          </ul>

          <hr className="hr" />

          <div className="card-head">
            <span className="step">⚙</span>
            <h2>Model</h2>
          </div>
          <div className="kpis">
            <div className="kpi">
              <span>Vision</span>
              <b>Radiology-Infer-Mini Q8</b>
            </div>
            <div className="kpi">
              <span>Backend</span>
              <b>llama-server :8088</b>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}

// ── single chat bubble ──
function Bubble({ m }) {
  const isUser = m.role === "user";
  return (
    <div className={`msg ${isUser ? "user" : "assistant"}`}>
      <div className="bubble">
        {m.images?.length > 0 && (
          <div className="bubble-imgs">
            {m.images.map((u, i) => (
              <img src={u} alt="attached" key={i} />
            ))}
          </div>
        )}
        {m.content && <div className="bubble-text" dir="auto">{m.content}</div>}
        {!isUser && m.content && (
          <button
            className="bubble-copy"
            title="Copy"
            onClick={() => navigator.clipboard?.writeText(m.content)}
          >
            <Copy size={12} />
          </button>
        )}
        {!isUser && m.toolCalls?.length > 0 && (
          <details className="tool-trace">
            <summary>{m.toolCalls.length} tool call(s)</summary>
            <pre>{JSON.stringify(m.toolCalls, null, 2)}</pre>
          </details>
        )}
      </div>
    </div>
  );
}