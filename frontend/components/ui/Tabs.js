import { useId, useRef } from "react";

/**
 * Accessible tabs (WAI-ARIA tabs pattern): roving focus with arrow keys,
 * Home/End, aria-selected/aria-controls, and a labelled tabpanel.
 *
 *   <Tabs tabs={[{ id, label, testId? }]} value={id} onChange={setId}
 *         label="EHR sections">{panel for value}</Tabs>
 */
export default function Tabs({ tabs, value, onChange, label, children, className = "", tabAttr }) {
  const base = useId().replace(/:/g, "");
  const refs = useRef({});
  const idx = Math.max(0, tabs.findIndex((t) => t.id === value));

  function onKey(e) {
    const rtl = typeof document !== "undefined" && document.documentElement.dir === "rtl";
    const next = { ArrowRight: rtl ? -1 : 1, ArrowLeft: rtl ? 1 : -1 }[e.key];
    let to = null;
    if (next) to = (idx + next + tabs.length) % tabs.length;
    if (e.key === "Home") to = 0;
    if (e.key === "End") to = tabs.length - 1;
    if (to === null) return;
    e.preventDefault();
    onChange(tabs[to].id);
    refs.current[tabs[to].id]?.focus();
  }

  return (
    <>
      <div className={className || "tabs"} role="tablist" aria-label={label}>
        {tabs.map((t) => {
          const selected = t.id === value;
          return (
            <button
              key={t.id}
              ref={(el) => { refs.current[t.id] = el; }}
              role="tab"
              id={`${base}-tab-${t.id}`}
              aria-selected={selected}
              aria-controls={`${base}-panel`}
              tabIndex={selected ? 0 : -1}
              className={`tab ${selected ? "active" : ""}`}
              onClick={() => onChange(t.id)}
              onKeyDown={onKey}
              data-testid={t.testId}
              {...(tabAttr ? { [tabAttr]: t.id } : {})}
            >
              {t.label}
              {t.badge != null && t.badge !== 0 && <span className="tab-badge">{t.badge}</span>}
            </button>
          );
        })}
      </div>
      <div role="tabpanel" id={`${base}-panel`} aria-labelledby={`${base}-tab-${tabs[idx]?.id}`}>
        {children}
      </div>
    </>
  );
}
