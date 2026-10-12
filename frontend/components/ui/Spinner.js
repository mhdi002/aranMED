import { useT } from "../../lib/i18n";

/** Inline loading spinner — wraps .spinner (tokens.css); announced as busy. */
export default function Spinner({ dark = true, style, label }) {
  const { t } = useT();
  return (
    <span className={`spinner ${dark ? "dark" : ""}`.trim()} style={style}
          role="status" aria-label={label || t("common.loading")} />
  );
}
