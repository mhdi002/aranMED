/** Shared card surface — wraps .card + optional .card-head (tokens.css). */
export function Card({ children, className = "", ...rest }) {
  return (
    <section className={`card ${className}`.trim()} {...rest}>
      {children}
    </section>
  );
}

export function CardHead({ step, title, hint, right, children }) {
  return (
    <div className="card-head">
      {step != null && <span className="step">{step}</span>}
      {title && <h2>{title}</h2>}
      {children}
      {hint && !right && <span className="muted">{hint}</span>}
      {right}
    </div>
  );
}

export default Card;
