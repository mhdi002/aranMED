import { useEffect, useRef } from "react";
import {
  Stethoscope, Mic, Document, Brain, Activity, Folder, Settings,
  Heart, XRay, GradCap, Layers, ListChecks, Upload, Network,
} from "./icons";
import { useT } from "../lib/i18n";
import { viewsForRole } from "../lib/roles";

const NAV = [
  { id: "dictate",   labelKey: "nav.dictate",   icon: Mic,        sectionKey: "nav.workspace" },
  { id: "radiology", labelKey: "nav.radiology", icon: XRay,       sectionKey: "nav.workspace" },
  { id: "reports",   labelKey: "nav.reports",   icon: Document,   sectionKey: "nav.workspace" },
  { id: "templates", labelKey: "nav.templates", icon: Folder,     sectionKey: "nav.workspace" },
  { id: "ehr",           labelKey: "nav.ehrHub",       icon: Stethoscope,sectionKey: "nav.ehrSection" },
  { id: "pacs",          labelKey: "nav.pacs",         icon: Layers,     sectionKey: "nav.imaging" },
  { id: "pacs/worklist", labelKey: "nav.pacsWorklist", icon: ListChecks, sectionKey: "nav.imaging" },
  { id: "pacs/upload",   labelKey: "nav.pacsUpload",   icon: Upload,     sectionKey: "nav.imaging" },
  { id: "pacs/nodes",    labelKey: "nav.pacsNodes",    icon: Network,    sectionKey: "nav.imaging" },
  { id: "interop",       labelKey: "nav.interop",      icon: Network,    sectionKey: "nav.admin" },
  { id: "education", labelKey: "nav.tutor",     icon: GradCap,    sectionKey: "nav.education" },
  { id: "asr",       labelKey: "nav.asr",       icon: Brain,      sectionKey: "nav.models" },
  { id: "llm",       labelKey: "nav.llm",       icon: Activity,   sectionKey: "nav.models" },
  { id: "vision",    labelKey: "nav.vision",    icon: XRay,       sectionKey: "nav.models" },
  { id: "settings",  labelKey: "nav.settings",  icon: Settings,   sectionKey: "nav.account" },
];

export default function Sidebar({ active = "dictate", onSelect, open = false, role = "doctor" }) {
  const { t } = useT();
  const ref = useRef(null);
  // On narrow screens the closed drawer is only moved off-screen; make it
  // inert so keyboard users cannot tab into links they cannot see.
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 760px)");
    const apply = () => {
      if (!ref.current) return;
      const hide = mq.matches && !open;
      ref.current.inert = hide;
      if (hide) ref.current.setAttribute("aria-hidden", "true");
      else ref.current.removeAttribute("aria-hidden");
      if (mq.matches && open) ref.current.querySelector(".nav-item")?.focus();
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [open]);
  const allowed = new Set(viewsForRole(role));
  const items = NAV.filter((n) => allowed.has(n.id));
  const sections = [...new Set(items.map((n) => n.sectionKey))];
  const pick = (id) => { if (typeof onSelect === "function") onSelect(id); };
  const isActive = (id) => active === id || (id === "pacs" && active === "pacs/viewer")
    || (id === "ehr" && active === "ehr/chart");

  return (
    <aside className={`sidebar ${open ? "open" : ""}`} id="app-sidebar" ref={ref}>
      <div className="brand">
        <div className="brand-mark"><Stethoscope size={20} /></div>
        <div>
          <div className="brand-name">aranmed</div>
          <div className="brand-sub">{t("nav.brandSub")}</div>
        </div>
      </div>

      <nav className="nav" aria-label={t("a11y.mainNav")}>
        {sections.map((sec) => (
          <div key={sec}>
            <div className="nav-section">{t(sec)}</div>
            {items.filter((n) => n.sectionKey === sec).map((n) => {
              const Ico = n.icon;
              return (
                <button
                  key={n.id}
                  type="button"
                  className={`nav-item ${isActive(n.id) ? "active" : ""}`}
                  aria-current={isActive(n.id) ? "page" : undefined}
                  onClick={() => pick(n.id)}
                >
                  <span className="ico"><Ico size={16} /></span>
                  {t(n.labelKey)}
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="sidebar-foot">
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <Heart size={12} /> {t("nav.footVersion")}
        </div>
        <div style={{ marginTop: 4 }}>{t("nav.footName")}</div>
      </div>
    </aside>
  );
}
