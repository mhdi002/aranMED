import { useEffect, useId, useRef } from "react";
import { useT } from "../../lib/i18n";

const FOCUSABLE = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), ' +
  'select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Accessible modal dialog: closes on Escape or backdrop click, moves focus
 * into the dialog, keeps Tab inside it, locks page scroll while open and
 * gives focus back to whatever opened it.
 */
export default function Modal({ open, title, children, onClose, actions, wide = false }) {
  const ref = useRef(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return undefined;
    const opener = document.activeElement;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const first = ref.current?.querySelector(FOCUSABLE);
    (first || ref.current)?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); closeRef.current?.(); return; }
      if (e.key !== "Tab" || !ref.current) return;
      const items = [...ref.current.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null);
      if (!items.length) { e.preventDefault(); return; }
      const [a, z] = [items[0], items[items.length - 1]];
      if (e.shiftKey && document.activeElement === a) { e.preventDefault(); z.focus(); }
      else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      if (opener && typeof opener.focus === "function") opener.focus();
    };
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose?.(); }}
    >
      <div
        className={`modal${wide ? " wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        tabIndex={-1}
        ref={ref}
      >
        {title && <h2 className="modal-title" id={titleId}>{title}</h2>}
        <div className="modal-body">{children}</div>
        {actions && <div className="modal-actions">{actions}</div>}
      </div>
    </div>
  );
}

/** Convenience: a confirm-style modal with Cancel + Confirm buttons. */
export function ConfirmModal({ open, title, children, onCancel, onConfirm, confirmLabel,
                               danger = false, busy = false }) {
  const { t } = useT();
  return (
    <Modal
      open={open}
      title={title}
      onClose={busy ? undefined : onCancel}
      actions={
        <>
          <button className="btn ghost" onClick={onCancel} disabled={busy}>{t("common.cancel")}</button>
          <button className={`btn ${danger ? "danger" : ""}`.trim()} onClick={onConfirm} disabled={busy}
                  data-testid="confirm-ok">
            {busy ? <span className="spinner" /> : null}
            {confirmLabel || t("common.confirm")}
          </button>
        </>
      }
    >
      {children}
    </Modal>
  );
}
