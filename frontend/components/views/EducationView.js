import { useEffect, useState } from "react";
import { useAuth } from "../../lib/auth";
import { apiFetch } from "../../lib/api";
import { useT } from "../../lib/i18n";

function McqRunner({ questions }) {
  const { t } = useT();
  const [i, setI] = useState(0);
  const [picked, setPicked] = useState(null);
  const [shown, setShown] = useState(false);
  const [score, setScore] = useState(0);

  if (!questions?.length) return null;
  const q = questions[i];
  const options = q.options || q.choices || [];
  const correct = q.answer_index;

  function reveal() {
    if (picked === correct) setScore((s) => s + 1);
    setShown(true);
  }
  function next() {
    setPicked(null); setShown(false);
    setI((x) => (x + 1) % questions.length);
  }

  return (
    <div className="mcq">
      <div className="mcq-head">
        <div className="muted small">
          {t("edu.mcq")} · {i + 1}/{questions.length}
        </div>
        <div className="muted small">{t("edu.score")}: {score}</div>
      </div>
      <h3>{q.stem}</h3>
      <ul className="mcq-options">
        {options.map((o, k) => (
          <li key={k}
              className={`opt ${picked === k ? "picked" : ""} ${
                shown && k === correct ? "correct" : ""
              } ${shown && picked === k && k !== correct ? "wrong" : ""}`}
              onClick={() => !shown && setPicked(k)}>
            <b>{String.fromCharCode(65 + k)}.</b> {o}
          </li>
        ))}
      </ul>
      {!shown
        ? <button className="btn" disabled={picked == null} onClick={reveal}>
            {t("edu.show")}</button>
        : <>
            <div className="muted" style={{ marginTop: 8 }}>{q.explanation}</div>
            <button className="btn ghost" style={{ marginTop: 8 }} onClick={next}>
              {t("edu.next")}
            </button>
          </>}
    </div>
  );
}

function CasePanel({ data }) {
  if (!data) return null;
  return (
    <div className="case-panel">
      <h3>{data.title}</h3>
      <p><b>Presentation</b> — {data.presentation}</p>
      <p><b>History</b> — {data.history}</p>
      <p><b>Exam</b> — {data.exam}</p>
      <p><b>Investigations</b> — {data.investigations}</p>
      <ul>{(data.questions || []).map((q, i) => (
        <li key={i}>
          <details>
            <summary>{q.q}</summary>
            <div className="muted">{q.answer}</div>
          </details>
        </li>
      ))}</ul>
      {data.teaching_points?.length > 0 && <>
        <b>Teaching points</b>
        <ul>{data.teaching_points.map((p, i) => <li key={i}>{p}</li>)}</ul>
      </>}
    </div>
  );
}

export default function EducationView() {
  const { t, lang } = useT();
  const { token } = useAuth();
  const [topic, setTopic] = useState("pulmonary embolism");
  const [kind, setKind] = useState("mcq");
  const [count, setCount] = useState(5);
  const [difficulty, setDifficulty] = useState("resident");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState(null);
  const [saved, setSaved] = useState([]);

  useEffect(() => { loadSaved(); }, [token]);

  async function loadSaved() {
    if (!token) return;
    try {
      const d = await apiFetch("/api/education/saved", {}, token);
      setSaved(d.items || []);
    } catch (_) {}
  }

  async function run() {
    setBusy(true); setErr(""); setResult(null);
    try {
      let url = "/api/education/mcq";
      if (kind === "case") url = "/api/education/case";
      if (kind === "exam") url = "/api/education/exam";
      if (kind === "explain") url = "/api/education/explain";

      const body = kind === "explain"
        ? { concept: topic, language: lang, level: difficulty }
        : { topic, count, difficulty, language: lang, save: true };

      const d = await apiFetch(url, {
        method: "POST", body: JSON.stringify(body),
      }, token);
      setResult({ kind, data: d });
      loadSaved();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  async function openSaved(id) {
    try {
      const d = await apiFetch(`/api/education/saved/${id}`, {}, token);
      setResult({ kind: d.kind, data: { data: d.data } });
    } catch (e) { setErr(e.message); }
  }

  return (
    <div>
      <div className="page-head">
        <h2 className="page-title">{t("edu.title")}</h2>
        <p className="page-sub">{t("edu.sub")}</p>
      </div>

      <div className="grid">
        <section className="card">
          <div className="card-head">
            <span className="step">1</span><h2>{t("edu.kind")}</h2>
          </div>
          <div className="seg">
            {[
              ["mcq", t("edu.mcq")],
              ["case", t("edu.case")],
              ["exam", t("edu.exam")],
              ["explain", t("edu.explain")],
            ].map(([k, label]) => (
              <button key={k} className={`seg-btn ${kind === k ? "active" : ""}`}
                      onClick={() => setKind(k)}>{label}</button>
            ))}
          </div>

          <label className="lbl">{t("edu.topic")}</label>
          <input className="input" value={topic} dir="auto"
                 onChange={(e) => setTopic(e.target.value)} />

          {kind !== "explain" && (
            <>
              <label className="lbl">{t("edu.count")}</label>
              <input className="input" type="number" min={1} max={15}
                     value={count}
                     onChange={(e) => setCount(parseInt(e.target.value || "1", 10))} />
            </>
          )}

          <label className="lbl">{t("edu.difficulty")}</label>
          <select className="select" value={difficulty}
                  onChange={(e) => setDifficulty(e.target.value)}>
            <option value="student">student</option>
            <option value="resident">resident</option>
            <option value="board">board</option>
          </select>

          <div className="toolbar">
            <button className="btn primary" onClick={run} disabled={busy || !token}>
              {busy ? <span className="spinner" /> : null} {t("edu.run")}
            </button>
          </div>
          {err && <div className="err">{err}</div>}
        </section>

        <section className="card">
          <div className="card-head">
            <span className="step">2</span>
            <h2>{t("edu.saved")}</h2>
            <span className="muted">{saved.length}</span>
          </div>
          <ul className="list">
            {saved.map((s) => (
              <li key={s.id} className="list-row">
                <div>
                  <div className="bold">{s.topic}</div>
                  <div className="muted small">
                    {s.kind} · {s.language} ·
                    {" "}{new Date(s.created_at * 1000).toLocaleDateString()}
                  </div>
                </div>
                <button className="btn ghost" onClick={() => openSaved(s.id)}>
                  {t("ehr.open")}
                </button>
              </li>
            ))}
            {saved.length === 0 && <div className="muted">—</div>}
          </ul>
        </section>
      </div>

      {result && (
        <section className="card" style={{ marginTop: 16 }}>
          <div className="card-head">
            <span className="step">3</span>
            <h2>{result.kind}</h2>
          </div>
          {result.kind === "mcq" && (
            <McqRunner questions={result.data.data?.questions} />
          )}
          {result.kind === "exam" && (
            <>
              <McqRunner questions={result.data.data?.mcqs} />
              <CasePanel data={result.data.data?.case} />
            </>
          )}
          {result.kind === "case" && (
            <CasePanel data={result.data.data?.case || result.data.data} />
          )}
          {result.kind === "explain" && (
            <pre className="report">{result.data.content
              || result.data.data?.explanation}</pre>
          )}
        </section>
      )}
    </div>
  );
}