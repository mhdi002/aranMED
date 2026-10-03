// View id -> route + topbar title. Single source of truth for AppShell,
// Sidebar navigation and the (now real, deep-linkable) per-view pages —
// see docs/core/ROADMAP.md and the M4/M5 frontend redesign notes.
export const VIEW_TITLES = {
  dictate: "Dictation Workbench",
  radiology: "Radiology Vision Chat",
  reports: "Saved Reports",
  templates: "Report Templates",
  ehr: "Electronic Health Record",
  alerts: "Medication Alerts",
  education: "Education Tutor",
  pacs: "Imaging Archive (PACS)",
  "pacs/viewer": "Image Viewer",
  "pacs/worklist": "Modality Worklist",
  "pacs/upload": "Import Studies",
  "pacs/nodes": "DICOM Nodes & Federation",
  // Role-generic titles. The concrete model name is resolved at runtime from
  // the backend registry (GET /api/models) — never hardcoded. See ModelsView.
  asr: "Speech Recognition Model",
  llm: "Language Model",
  vision: "Vision Model",
  settings: "Settings",
};

export function routeForView(id) {
  return `/${id}`;
}

export function viewForPathname(pathname) {
  return (pathname || "/").replace(/^\//, "") || "dictate";
}
