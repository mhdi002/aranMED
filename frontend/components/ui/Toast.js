import { createContext, useCallback, useContext, useRef, useState } from "react";

/**
 * Small toast notifications for "saved", "copied" and failures that are not
 * tied to a form. Announced to screen readers (role=status / role=alert).
 *
 *   const toast = useToast();
 *   toast.success(t("common.saved"));
 *   toast.error(err.message);
 */
const Ctx = createContext({ success: () => {}, error: () => {}, info: () => {} });

export function ToastProvider({ children }) {
  const [items, setItems] = useState([]);
  const seq = useRef(0);

  const dismiss = useCallback((id) => setItems((xs) => xs.filter((x) => x.id !== id)), []);
  const push = useCallback((kind, message, ms = 4000) => {
    if (!message) return;
    const id = ++seq.current;
    setItems((xs) => [...xs.slice(-3), { id, kind, message: String(message) }]);
    setTimeout(() => dismiss(id), kind === "error" ? Math.max(ms, 7000) : ms);
  }, [dismiss]);

  const api = useRef(null);
  if (!api.current) api.current = {};
  api.current.success = (m, ms) => push("success", m, ms);
  api.current.error = (m, ms) => push("error", m, ms);
  api.current.info = (m, ms) => push("info", m, ms);

  return (
    <Ctx.Provider value={api.current}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((x) => (
          <div key={x.id} className={`toast toast-${x.kind}`} role={x.kind === "error" ? "alert" : "status"}
               data-testid="toast">
            <span>{x.message}</span>
            <button className="toast-x" onClick={() => dismiss(x.id)} aria-label="×">×</button>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export const useToast = () => useContext(Ctx);
