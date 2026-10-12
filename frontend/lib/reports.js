// Reports saved in this browser (Dictate → Save, Reports view). Guarded:
// storage can be full, disabled (private mode) or hold malformed JSON.
const KEY = "asr.reports";

export function loadSavedReports() {
  try {
    const list = JSON.parse(localStorage.getItem(KEY) || "[]");
    return Array.isArray(list) ? list.filter((r) => r && typeof r.report === "string") : [];
  } catch (_) {
    return [];
  }
}

export function saveReports(list) {
  try {
    localStorage.setItem(KEY, JSON.stringify(list));
    return true;
  } catch (_) {
    return false;
  }
}

export function clearSavedReports() {
  try { localStorage.removeItem(KEY); } catch (_) { /* nothing to clear */ }
}
