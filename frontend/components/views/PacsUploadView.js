import { useRef, useState } from "react";
import { useRouter } from "next/router";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { uploadDicom, fmtBytes, fmtDicomDate } from "../../lib/pacs";
import { Button, Card, CardHead } from "../ui";
import { Upload } from "../icons";

export default function PacsUploadView() {
  const { t } = useT();
  const { token } = useAuth();
  const router = useRouter();
  const input = useRef(null);
  const [files, setFiles] = useState([]);
  const [drag, setDrag] = useState(false);
  const [progress, setProgress] = useState(0);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [err, setErr] = useState("");

  const add = (list) => setFiles((f) => [...f, ...Array.from(list || [])]);
  const total = files.reduce((n, f) => n + f.size, 0);

  async function start() {
    setBusy(true); setErr(""); setResult(null); setProgress(0);
    try {
      setResult(await uploadDicom(files, token, setProgress));
      setFiles([]);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  return (
    <div>
      <div className="page-head">
        <h2 className="page-title">{t("up.title")}</h2>
        <p className="page-sub">{t("up.sub")}</p>
      </div>
      <Card>
        <div className={`dropzone ${drag ? "drag" : ""}`} role="button" tabIndex={0}
             onClick={() => input.current?.click()}
             onKeyDown={(e) => { if (e.key === "Enter") input.current?.click(); }}
             onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
             onDragLeave={() => setDrag(false)}
             onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }}>
          <Upload size={28} />
          <div>{t("up.drop")}</div>
          <input ref={input} type="file" multiple hidden data-testid="upload-input"
                 accept=".dcm,.zip,application/dicom,application/zip,*/*"
                 onChange={(e) => add(e.target.files)} />
        </div>
        {files.length > 0 && (
          <div className="upload-list">
            <div className="muted small">{files.length} file(s) · {fmtBytes(total)}</div>
            <ul className="mini-list">{files.slice(0, 12).map((f, i) => <li key={i}>{f.name} <span className="muted">{fmtBytes(f.size)}</span></li>)}</ul>
            {files.length > 12 && <div className="muted small">… +{files.length - 12}</div>}
          </div>
        )}
        {busy && <div className="progress"><div style={{ width: `${Math.round(progress * 100)}%` }} /></div>}
        <div className="toolbar">
          <Button onClick={start} loading={busy} disabled={!files.length} data-testid="upload-start">{t("up.start")}</Button>
          {files.length > 0 && <Button variant="ghost" onClick={() => setFiles([])}>{t("pacs.reset")}</Button>}
        </div>
        {err && <div className="err">{err}</div>}
      </Card>
      {result && (
        <Card data-testid="upload-result">
          <CardHead title={`${result.stored} ${t("up.stored")} · ${result.duplicates} ${t("up.dups")} · ${result.failed.length} ${t("up.failed")}`} />
          <ul className="mini-list">
            {result.studies.map((s) => s && (
              <li key={s.StudyInstanceUID}>
                <b dir="auto">{(s.PatientName || "—").replace(/\^/g, " ")}</b> · {fmtDicomDate(s.StudyDate)} · <span dir="auto">{s.StudyDescription || "—"}</span>
                {" · "}{s.NumberOfStudyRelatedInstances} img{" "}
                <Button variant="ghost" onClick={() => router.push(`/pacs/viewer?study=${s.StudyInstanceUID}`)}>{t("pacs.open")}</Button>
              </li>
            ))}
          </ul>
          {result.failed.length > 0 && (
            <details><summary className="small">{t("up.failed")}</summary>
              <ul className="mini-list">{result.failed.map((f, i) => <li key={i}>{f.file}: <span className="muted">{f.error}</span></li>)}</ul>
            </details>
          )}
        </Card>
      )}
    </div>
  );
}
