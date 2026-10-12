import { Children, cloneElement, isValidElement, useEffect, useId, useRef } from "react";

/**
 * Labeled form field — wraps .lbl + .input/.textarea/.select so every
 * view stops repeating <label className="lbl">…</label> by hand.
 *
 * The label is tied to its control: Field generates an id (or uses the
 * child's own) and sets it on the single child control, so screen readers
 * announce the field by name and clicking the label focuses the input.
 */
export function Label({ children, hint, htmlFor, id }) {
  const ref = useRef(null);
  const auto = `c${useId().replace(/:/g, "")}`;
  // Views commonly write <Label>…</Label><TextInput …/> as siblings. When no
  // htmlFor is given, tie the label to the control that follows it, so every
  // field gets an accessible name without touching each call site.
  useEffect(() => {
    const el = ref.current;
    if (!el || htmlFor) return;
    const ctl = el.nextElementSibling;
    if (ctl && /^(INPUT|SELECT|TEXTAREA)$/.test(ctl.tagName)) {
      if (!ctl.id) ctl.id = auto;
      el.htmlFor = ctl.id;
    }
  });
  return (
    <label className="lbl" htmlFor={htmlFor} id={id} ref={ref}>
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

export function Field({ label, hint, children, id }) {
  const auto = useId();
  const only = Children.count(children) === 1 && isValidElement(children) ? children : null;
  const controlId = id || only?.props?.id || `f${auto.replace(/:/g, "")}`;
  const control = only && typeof only.type === "string"
    ? cloneElement(only, { id: controlId })
    : only && !only.props.id ? cloneElement(only, { id: controlId }) : children;
  return (
    <div className="field">
      {label && <Label hint={hint} htmlFor={only ? controlId : undefined}>{label}</Label>}
      {control}
    </div>
  );
}

export default Field;
