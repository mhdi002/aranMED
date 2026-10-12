import { useEffect, useRef } from "react";

/**
 * Run *fn* now and every *intervalMs* while the page is visible.
 *
 * Polling a board every few seconds from a background tab wastes the
 * server's time and the user's battery; this pauses while the tab is hidden
 * and refreshes immediately when it comes back.
 */
export default function usePolling(fn, intervalMs, enabled = true) {
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return undefined;
    let timer = null;
    const tick = () => { fnRef.current(); };
    const start = () => {
      if (timer) return;
      tick();
      timer = setInterval(tick, intervalMs);
    };
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    const onVis = () => (document.visibilityState === "hidden" ? stop() : start());
    if (typeof document === "undefined" || document.visibilityState !== "hidden") start();
    document.addEventListener("visibilitychange", onVis);
    return () => { stop(); document.removeEventListener("visibilitychange", onVis); };
  }, [intervalMs, enabled]);
}
