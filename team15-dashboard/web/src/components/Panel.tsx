import type { ReactNode } from 'react'

export function Panel({ title, detail, action, children, className = '' }: {
  title: string; detail?: string; action?: ReactNode; children: ReactNode; className?: string
}) {
  return <section className={`panel ${className}`}>
    <header className="panel-head"><div><h2>{title}</h2>{detail && <p>{detail}</p>}</div>{action}</header>
    {children}
  </section>
}

export function Stat({ label, value, note, tone = 'neutral' }: { label: string; value: ReactNode; note?: string; tone?: 'neutral' | 'good' | 'warn' | 'bad' }) {
  return <article className={`stat stat-${tone}`}><span>{label}</span><strong>{value}</strong>{note && <small>{note}</small>}</article>
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty-state">{children}</div>
}

export function SourceTag({ label, status }: { label: string; status?: string }) {
  return <span className={`source-tag source-${status || 'unknown'}`}><i />{label}</span>
}
