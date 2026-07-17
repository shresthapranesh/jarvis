import type {TodoItem} from '../lib/types';

interface Props {
  todos: TodoItem[];
  compact?: boolean;
}

const STATUS_GLYPH: Record<TodoItem['status'], string> = {
  pending: '○',
  in_progress: '◐',
  done: '●',
};

export function TodoList({todos, compact = false}: Props) {
  if (!todos || todos.length === 0) return null;

  const doneCount = todos.filter((t) => t.status === 'done').length;
  const inProgress = todos.findIndex(t => t.status === 'in_progress');
  const pct = todos.length ? Math.round((doneCount / todos.length) * 100) : 0;

  return (
    <div className={`todo-card${compact ? ' todo-card--compact' : ''}`}>
      <div className="todo-card-header">
        <span className="todo-card-title">📋 Plan {inProgress >= 0 ? `· step ${inProgress+1}` : ''}</span>
        <span className="todo-card-progress">
          {doneCount}/{todos.length}
        </span>
      </div>
      {!compact && (
        <div style={{height:3, background:'var(--surface2)', borderRadius:3, overflow:'hidden', margin:'0 12px 8px'}}>
          <div style={{width:`${pct}%`, height:'100%', background: pct === 100 ? 'var(--accent)' : 'var(--text)', transition:'width 0.3s'}} />
        </div>
      )}
      <ul className="todo-list">
        {todos.map((t, i) => (
          <li key={i} className={`todo-item todo-item--${t.status}`} style={{opacity: t.status === 'pending' && i > inProgress && inProgress !== -1 ? 0.7 : 1}}>
            <span className="todo-glyph" aria-hidden>
              {STATUS_GLYPH[t.status]}
            </span>
            <span className="todo-text">{t.text}</span>
            {t.status === 'in_progress' && <span className="live-dot" style={{marginLeft:6}} />}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function PlanningEmpty({reason}: {reason?: string}) {
  return (
    <div style={{padding:'8px 12px', background:'var(--surface)', border:'1px dashed var(--border)', borderRadius:8, fontSize:'0.82rem', color:'var(--text-dim)'}}>
      <div>🧠 Planning… {reason || 'Complex task detected, generating plan'}</div>
      <div style={{height:3, background:'var(--surface2)', borderRadius:3, marginTop:6, overflow:'hidden'}}>
        <div style={{width:'60%', height:'100%', background:'var(--accent)', animation:'pulse 1.2s infinite'}} />
      </div>
    </div>
  );
}
