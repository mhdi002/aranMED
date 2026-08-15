/**
 * Shared button — wraps the .btn class family (tokens.css) so call sites
 * stop hand-writing `className="btn ghost icon-only"` combinations.
 */
export default function Button({
  variant = "solid", // "solid" | "ghost" | "danger"
  iconOnly = false,
  loading = false,
  children,
  className = "",
  disabled,
  ...rest
}) {
  const cls = [
    "btn",
    variant === "ghost" ? "ghost" : "",
    variant === "danger" ? "danger" : "",
    iconOnly ? "icon-only" : "",
    className,
  ].filter(Boolean).join(" ");

  return (
    <button className={cls} disabled={disabled || loading} {...rest}>
      {loading ? <span className="spinner" /> : null}
      {children}
    </button>
  );
}
