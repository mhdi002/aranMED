/**
 * Labeled form field — wraps .lbl + .input/.textarea/.select so every
 * view stops repeating <label className="lbl">…</label> by hand.
 */
export function Label({ children, hint }) {
  return (
    <label className="lbl">
      {children} {hint && <span className="muted">({hint})</span>}
    </label>
  );
}

export function TextInput({ className = "", ...rest }) {
  return <input className={`input ${className}`.trim()} {...rest} />;
}

export function TextArea({ className = "", ...rest }) {
  return <textarea className={`textarea ${className}`.trim()} {...rest} />;
}

export function Select({ className = "", children, ...rest }) {
  return (
    <select className={`select ${className}`.trim()} {...rest}>
      {children}
    </select>
  );
}

export function Field({ label, hint, children }) {
  return (
    <div>
      {label && <Label hint={hint}>{label}</Label>}
      {children}
    </div>
  );
}

export default Field;
