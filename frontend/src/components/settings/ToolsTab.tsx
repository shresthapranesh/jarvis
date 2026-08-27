import * as stylex from '@stylexjs/stylex';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {ToolsQuery as TToolsQuery} from '../../__generated__/ToolsQuery.graphql';
import {useToast} from '../../lib/toast';
import {commitSetToolPolicy} from '../../relay/SetToolPolicyMutation';
import {toolsQuery} from '../../relay/ToolsQuery';
import {SearchIcon} from '../icons';
import {useQueryRetry} from '../QueryBoundary';
import {badge, field, page} from '../ui';
// `tools` is also a local variable name in this tab.
import {settings as sx, tools as toolStyles} from './settings.styles';

type Tool = TToolsQuery['response']['tools'][number];

const KIND_LABEL: Record<string, string> = {
  bound: 'Agent tools',
  sdk: 'jarvis SDK',
  mcp: 'MCP',
};

// What each family costs and how it is reached — the thing a tool's row cannot
// say on its own, and the reason the page groups by kind at all.
const KIND_BLURB: Record<string, string> = {
  bound: 'Coupled to the agent graph. Their schemas are sent to the model on every LLM call.',
  sdk: 'Python functions inside run_cell, discovered on demand. They cost nothing until used.',
  mcp: 'Tools from external MCP servers. Bound ones cost tokens per call; on-demand ones do not.',
};

export function ToolsTab() {
  const toast = useToast();
  const data = useLazyLoadQuery<TToolsQuery>(
    toolsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );

  const [filter, setFilter] = useState('');
  const [kindFilter, setKindFilter] = useState('all');
  const [pending, setPending] = useState<string | null>(null);

  const tools = data.tools;
  const gated = tools.filter((t) => t.requiresApproval).length;
  const off = tools.filter((t) => !t.enabled).length;

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return tools.filter((t) => {
      if (kindFilter !== 'all' && t.kind !== kindFilter) return false;
      if (!q) return true;
      return (
        t.name.toLowerCase().includes(q) ||
        t.group.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q)
      );
    });
  }, [tools, filter, kindFilter]);

  // Group by kind, then by the tool's own group (MCP server / SDK category), so
  // an MCP server reads as a unit instead of scattering through one flat list.
  const groups = useMemo(() => {
    const out = new Map<string, Map<string, Tool[]>>();
    for (const t of filtered) {
      if (!out.has(t.kind)) out.set(t.kind, new Map());
      const byGroup = out.get(t.kind)!;
      if (!byGroup.has(t.group)) byGroup.set(t.group, []);
      byGroup.get(t.group)!.push(t);
    }
    return out;
  }, [filtered]);

  async function update(tool: Tool, change: {enabled?: boolean; requiresApproval?: boolean}) {
    setPending(tool.key);
    try {
      await commitSetToolPolicy({
        key: tool.key,
        enabled: change.enabled ?? null,
        requiresApproval: change.requiresApproval ?? null,
      });
    } catch (e) {
      toast.push((e as Error).message || String(e), 'error');
    } finally {
      setPending(null);
    }
  }

  return (
    <div {...stylex.props(page.section)}>
      <h2 {...stylex.props(page.sectionTitle)}>
        Tools <span {...stylex.props(page.count)}>{tools.length}</span>
        <span {...stylex.props(page.sectionHint)}>
          {off} off · {gated} need approval
        </span>
      </h2>

      <div {...stylex.props(sx.filterRow)}>
        <div {...stylex.props(sx.search)}>
          <SearchIcon size={14} />
          <input
            placeholder="Search tools…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <select
          {...stylex.props(field.select, sx.filterSelect)}
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
        >
          <option value="all">All kinds</option>
          <option value="bound">Agent tools</option>
          <option value="sdk">jarvis SDK</option>
          <option value="mcp">MCP</option>
        </select>
      </div>

      {filtered.length === 0 ? (
        <div {...stylex.props(page.empty)}>No tools match the filter.</div>
      ) : (
        [...groups.entries()].map(([kind, byGroup]) => (
          <section key={kind} {...stylex.props(toolStyles.kind)}>
            <h3 {...stylex.props(toolStyles.kindTitle)}>
              {KIND_LABEL[kind] ?? kind}
              <span {...stylex.props(toolStyles.kindBlurb)}>{KIND_BLURB[kind]}</span>
            </h3>
            {[...byGroup.entries()].map(([group, rows]) => (
              <div key={group} {...stylex.props(toolStyles.group)}>
                {kind !== 'bound' && <div {...stylex.props(toolStyles.groupName)}>{group}</div>}
                <ul {...stylex.props(toolStyles.list)}>
                  {rows.map((tool) => (
                    <ToolRow
                      key={tool.key}
                      tool={tool}
                      busy={pending === tool.key}
                      onChange={(change) => void update(tool, change)}
                    />
                  ))}
                </ul>
              </div>
            ))}
          </section>
        ))
      )}
    </div>
  );
}

function ToolRow({
  tool,
  busy,
  onChange,
}: {
  tool: Tool;
  busy: boolean;
  onChange: (change: {enabled?: boolean; requiresApproval?: boolean}) => void;
}) {
  return (
    <li {...stylex.props(toolStyles.row, !tool.enabled && toolStyles.rowOff)}>
      <div {...stylex.props(toolStyles.rowMain)}>
        <div {...stylex.props(toolStyles.rowHead)}>
          <span {...stylex.props(toolStyles.rowName)}>{tool.name}</span>
          {tool.inPrompt && (
            <span
              {...stylex.props(badge.base)}
              title="This tool's schema is sent to the model on every LLM call, used or not."
            >
              in prompt
            </span>
          )}
          {!tool.available && (
            <span {...stylex.props(badge.base)} title={tool.detail || 'Not currently reachable'}>
              {tool.detail || 'unavailable'}
            </span>
          )}
          {tool.available && tool.detail && (
            <span {...stylex.props(badge.base)}>{tool.detail}</span>
          )}
          {tool.requiresApproval && <span {...stylex.props(badge.base, badge.live)}>approval</span>}
        </div>
        {tool.description && <p {...stylex.props(toolStyles.rowDesc)}>{tool.description}</p>}
      </div>
      <div {...stylex.props(toolStyles.rowControls)}>
        <label {...stylex.props(toolStyles.toggle)} title="Let the agent use this tool at all">
          <input
            type="checkbox"
            checked={tool.enabled}
            disabled={busy}
            onChange={(e) => onChange({enabled: e.target.checked})}
          />
          <span>Enabled</span>
        </label>
        <label
          {...stylex.props(toolStyles.toggle)}
          title="Every call blocks until a human approves it, wherever it runs — chat, board task, automation or the SDK."
        >
          <input
            type="checkbox"
            checked={tool.requiresApproval}
            disabled={busy || !tool.enabled}
            onChange={(e) => onChange({requiresApproval: e.target.checked})}
          />
          <span>Ask first</span>
        </label>
      </div>
    </li>
  );
}
