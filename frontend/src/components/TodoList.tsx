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
  return (
    <div className={`todo-card${compact ? ' todo-card--compact' : ''}`}>
      <div className="todo-card-header">
        <span className="todo-card-title">Plan</span>
        <span className="todo-card-progress">
          {doneCount}/{todos.length}
        </span>
      </div>
      <ul className="todo-list">
        {todos.map((t, i) => (
          <li key={i} className={`todo-item todo-item--${t.status}`}>
            <span className="todo-glyph" aria-hidden>
              {STATUS_GLYPH[t.status]}
            </span>
            <span className="todo-text">{t.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
