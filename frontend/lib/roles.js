// Role-based view access — radiologists get a focused ASR + chat workspace.
export const ROLE_VIEWS = {
  radiologist: ["dictate", "radiology", "pacs", "pacs/viewer", "pacs/worklist", "pacs/upload"],
  doctor: ["dictate", "radiology", "reports", "templates", "ehr", "alerts",
           "pacs", "pacs/viewer", "pacs/worklist", "pacs/upload", "settings"],
  resident: ["dictate", "radiology", "reports", "templates", "ehr",
             "pacs", "pacs/viewer", "pacs/worklist", "education", "settings"],
  student: ["education", "settings"],
  admin: [
    "dictate", "radiology", "reports", "templates", "ehr", "alerts",
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
