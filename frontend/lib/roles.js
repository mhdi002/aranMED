// Role-based view access — radiologists get a focused ASR + chat workspace.
export const ROLE_VIEWS = {
  radiologist: ["dictate", "radiology", "pacs", "pacs/viewer", "pacs/worklist", "pacs/upload",
                "clinical", "clinical/chart", "transfers"],
  doctor: ["dictate", "radiology", "reports", "templates", "ehr", "alerts",
           "clinical", "clinical/chart", "transfers", "ems",
           "pacs", "pacs/viewer", "pacs/worklist", "pacs/upload", "settings"],
  resident: ["dictate", "radiology", "reports", "templates", "ehr",
             "clinical", "clinical/chart", "transfers", "ems",
             "pacs", "pacs/viewer", "pacs/worklist", "education", "settings"],
  student: ["education", "settings"],
  admin: [
    "dictate", "radiology", "reports", "templates", "ehr", "alerts",
    "clinical", "clinical/chart", "transfers", "ems", "interop",
    "pacs", "pacs/viewer", "pacs/worklist", "pacs/upload", "pacs/nodes",
    "education", "asr", "llm", "vision", "settings",
  ],
};

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
