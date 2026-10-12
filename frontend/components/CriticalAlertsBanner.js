import { useT } from "../lib/i18n";

/** Warning banner for critical / lethal clinical findings. */
export default function CriticalAlertsBanner({ alerts }) {
  const { t } = useT();
  const list = Array.isArray(alerts) ? alerts.filter(Boolean) : [];
  if (!list.length) return null;

  return (
    <div
      className="critical-alerts-banner"
      role="alert"
      aria-live="assertive"
    >
      <div className="critical-alerts-banner__title">
        {t("alert.critical")}
      </div>
      <ul className="critical-alerts-banner__list">
        {list.map((a, i) => (
          <li key={i}>
            <strong>{(a.severity || "critical").toUpperCase()}</strong>
            {a.label ? ` · ${a.label}` : ""}
            {": "}
            {a.message || a.excerpt || t("alert.review")}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Softer banner for spoken-vs-selected template mismatch / naming rules. */
export function TemplateMismatchBanner({ mismatch }) {
  const { t } = useT();
  if (!mismatch || (!mismatch.mismatch && !(mismatch.naming_violations || []).length)) {
    return null;
  }
  const violations = mismatch.naming_violations || [];
  return (
    <div className="template-mismatch-banner" role="status">
      <div className="template-mismatch-banner__title">
        {t("alert.templateNotice")}
      </div>
      <p style={{ margin: "4px 0 0" }}>
        {mismatch.message || t("alert.templateDefault")}
      </p>
      {violations.length > 0 && (
        <ul style={{ margin: "6px 0 0", paddingInlineStart: 18 }}>
          {violations.map((v, i) => (
            <li key={i}>{v.message || v.type}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
