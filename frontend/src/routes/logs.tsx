import {createFileRoute} from '@tanstack/react-router';
import {useEffect, useMemo, useRef, useState} from 'react';

import {useLogStream} from '../hooks/useLogStream';
import type {LogLevel, LogRecord} from '../lib/types';

export const Route = createFileRoute('/logs')({
  component: LogsPage,
});

type LevelFilter = 'ALL' | 'DEBUG' | 'INFO' | 'WARN+' | 'ERROR+';

const LEVEL_ORDER: LogLevel[] = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

function levelMinIndex(filter: LevelFilter): number {
  if (filter === 'ALL' || filter === 'DEBUG') return 0;
  if (filter === 'INFO') return 1;
  if (filter === 'WARN+') return 2;
  return 3; // ERROR+
}

function LogsPage() {
  const [levelFilter, setLevelFilter] = useState<LevelFilter>('ALL');
  const [loggerFilter, setLoggerFilter] = useState<string>('ALL');
  const [search, setSearch] = useState('');
  const [paused, setPaused] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [cutoff, setCutoff] = useState<string | null>(null);

  const {logs, connected, error} = useLogStream(true);

  const [frozen, setFrozen] = useState<LogRecord[] | null>(null);
  useEffect(() => {
    if (paused && frozen === null) setFrozen(logs);
    if (!paused && frozen !== null) setFrozen(null);
  }, [paused, frozen, logs]);
  const source = frozen ?? logs;

  const seenLoggers = useMemo(
    () => Array.from(new Set(logs.map((r) => r.logger))).sort(),
    [logs],
  );

  const visible = useMemo(() => {
    const minIdx = levelMinIndex(levelFilter);
    const needle = search.trim().toLowerCase();
    return source.filter((r) => {
      if (cutoff && r.ts <= cutoff) return false;
      if (loggerFilter !== 'ALL' && r.logger !== loggerFilter) return false;
      const lvlIdx = LEVEL_ORDER.indexOf(r.level);
      if (lvlIdx >= 0 && lvlIdx < minIdx) return false;
      if (needle) {
        const blob = `${r.logger} ${r.message}`.toLowerCase();
        if (!blob.includes(needle)) return false;
      }
      return true;
    });
  }, [source, cutoff, loggerFilter, levelFilter, search]);

  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (autoScroll && !paused) {
      bottomRef.current?.scrollIntoView({block: 'end'});
    }
  }, [visible.length, autoScroll, paused]);

  return (
    <div className="page logs-page">
      <header className="logs-header">
        <div className="logs-title-row">
          <h1>Logs</h1>
          <span className={`logs-status ${connected ? 'logs-status--ok' : 'logs-status--err'}`}>
            {connected ? 'streaming' : error ? `disconnected — ${error}` : 'connecting…'}
          </span>
        </div>
        <div className="logs-toolbar">
          <label className="logs-field">
            <span>Level</span>
            <select value={levelFilter} onChange={(e) => setLevelFilter(e.target.value as LevelFilter)}>
              <option value="ALL">All</option>
              <option value="DEBUG">DEBUG+</option>
              <option value="INFO">INFO+</option>
              <option value="WARN+">WARN+</option>
              <option value="ERROR+">ERROR+</option>
            </select>
          </label>
          <label className="logs-field">
            <span>Logger</span>
            <select value={loggerFilter} onChange={(e) => setLoggerFilter(e.target.value)}>
              <option value="ALL">All</option>
              {seenLoggers.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label className="logs-field logs-field--grow">
            <span>Search</span>
            <input
              type="text"
              placeholder="substring of logger or message"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <button
            type="button"
            className={`logs-btn ${paused ? 'logs-btn--active' : ''}`}
            onClick={() => setPaused((p) => !p)}
            title="Pause keeps the SSE connection open in the background; resume catches up from the buffer"
          >
            {paused ? 'Resume' : 'Pause'}
          </button>
          <button
            type="button"
            className={`logs-btn ${autoScroll ? 'logs-btn--active' : ''}`}
            onClick={() => setAutoScroll((s) => !s)}
          >
            Auto-scroll
          </button>
          <button
            type="button"
            className="logs-btn"
            onClick={() => setCutoff(new Date().toISOString())}
          >
            Clear
          </button>
        </div>
      </header>

      <div className="logs-body">
        {visible.length === 0 ? (
          <div className="logs-empty">No log lines match your filters.</div>
        ) : (
          <ul className="logs-list">
            {visible.map((r, i) => (
              <li key={`${r.ts}-${i}`} className={`logs-row logs-row--${r.level.toLowerCase()}`}>
                <span className="logs-cell logs-cell--ts">{r.ts}</span>
                <span className="logs-cell logs-cell--level">{r.level}</span>
                <span className="logs-cell logs-cell--logger">{r.logger}</span>
                <span className="logs-cell logs-cell--msg">{r.message}</span>
              </li>
            ))}
          </ul>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
