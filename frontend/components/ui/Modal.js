import { useEffect, useRef } from "react";

/**
 * Accessible modal dialog — replaces the app's former reliance on the
 * native window.confirm()/alert() for destructive-action confirmation
 * (EhrView, ReportsView, SettingsView). Closes on Escape or backdrop click,
 * moves focus to itself on open.
 */
export default function Modal({ open, title, children, onClose, actions }) {
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    ref.current?.focus();
    const onKey = (e) => { if (e.key === "Escape") onClose?.(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose?.(); }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={ref}
      >
        {title && <h2 className="modal-title">{title}</h2>}
        <div className="modal-body">{children}</div>
        {actions && <div className="modal-actions">{actions}</div>}
      </div>
    </div>
  );
}

/** Convenience: a confirm-style modal with Cancel + Confirm buttons. */
export function ConfirmModal({ open, title, children, onCancel, onConfirm, confirmLabel = "Confirm", danger = false }) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={onCancel}
      actions={
        <>
          <button className="btn ghost" onClick={onCancel}>Cancel</button>
          <button className={`btn ${danger ? "danger" : ""}`.trim()} onClick={onConfirm}>
            {confirmLabel}
          </button>
        </>
      }
    >
      {children}
    </Modal>
  );
}
