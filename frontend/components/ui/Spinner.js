/** Inline loading spinner — wraps .spinner (tokens.css). */
export default function Spinner({ dark = false, style }) {
  return <span className={`spinner ${dark ? "dark" : ""}`.trim()} style={style} />;
}
