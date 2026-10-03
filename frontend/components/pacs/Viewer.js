// Diagnostic image viewer (client-only; loaded with next/dynamic ssr:false).
//
// Cornerstone3D stack viewports fed by our DICOMweb server. Mouse:
//   left = active tool (W/L by default), middle = pan, right = zoom,
//   wheel = scroll through the stack. Keyboard: ↑/↓ or PgUp/PgDn scroll,
//   I invert, R reset, C cine.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ensureInit, registerInstance, setToken, metaFor } from "./csSetup";
import { dicomwebJson, WINDOW_PRESETS } from "../../lib/pacs";

const ENGINE_ID = "aranmed-engine";
const TOOL_GROUP = "aranmed-tools";
const LAYOUTS = { "1x1": [1, 1], "1x2": [1, 2], "2x2": [2, 2] };
const TOOLS = [
  { id: "WindowLevel", label: "W/L", key: "w" },
  { id: "Pan", label: "Pan", key: "p" },
  { id: "Zoom", label: "Zoom", key: "z" },
  { id: "Length", label: "Length", key: "l" },
  { id: "Angle", label: "Angle", key: "a" },
  { id: "EllipticalROI", label: "Ellipse ROI", key: "e" },
  { id: "RectangleROI", label: "Rect ROI", key: "b" },
  { id: "Probe", label: "Probe (HU)", key: "h" },
  { id: "PlanarRotate", label: "Rotate", key: "t" },
];

export default function Viewer({ studyUid, series, token, t, onMeasurements, initialSeries }) {
  const [layout, setLayout] = useState("1x1");
  const [assign, setAssign] = useState([]);           // viewport index -> seriesUid
  const [active, setActive] = useState(0);
  const [tool, setTool] = useState("WindowLevel");
  const [overlay, setOverlay] = useState({});         // vpIdx -> {index,total,wc,ww,zoom,desc}
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [cine, setCine] = useState(false);
  const [fps, setFps] = useState(10);
  const [loading, setLoading] = useState({});
  const elRefs = useRef([]);
  const engineRef = useRef(null);
  const libRef = useRef(null);
  const stacks = useRef({});                           // seriesUid -> imageIds
  const cineTimer = useRef(null);

  const [rows, cols] = LAYOUTS[layout];
  const count = rows * cols;
  const vpId = (i) => `aranmed-vp-${i}`;

  // initial series assignment
  useEffect(() => {
    if (!series?.length) return;
    const order = [...series].sort((a, b) => (a.SeriesNumber || 0) - (b.SeriesNumber || 0));
    const first = initialSeries && order.find((s) => s.SeriesInstanceUID === initialSeries);
    const uids = order.map((s) => s.SeriesInstanceUID);
    if (first) uids.unshift(...uids.splice(uids.indexOf(initialSeries), 1));
    setAssign(uids);
  }, [series, initialSeries]);

  const loadStack = useCallback(async (seriesUid) => {
    if (stacks.current[seriesUid]) return stacks.current[seriesUid];
    const metas = await dicomwebJson(`/studies/${studyUid}/series/${seriesUid}/metadata`, token);
    const entries = metas.map((m) => registerInstance(studyUid, seriesUid, m))
      .filter((e) => e.info.rows > 0);
    entries.sort((a, b) => (a.info.instanceNumber || 0) - (b.info.instanceNumber || 0));
    const ids = entries.flatMap((e) => e.ids);
    stacks.current[seriesUid] = ids;
    return ids;
  }, [studyUid, token]);

  const refreshOverlay = useCallback((i) => {
    const eng = engineRef.current;
    const vp = eng?.getViewport(vpId(i));
    if (!vp) return;
    const props = vp.getProperties?.() || {};
    const voi = props.voiRange;
    const wl = voi ? libRef.current.cs.utilities.windowLevel.toWindowLevel(voi.lower, voi.upper) : null;
    const ids = vp.getImageIds?.() || [];
    const idx = vp.getCurrentImageIdIndex?.() ?? 0;
    const m = metaFor(ids[idx]);
    let zoom;
    try { zoom = vp.getZoom?.(); } catch (_) { zoom = undefined; }
    setOverlay((o) => ({
      ...o,
      [i]: {
        index: idx + 1, total: ids.length,
        wc: wl ? Math.round(wl.windowCenter) : undefined,
        ww: wl ? Math.round(wl.windowWidth) : undefined,
        zoom: zoom ? zoom.toFixed(2) : undefined,
        modality: m?.modality, instance: m?.instanceNumber, frame: m?.frame,
        invert: props.invert,
      },
    }));
  }, []);

  const collectMeasurements = useCallback(() => {
    const cst = libRef.current?.cst;
    if (!cst || !onMeasurements) return;
    const all = cst.annotation.state.getAllAnnotations() || [];
    onMeasurements(all.map((a) => {
      const stats = a.data?.cachedStats ? Object.values(a.data.cachedStats)[0] || {} : {};
      return {
        uid: a.annotationUID, tool: a.metadata?.toolName,
        imageId: a.metadata?.referencedImageId,
        length: stats.length, unit: stats.unit, angle: stats.angle,
        area: stats.area, areaUnit: stats.areaUnit, mean: stats.mean, stdDev: stats.stdDev,
        max: stats.max, min: stats.min, value: stats.value ?? (Array.isArray(stats.value) ? stats.value[0] : undefined),
        modalityUnit: stats.modalityUnit,
      };
    }));
  }, [onMeasurements]);

  // Cornerstone init + engine
  useEffect(() => {
    let cancelled = false;
    setToken(token);
    (async () => {
      try {
        const lib = await ensureInit();
        if (cancelled) return;
        libRef.current = lib;
        const { cs, cst } = lib;
        engineRef.current = cs.getRenderingEngine(ENGINE_ID) || new cs.RenderingEngine(ENGINE_ID);
        let tg = cst.ToolGroupManager.getToolGroup(TOOL_GROUP);
        if (!tg) {
          tg = cst.ToolGroupManager.createToolGroup(TOOL_GROUP);
          TOOLS.forEach((x) => tg.addTool(cst[`${x.id}Tool`].toolName));
          tg.addTool(cst.StackScrollTool.toolName);
        }
        const B = cst.Enums.MouseBindings;
        tg.setToolActive(cst.PanTool.toolName, { bindings: [{ mouseButton: B.Auxiliary }] });
        tg.setToolActive(cst.ZoomTool.toolName, { bindings: [{ mouseButton: B.Secondary }] });
        tg.setToolActive(cst.StackScrollTool.toolName, { bindings: [{ mouseButton: B.Wheel }] });
        tg.setToolActive(cst.WindowLevelTool.toolName, { bindings: [{ mouseButton: B.Primary }] });
        const evts = [cst.Enums.Events.ANNOTATION_COMPLETED, cst.Enums.Events.ANNOTATION_MODIFIED,
                      cst.Enums.Events.ANNOTATION_REMOVED];
        evts.forEach((e) => cs.eventTarget.addEventListener(e, collectMeasurements));
        setReady(true);
      } catch (e) {
        setError(String(e?.message || e));
      }
    })();
    return () => {
      cancelled = true;
      const lib = libRef.current;
      if (lib) {
        [lib.cst.Enums.Events.ANNOTATION_COMPLETED, lib.cst.Enums.Events.ANNOTATION_MODIFIED,
         lib.cst.Enums.Events.ANNOTATION_REMOVED].forEach((e) =>
          lib.cs.eventTarget.removeEventListener(e, collectMeasurements));
      }
    };
  }, [token, collectMeasurements]);

  // (re)build viewports when layout / assignment changes
  useEffect(() => {
    if (!ready || !assign.length) return undefined;
    const { cs, cst } = libRef.current;
    const eng = engineRef.current;
    const tg = cst.ToolGroupManager.getToolGroup(TOOL_GROUP);
    const inputs = [];
    for (let i = 0; i < count; i += 1) {
      const el = elRefs.current[i];
      if (el) inputs.push({ viewportId: vpId(i), type: cs.Enums.ViewportType.STACK, element: el,
                            defaultOptions: { background: [0, 0, 0] } });
    }
    eng.getViewports().forEach((vp) => {
      if (!inputs.find((x) => x.viewportId === vp.id)) {
        try { tg.removeViewports(ENGINE_ID, vp.id); } catch (_) { /* noop */ }
        eng.disableElement(vp.id);
      }
    });
    eng.setViewports(inputs);
    inputs.forEach((x) => tg.addViewport(x.viewportId, ENGINE_ID));
    const listeners = [];
    let cancelled = false;
    (async () => {
      for (let i = 0; i < inputs.length; i += 1) {
        const seriesUid = assign[i % assign.length];
        if (!seriesUid) continue;
        setLoading((l) => ({ ...l, [i]: true }));
        try {
          const ids = await loadStack(seriesUid);
          if (cancelled || !ids.length) continue;
          const vp = eng.getViewport(vpId(i));
          await vp.setStack(ids, Math.floor(ids.length / 2));
          vp.render();
          const el = elRefs.current[i];
          const h = () => refreshOverlay(i);
          [cs.Enums.Events.IMAGE_RENDERED, cs.Enums.Events.VOI_MODIFIED,
           cs.Enums.Events.STACK_NEW_IMAGE].forEach((e) => { el.addEventListener(e, h); listeners.push([el, e, h]); });
          el.dataset.loaded = "true";
          el.dataset.series = seriesUid;
          refreshOverlay(i);
        } catch (e) {
          setError(String(e?.message || e));
        } finally {
          setLoading((l) => ({ ...l, [i]: false }));
        }
      }
    })();
    const onResize = () => eng.resize(true, true);
    window.addEventListener("resize", onResize);
    return () => {
      cancelled = true;
      window.removeEventListener("resize", onResize);
      listeners.forEach(([el, e, h]) => el.removeEventListener(e, h));
    };
  }, [ready, assign, count, loadStack, refreshOverlay]);

  // primary tool switch
  useEffect(() => {
    if (!ready) return;
    const { cst } = libRef.current;
    const tg = cst.ToolGroupManager.getToolGroup(TOOL_GROUP);
    const B = cst.Enums.MouseBindings;
    TOOLS.forEach((x) => {
      const name = cst[`${x.id}Tool`].toolName;
      if (x.id === tool) return;
      if (x.id === "Pan") tg.setToolActive(name, { bindings: [{ mouseButton: B.Auxiliary }] });
      else if (x.id === "Zoom") tg.setToolActive(name, { bindings: [{ mouseButton: B.Secondary }] });
      else tg.setToolPassive(name);
    });
    tg.setToolActive(cst[`${tool}Tool`].toolName, { bindings: [{ mouseButton: B.Primary }] });
  }, [tool, ready]);

  const activeVp = useCallback(() => engineRef.current?.getViewport(vpId(active)), [active]);

  const applyPreset = (p) => {
    const vp = activeVp();
    if (!vp) return;
    if (p.c === null) vp.resetProperties();
    else vp.setProperties({ voiRange: libRef.current.cs.utilities.windowLevel.toLowHighRange(p.w, p.c) });
    vp.render();
    refreshOverlay(active);
  };
  const invert = () => {
    const vp = activeVp();
    if (!vp) return;
    vp.setProperties({ invert: !vp.getProperties().invert });
    vp.render();
    refreshOverlay(active);
  };
  const rotate = () => {
    const vp = activeVp();
    if (!vp) return;
    const cur = vp.getViewPresentation?.()?.rotation || 0;
    vp.setViewPresentation({ rotation: (cur + 90) % 360 });
    vp.render();
  };
  const flip = (axis) => {
    const vp = activeVp();
    if (!vp) return;
    const cam = vp.getCamera();
    vp.setCamera(axis === "h" ? { flipHorizontal: !cam.flipHorizontal } : { flipVertical: !cam.flipVertical });
    vp.render();
  };
  const reset = () => {
    const vp = activeVp();
    if (!vp) return;
    vp.resetCamera();
    vp.resetProperties();
    vp.render();
    refreshOverlay(active);
  };
  const scroll = useCallback((delta) => {
    const vp = activeVp();
    if (!vp?.getImageIds) return;
    const n = vp.getImageIds().length;
    if (!n) return;
    const next = (vp.getCurrentImageIdIndex() + delta + n) % n;
    vp.setImageIdIndex(next);
  }, [activeVp]);
  const clearMeasurements = () => {
    const cst = libRef.current?.cst;
    if (!cst) return;
    cst.annotation.state.removeAllAnnotations();
    engineRef.current?.render();
    collectMeasurements();
  };

  // cine
  useEffect(() => {
    clearInterval(cineTimer.current);
    if (cine) cineTimer.current = setInterval(() => scroll(1), Math.max(30, 1000 / fps));
    return () => clearInterval(cineTimer.current);
  }, [cine, fps, scroll]);

  // keyboard
  useEffect(() => {
    const onKey = (e) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
      if (e.key === "ArrowDown" || e.key === "PageDown") { scroll(1); e.preventDefault(); }
      else if (e.key === "ArrowUp" || e.key === "PageUp") { scroll(-1); e.preventDefault(); }
      else if (e.key === "i") invert();
      else if (e.key === "r") reset();
      else if (e.key === "c") setCine((c) => !c);
      else {
        const tl = TOOLS.find((x) => x.key === e.key);
        if (tl) setTool(tl.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const seriesByUid = useMemo(() => Object.fromEntries((series || []).map((s) => [s.SeriesInstanceUID, s])), [series]);

  const showSeries = (uid) => {
    setAssign((a) => {
      const next = [...a];
      next[active] = uid;
      // keep remaining series available for other viewports
      return next.length ? next : [uid];
    });
    stacks.current[uid] || loadStack(uid).catch(() => {});
  };

  return (
    <div className="viewer" data-testid="pacs-viewer">
      <div className="viewer-toolbar" role="toolbar" aria-label="Viewer tools">
        <div className="vt-group">
          {TOOLS.map((x) => (
            <button key={x.id} type="button" className={`vt-btn ${tool === x.id ? "active" : ""}`}
                    title={`${x.label} (${x.key})`} onClick={() => setTool(x.id)}
                    data-tool={x.id}>{t ? t(`pacs.tool.${x.id}`) : x.label}</button>
          ))}
        </div>
        <div className="vt-group">
          <select className="vt-select" aria-label="Window preset" data-testid="wl-preset"
                  onChange={(e) => applyPreset(WINDOW_PRESETS.find((p) => p.id === e.target.value))}
                  defaultValue="default">
            {WINDOW_PRESETS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
          <button type="button" className="vt-btn" onClick={invert} title="Invert (i)">Invert</button>
          <button type="button" className="vt-btn" onClick={rotate} title="Rotate 90°">⟳ 90°</button>
          <button type="button" className="vt-btn" onClick={() => flip("h")} title="Flip horizontal">⇋</button>
          <button type="button" className="vt-btn" onClick={() => flip("v")} title="Flip vertical">⇵</button>
          <button type="button" className="vt-btn" onClick={reset} title="Reset (r)">Reset</button>
        </div>
        <div className="vt-group">
          <button type="button" className={`vt-btn ${cine ? "active" : ""}`} onClick={() => setCine((c) => !c)}
                  title="Cine (c)">{cine ? "❚❚" : "▶"} Cine</button>
          <input type="range" min="2" max="30" value={fps} aria-label="Cine speed"
                 onChange={(e) => setFps(parseInt(e.target.value, 10))} className="vt-range" />
          <span className="vt-fps">{fps} fps</span>
        </div>
        <div className="vt-group">
          {Object.keys(LAYOUTS).map((l) => (
            <button key={l} type="button" className={`vt-btn ${layout === l ? "active" : ""}`}
                    onClick={() => setLayout(l)} data-layout={l}>{l}</button>
          ))}
          <button type="button" className="vt-btn" onClick={clearMeasurements}>Clear marks</button>
        </div>
      </div>

      {error && <div className="viewer-error">{error}</div>}

      <div className="viewer-grid" style={{ gridTemplateColumns: `repeat(${cols}, 1fr)`,
                                            gridTemplateRows: `repeat(${rows}, 1fr)` }}>
        {Array.from({ length: count }).map((_, i) => {
          const o = overlay[i] || {};
          const s = seriesByUid[assign[i % Math.max(1, assign.length)]];
          return (
            <div key={`${layout}-${i}`} className={`vp-wrap ${active === i ? "active" : ""}`}
                 onMouseDown={() => setActive(i)} onContextMenu={(e) => e.preventDefault()}>
              <div ref={(el) => { elRefs.current[i] = el; }} className="vp" data-testid={`viewport-${i}`} />
              <div className="vp-ov tl">
                <div>{s ? `${s.Modality || ""} · ${s.SeriesDescription || `Series ${s.SeriesNumber ?? ""}`}` : ""}</div>
                <div>{o.total ? `Im ${o.index}/${o.total}` : ""}</div>
              </div>
              <div className="vp-ov br" data-testid={`overlay-${i}`}>
                {o.ww !== undefined && <div>W {o.ww} · L {o.wc}</div>}
                {o.zoom && <div>Zoom {o.zoom}×</div>}
                {o.invert && <div>INV</div>}
              </div>
              {loading[i] && <div className="vp-loading"><span className="spinner" /></div>}
            </div>
          );
        })}
      </div>

      <div className="viewer-hint muted small">
        Left: {tool} · Middle: pan · Right: zoom · Wheel/↑↓: scroll · i invert · r reset · c cine
      </div>

      <SeriesPicker series={series} assign={assign} active={active} onPick={showSeries} />
    </div>
  );
}

function SeriesPicker({ series, assign, active, onPick }) {
  if (!series?.length) return null;
  return (
    <div className="series-pick" aria-label="Series">
      {series.map((s) => (
        <button key={s.SeriesInstanceUID} type="button"
                className={`sp-item ${assign[active] === s.SeriesInstanceUID ? "active" : ""}`}
                onClick={() => onPick(s.SeriesInstanceUID)} data-series={s.SeriesInstanceUID}>
          <b>{s.Modality} #{s.SeriesNumber ?? "?"}</b>
          <span>{s.SeriesDescription || "—"}</span>
          <span className="muted">{s.NumberOfSeriesRelatedInstances} img</span>
        </button>
      ))}
    </div>
  );
}
