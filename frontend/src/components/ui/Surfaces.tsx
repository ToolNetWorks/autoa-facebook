import { Children, cloneElement, isValidElement, type ReactElement, type ReactNode } from 'react';

export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: 'neutral' | 'ok' | 'warn' | 'bad' | 'info';
}) {
  return <span className={`badge ${tone === 'neutral' ? '' : tone}`.trim()}>{children}</span>;
}

export function Card({
  children,
  className = '',
  title,
  description,
  actions,
}: {
  children?: ReactNode;
  className?: string;
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className={`card ${className}`.trim()}>
      {title || actions ? (
        <div className="card-head">
          <div>
            {title ? <h2>{title}</h2> : null}
            {description ? <p>{description}</p> : null}
          </div>
          {actions ? <div className="btn-row">{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  muted,
  onClick,
}: {
  label: ReactNode;
  value: ReactNode;
  muted?: boolean;
  onClick?: () => void;
}) {
  return (
    <div
      className={`stat ${muted ? 'muted' : ''} ${onClick ? 'stat-clickable' : ''}`}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
    >
      <span>{label}</span>
      <strong className={muted ? 'muted' : undefined}>{value}</strong>
    </div>
  );
}

export function Skeleton({
  variant = 'text',
  width,
}: {
  variant?: 'text' | 'title' | 'block';
  width?: string;
}) {
  return <div className={`skeleton ${variant}`} style={width ? { width } : undefined} />;
}

export function Progress({ value, indeterminate }: { value?: number; indeterminate?: boolean }) {
  const percent = typeof value === 'number' ? Math.max(0, Math.min(100, value)) : 0;
  return (
    <div
      className={indeterminate ? 'progress indeterminate' : 'progress'}
      role="progressbar"
      aria-valuemin={indeterminate ? undefined : 0}
      aria-valuemax={indeterminate ? undefined : 100}
      aria-valuenow={indeterminate ? undefined : percent}
    >
      <i style={indeterminate ? undefined : { width: `${percent}%` }} />
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="state">
      <h3>{title}</h3>
      {description ? <p className="hint">{description}</p> : null}
      {action ? <div className="btn-row" style={{ marginTop: 'var(--sp-2)' }}>{action}</div> : null}
    </div>
  );
}

export function ErrorState({ title, description, action }: { title: string; description?: ReactNode; action?: ReactNode }) {
  return (
    <div className="state bad">
      <h3>{title}</h3>
      {description ? <p className="hint">{description}</p> : null}
      {action ? <div className="btn-row" style={{ marginTop: 'var(--sp-2)' }}>{action}</div> : null}
    </div>
  );
}

export function Alert({
  tone = 'info',
  title,
  children,
}: {
  tone?: 'info' | 'warn' | 'bad';
  title?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={`alert ${tone === 'info' ? '' : tone}`.trim()}>
      <div>
        {title ? <b>{title}</b> : null}
        {title && children ? ' ' : null}
        {children}
      </div>
    </div>
  );
}

export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: string; label: string; count?: number }[];
  active: string;
  onChange: (id: string) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          aria-selected={tab.id === active}
          className={tab.id === active ? 'tab active' : 'tab'}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
          {tab.count !== undefined ? ` (${tab.count})` : ''}
        </button>
      ))}
    </div>
  );
}

export function DataTable({ head, children }: { head: string[]; children: ReactNode }) {
  const rows = Children.toArray(children).map((row) =>
    isValidElement(row)
      ? cloneElement(row as ReactElement<{ children?: ReactNode }>, {
          children: Children.toArray((row as ReactElement<{ children?: ReactNode }>).props.children).map((cell, index) =>
            isValidElement(cell)
              ? cloneElement(cell as ReactElement<Record<string, unknown>>, { 'data-label': head[index] ?? '' })
              : cell,
          ),
        })
      : row,
  );

  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            {head.map((cell) => (
              <th key={cell}>{cell}</th>
            ))}
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  );
}
