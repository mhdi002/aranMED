// Shared display formatting.

/** Epoch seconds (or ms, or an ISO string) -> local date-time, "—" when empty. */
export function fmtTs(t, lang) {
  if (t === null || t === undefined || t === "") return "—";
  let d;
  if (typeof t === "number") d = new Date(t < 1e12 ? t * 1000 : t);
  else d = new Date(t);
  if (Number.isNaN(d.getTime())) return String(t);
  return d.toLocaleString(lang === "fa" ? "fa-IR" : undefined);
}
