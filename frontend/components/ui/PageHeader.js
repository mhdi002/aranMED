/** Shared page title/subtitle block — wraps .page-head/.page-title/.page-sub. */
export default function PageHeader({ title, subtitle, actions }) {
  return (
    <div className="page-head row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
      <div>
        <h2 className="page-title">{title}</h2>
        {subtitle && <p className="page-sub">{subtitle}</p>}
      </div>
      {actions}
    </div>
  );
}
