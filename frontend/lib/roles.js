// Role-based view access — radiologists get a focused ASR + chat workspace.
// Every patient-data screen (search, chart, transfers, EMS, medication
// alerts, free-text intake) lives inside the single EHR section ("ehr" hub +
// "ehr/chart"); EHR_TABS says which hub tabs each role sees.
export const ROLE_VIEWS = {
  radiologist: ["dictate", "radiology", "ehr", "ehr/chart",
                "pacs", "pacs/viewer", "pacs/worklist", "pacs/upload"],
  doctor: ["dictate", "radiology", "reports", "templates", "ehr", "ehr/chart",
           "pacs", "pacs/viewer", "pacs/worklist", "pacs/upload", "settings"],
  resident: ["dictate", "radiology", "reports", "templates", "ehr", "ehr/chart",
             "pacs", "pacs/viewer", "pacs/worklist", "education", "settings"],
  student: ["education", "settings"],
  admin: [
    "dictate", "radiology", "reports", "templates", "ehr", "ehr/chart", "interop",
    "pacs", "pacs/viewer", "pacs/worklist", "pacs/upload", "pacs/nodes",
    "education", "asr", "llm", "vision", "settings",
  ],
};

export const EHR_TABS = {
  radiologist: ["patients", "transfers"],
  doctor: ["patients", "transfers", "ems", "alerts", "intake"],
  resident: ["patients", "transfers", "ems", "intake"],
  student: [],
  admin: ["patients", "transfers", "ems", "alerts", "intake"],
};

export function ehrTabsForRole(role) {
  return EHR_TABS[role] || EHR_TABS.doctor;
}

export const ROLE_LABELS = {
  radiologist: "Radiologist",
  doctor: "Doctor",
  resident: "Resident",
  student: "Student",
  admin: "Admin",
};

export function viewsForRole(role) {
  return ROLE_VIEWS[role] || ROLE_VIEWS.doctor;
}

export function defaultViewForRole(role) {
  return viewsForRole(role)[0] || "dictate";
}

export function canAccessView(role, viewId) {
  return viewsForRole(role).includes(viewId);
}
