// Role-based view access — radiologists get a focused ASR + chat workspace.
export const ROLE_VIEWS = {
  radiologist: ["dictate", "radiology"],
  doctor: ["dictate", "radiology", "reports", "templates", "ehr", "alerts", "settings"],
  resident: ["dictate", "radiology", "reports", "templates", "ehr", "education", "settings"],
  student: ["education", "settings"],
  admin: [
    "dictate", "radiology", "reports", "templates", "ehr", "alerts",
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
