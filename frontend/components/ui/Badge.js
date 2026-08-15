/** Small status/label pill — wraps .pill (tokens.css). */
export default function Badge({ tone = "muted", children, className = "" }) {
  return <span className={`pill ${tone} ${className}`.trim()}>{children}</span>;
}
