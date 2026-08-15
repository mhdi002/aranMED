/** Row of action buttons — wraps .toolbar (tokens.css). */
export default function Toolbar({ children, className = "" }) {
  return <div className={`toolbar ${className}`.trim()}>{children}</div>;
}
