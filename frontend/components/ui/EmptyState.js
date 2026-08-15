/** Centered placeholder for empty lists — wraps .empty-state (tokens.css). */
export default function EmptyState({ children }) {
  return <div className="empty-state">{children}</div>;
}
