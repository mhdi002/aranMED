import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { Image as ImageIcon, Send, Trash, XRay, Sparkle, Copy } from "../icons";
import CriticalAlertsBanner from "../CriticalAlertsBanner";
import { Button, useToast } from "../ui";

const SUGGESTION_KEYS = ["rad.sg1", "rad.sg2", "rad.sg3", "rad.sg4"];

function newSessionId() {
  const rnd = (typeof crypto !== "undefined" && crypto.randomUUID)
    ? crypto.randomUUID().replace(/-/g, "")
    : Math.random().toString(36).slice(2) + Date.now().toString(36);
  return rnd;
}

function readSid() {
  try {
    let id = localStorage.getItem("asr.radiology.sid");
    if (!id) { id = newSessionId(); localStorage.setItem("asr.radiology.sid", id); }
    return id;
  } catch (_) { return newSessionId(); }
}

export default function RadiologyView() {
  const { t } = useT();
  const { token } = useAuth();
  const toast = useToast();
  const [sessionId, setSessionId] = useState("");
  const [messages, setMessages] = useState([{ role: "assistant", content: "", greeting: true }]);
  const [input, setInput] = useState("");
  const [pendingImages, setPendingImages] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [criticalAlerts, setCriticalAlerts] = useState([]);
  const [health, setHealth] = useState(null);
  const fileRef = useRef(null);
  const endRef = useRef(null);
  // Object URLs shown in sent bubbles stay valid until the view unmounts.
  const urlsRef = useRef([]);

  useEffect(() => () => urlsRef.current.forEach((u) => URL.revokeObjectURL(u)), []);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    apiFetch("/api/health", { timeoutMs: 30000 }, token)
      .then((d) => alive && setHealth(d))
      .catch(() => {});
    return () => { alive = false; };
  }, [token]);

  useEffect(() => { setSessionId(readSid()); }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, busy]);

  function addFiles(files) {
    const imgs = files.filter((f) => f.type.startsWith("image/"));
    if (!imgs.length) return;
    setPendingImages((prev) => [
      ...prev,
      ...imgs.map((f) => {
        const url = URL.createObjectURL(f);
        urlsRef.current.push(url);
        return { file: f, url, name: f.name };
      }),
    ]);
  }

  function pickFiles(e) {
    addFiles([...(e.target.files || [])]);
    if (fileRef.current) fileRef.current.value = "";
  }

  function dropFiles(e) {
    e.preventDefault();
    addFiles([...(e.dataTransfer.files || [])]);
  }

  function removePending(idx) {
    setPendingImages((prev) => prev.filter((_, i) => i !== idx));
  }

  async function send(text) {
    const prompt = (text ?? input).trim();
    if (!prompt && pendingImages.length === 0) return;
    setErr("");

    const sent = pendingImages;
    setMessages((m) => [...m, {
      role: "user",
      content: prompt || t("rad.imageOnly"),
      images: sent.map((p) => ({ url: p.url, name: p.name })),
    }]);
    setInput("");
    setPendingImages([]);
    setBusy(true);

    try {
      const fd = new FormData();
      fd.append("text", prompt || "Analyse the attached medical image.");
      if (sessionId) fd.append("session_id", sessionId);
      sent.forEach((p, i) => fd.append("images", p.file, p.file.name || `image-${i}.png`));
      const d = await apiFetch("/api/chat", { method: "POST", body: fd, timeoutMs: 120000 }, token);
      setCriticalAlerts(d.critical_alerts || []);
      setMessages((m) => [...m, {
        role: "assistant",
        content: d.answer || t("rad.noResponse"),
        model: d.model,
        toolCalls: d.tool_calls,
      }]);
    } catch (e) {
      setErr(e.status === 403 ? t("rad.sessionTaken") : String(e.message || e));
      if (e.status === 403) startNewSession();
    } finally {
      setBusy(false);
    }
  }

  function startNewSession() {
    const id = newSessionId();
    try { localStorage.setItem("asr.radiology.sid", id); } catch (_) { /* private mode */ }
    setSessionId(id);
    return id;
  }

  function newChat() {
    const old = sessionId;
    setMessages([{ role: "assistant", content: t("rad.newSession") }]);
    setPendingImages([]);
    setCriticalAlerts([]);
    setErr("");
    startNewSession();
    if (old) apiFetch(`/api/sessions/${encodeURIComponent(old)}/reset`, { method: "POST" }, token).catch(() => {});
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast.success(t("common.copied")); }
    catch (_) { toast.error(t("common.copyFailed")); }
  }

  return (
    <div className="rad-chat">
      <div className="page-head">
        <h2 className="page-title">
          <span className="rad-badge" aria-hidden="true"><XRay size={18} /></span>
          {t("rad.title")}
        </h2>
        <p className="page-sub">{t("rad.sub")}</p>
      </div>

      <div className="rad-grid">
        <section
          className="card chat-card"
          aria-label={t("rad.conversation")}
          onDragOver={(e) => e.preventDefault()}
          onDrop={dropFiles}
        >
          <div className="chat-head">
            <div className="row">
              <span className="step" aria-hidden="true">VL</span>
              <h2>{t("rad.conversation")}</h2>
              <span className="muted">{t("rad.session")} {sessionId.slice(0, 6) || "…"}</span>
            </div>
            <Button variant="ghost" onClick={newChat} title={t("rad.newChatHint")}>
              <Trash size={14} /> {t("rad.newChat")}
            </Button>
          </div>

          <div className="chat-stream" role="log" aria-live="polite" aria-label={t("rad.conversation")}>
            {messages.map((m, i) => (
              <Bubble key={i} m={m.greeting ? { ...m, content: t("rad.greeting") } : m} onCopy={copy} t={t} />
            ))}
            {busy && (
              <div className="msg assistant">
                <div className="bubble thinking" role="status" aria-label={t("rad.thinking")}>
                  <span className="dot" /><span className="dot" /><span className="dot" />
                </div>
              </div>
            )}
            <div ref={endRef} />
          </div>

          {err && <div className="err" role="alert">{err}</div>}
          <CriticalAlertsBanner alerts={criticalAlerts} />

          {pendingImages.length > 0 && (
            <div className="pending-thumbs">
              {pendingImages.map((p, i) => (
                <div className="thumb" key={p.url}>
                  <img src={p.url} alt={t("rad.attachment", { name: p.name || i + 1 })} />
                  <button className="rm" title={t("common.remove")} aria-label={t("common.remove")}
                          onClick={() => removePending(i)}>×</button>
                </div>
              ))}
            </div>
          )}

          <div className="composer">
            <button type="button" className="upload-btn" title={t("rad.attach")} aria-label={t("rad.attach")}
                    onClick={() => fileRef.current?.click()}>
              <ImageIcon size={16} />
            </button>
            <input ref={fileRef} type="file" accept="image/*" multiple onChange={pickFiles}
                   className="sr-only" tabIndex={-1} aria-label={t("rad.attach")} />
            <textarea
              className="textarea composer-input"
              rows={1}
              dir="auto"
              aria-label={t("rad.placeholder")}
              placeholder={t("rad.placeholder")}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
              }}
              disabled={busy}
            />
            <Button onClick={() => send()} loading={busy}
                    disabled={!input.trim() && pendingImages.length === 0}>
              {!busy && <Send size={14} />}
              {t("rad.send")}
            </Button>
          </div>
        </section>

        <aside className="card side-card">
          <div className="card-head">
            <span className="step" aria-hidden="true">?</span>
            <h2>{t("rad.quick")}</h2>
          </div>
          <div className="suggestions">
            {SUGGESTION_KEYS.map((k) => (
              <button key={k} className="sg" onClick={() => send(t(k))} disabled={busy}>
                <Sparkle size={12} /> {t(k)}
              </button>
            ))}
          </div>

          <hr className="hr" />

          <div className="card-head">
            <span className="step" aria-hidden="true">!</span>
            <h2>{t("rad.tips")}</h2>
          </div>
          <ul className="tips">
            <li>{t("rad.tip1")}</li>
            <li>{t("rad.tip2")}</li>
            <li>{t("rad.tip3")}</li>
            <li>{t("rad.tip4")}</li>
          </ul>

          <hr className="hr" />

          <div className="card-head">
            <span className="step" aria-hidden="true">⚙</span>
            <h2>{t("rad.model")}</h2>
          </div>
          {/* Model identity comes from the live registry (GET /api/health). */}
          <div className="kpis">
            <div className="kpi">
              <span>{t("rad.vision")}</span>
              <b>{health?.vision_model || t("common.notConfigured")}</b>
            </div>
            <div className="kpi">
              <span>{t("rad.knowledge")}</span>
              <b>{health?.medrag?.ok ? t("rad.medragOn") : t("rad.medragOff")}</b>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}

// ── single chat bubble ──
function Bubble({ m, onCopy, t }) {
  const isUser = m.role === "user";
  return (
    <div className={`msg ${isUser ? "user" : "assistant"}`}>
      <div className="bubble">
        {m.images?.length > 0 && (
          <div className="bubble-imgs">
            {m.images.map((img, i) => (
              <img src={img.url} alt={t("rad.attachment", { name: img.name || i + 1 })} key={i} />
            ))}
          </div>
        )}
        {m.content && <div className="bubble-text" dir="auto">{m.content}</div>}
        {!isUser && m.content && (
          <button className="bubble-copy" title={t("common.copy")} aria-label={t("common.copy")}
                  onClick={() => onCopy(m.content)}>
            <Copy size={12} />
          </button>
        )}
        {!isUser && m.toolCalls?.length > 0 && (
          <details className="tool-trace">
            <summary>{t("rad.toolCalls", { n: m.toolCalls.length })}</summary>
            <pre>{JSON.stringify(m.toolCalls, null, 2)}</pre>
          </details>
        )}
      </div>
    </div>
  );
}
