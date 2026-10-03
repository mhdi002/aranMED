import {
  Stethoscope, Mic, Document, Brain, Activity, Folder, Settings,
  Heart, XRay, Bell, GradCap, Layers, ListChecks, Upload, Network, User, Send, Activity as Pulse,
} from "./icons";
import { useT } from "../lib/i18n";
import { viewsForRole } from "../lib/roles";

const NAV = [
  { id: "dictate",   labelKey: "nav.dictate",   icon: Mic,        sectionKey: "nav.workspace" },
  { id: "radiology", labelKey: "nav.radiology", icon: XRay,       sectionKey: "nav.workspace" },
  { id: "reports",   labelKey: "nav.reports",   icon: Document,   sectionKey: "nav.workspace" },
  { id: "templates", labelKey: "nav.templates", icon: Folder,     sectionKey: "nav.workspace" },
  { id: "ehr",       labelKey: "nav.ehr",       icon: Stethoscope,sectionKey: "nav.workspace" },
  { id: "alerts",    labelKey: "nav.alerts",    icon: Bell,       sectionKey: "nav.workspace" },
  { id: "clinical",      labelKey: "nav.patients",     icon: User,       sectionKey: "nav.clinical" },
  { id: "transfers",     labelKey: "nav.transfers",    icon: Send,       sectionKey: "nav.clinical" },
  { id: "ems",           labelKey: "nav.ems",          icon: Pulse,      sectionKey: "nav.clinical" },
  { id: "interop",       labelKey: "nav.interop",      icon: Network,    sectionKey: "nav.clinical" },
  { id: "pacs",          labelKey: "nav.pacs",         icon: Layers,     sectionKey: "nav.imaging" },
  { id: "pacs/worklist", labelKey: "nav.pacsWorklist", icon: ListChecks, sectionKey: "nav.imaging" },
  { id: "pacs/upload",   labelKey: "nav.pacsUpload",   icon: Upload,     sectionKey: "nav.imaging" },
  { id: "pacs/nodes",    labelKey: "nav.pacsNodes",    icon: Network,    sectionKey: "nav.imaging" },
  { id: "education", labelKey: "nav.tutor",     icon: GradCap,    sectionKey: "nav.education" },
  { id: "asr",       labelKey: "nav.asr",       icon: Brain,      sectionKey: "nav.models" },
  { id: "llm",       labelKey: "nav.llm",       icon: Activity,   sectionKey: "nav.models" },
  { id: "vision",    labelKey: "nav.vision",    icon: XRay,       sectionKey: "nav.models" },
  { id: "settings",  labelKey: "nav.settings",  icon: Settings,   sectionKey: "nav.account" },
];

export default function Sidebar({ active = "dictate", onSelect, open = false, role = "doctor" }) {
  const { t } = useT();
  const allowed = new Set(viewsForRole(role));
  const items = NAV.filter((n) => allowed.has(n.id));
  const sections = [...new Set(items.map((n) => n.sectionKey))];
  const pick = (id) => { if (typeof onSelect === "function") onSelect(id); };

  return (
    <aside className={`sidebar ${open ? "open" : ""}`}>
      <div className="brand">
        <div className="brand-mark"><Stethoscope size={20} /></div>
        <div>
          <div className="brand-name">aranmed</div>
          <div className="brand-sub">{t("nav.brandSub")}</div>
        </div>
      </div>

      <nav className="nav">
        {sections.map((sec) => (
          <div key={sec}>
            <div className="nav-section">{t(sec)}</div>
            {items.filter((n) => n.sectionKey === sec).map((n) => {
              const Ico = n.icon;
              return (
                <button
                  key={n.id}
                  type="button"
                  className={`nav-item ${active === n.id || (n.id === "pacs" && active === "pacs/viewer") || (n.id === "clinical" && active === "clinical/chart") ? "active" : ""}`}
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
          <Heart size={12} /> v2.0 — local-first
        </div>
        <div style={{ marginTop: 4 }}>© Radiology AI Workbench</div>
      </div>
    </aside>
  );
}
