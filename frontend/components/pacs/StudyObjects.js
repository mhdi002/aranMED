import { useState } from "react";
import { apiUrl } from "../../lib/config";
import { DICOMWEB } from "../../lib/pacs";
import { Badge, Button, Modal } from "../ui";

const LABEL = { sr: "Structured report", pdf: "PDF document", cda: "CDA document", stl: "3D model (STL)",
                obj: "3D model (OBJ)", video: "Video", waveform: "Waveform (ECG)", rt: "Radiotherapy object",
                other: "Object" };

/** Non-image objects of a study: SR as text, PDF/video in a viewer, others downloadable. */
export default function StudyObjects({ studyUid, objects, token, t }) {
  const [open, setOpen] = useState(null);
  const [err, setErr] = useState("");
  if (!objects?.length) return null;
  const base = (o) => apiUrl(`${DICOMWEB}/studies/${studyUid}/series/${o.SeriesInstanceUID}/instances/${o.SOPInstanceUID}`);

  async function show(o) {
    setErr("");
    try {
      if (o.kind === "sr") {
        const r = await fetch(`${base(o)}/sr`, { headers: { Authorization: `Bearer ${token}` } });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        setOpen({ o, sr: await r.json() });
        return;
      }
      const tail = o.kind === "video" ? "/video" : ["pdf", "cda", "stl", "obj"].includes(o.kind) ? "/document" : "";
      const r = await fetch(`${base(o)}${tail}`, {
        headers: { Authorization: `Bearer ${token}`, ...(tail ? {} : { Accept: "application/dicom" }) } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const href = URL.createObjectURL(blob);
      if (o.kind === "pdf" || o.kind === "video") setOpen({ o, href, type: blob.type });
      else {
        const a = document.createElement("a");
        a.href = href; a.download = `${o.SOPInstanceUID}.${tail ? (blob.type.split("/")[1] || "bin") : "dcm"}`;
        a.click();
      }
    } catch (e) { setErr(e.message); }
  }

  return (
    <div data-testid="study-objects">
      <div className="lbl">{t("pacs.objects")}</div>
      <ul className="mini-list">
        {objects.map((o) => (
          <li key={o.SOPInstanceUID}>
            <Badge tone="info">{LABEL[o.kind] || o.kind}</Badge> <span dir="auto">{o.SeriesDescription || o.Modality || ""}</span>{" "}
            <Button variant="ghost" onClick={() => show(o)} data-object-kind={o.kind}>
              {["sr", "pdf", "video"].includes(o.kind) ? t("pacs.openObject") : t("pacs.download")}
            </Button>
          </li>
        ))}
      </ul>
      {err && <div className="err small">{err}</div>}
      <Modal open={!!open} title={open ? (open.sr?.title || LABEL[open.o.kind]) : ""}
             onClose={() => { if (open?.href) URL.revokeObjectURL(open.href); setOpen(null); }}>
        {open?.sr && (
          <>
            <div className="muted small">{open.sr.completion} · {open.sr.verification}</div>
            <pre className="raw" dir="auto" data-testid="sr-text">{open.sr.text}</pre>
          </>
        )}
        {open?.o.kind === "pdf" && open.href && (
          <iframe title="document" src={open.href} style={{ width: "100%", height: "70vh", border: 0 }} data-testid="pdf-frame" />
        )}
        {open?.o.kind === "video" && open.href && (
          <video controls src={open.href} style={{ width: "100%" }} data-testid="video-player" />
        )}
      </Modal>
    </div>
  );
}
