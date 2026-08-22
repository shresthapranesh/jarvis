/**
 * Pure config <-> form transforms for the MCP server editor.
 *
 * Kept free of React so the round-trip (stored connection JSON -> form fields ->
 * connection JSON) can be reasoned about — and tested — on its own. The server
 * treats every non-`transport` key as a transport constructor kwarg, so what
 * `formToConfigJson` emits has to stay a valid connection dict.
 */

export type McpTransport = 'stdio' | 'http' | 'sse' | 'streamable-http';

export interface McpFormState {
  name: string;
  transport: McpTransport;
  command: string;
  args: string[];
  env: {k: string; v: string}[];
  url: string;
  headers: {k: string; v: string}[];
  advancedJson: string;
  useAdvanced: boolean;
}

export interface McpPreset {
  id: string;
  name: string;
  desc: string;
  config: any;
}

export const MCP_PRESETS: McpPreset[] = [
  {
    id: 'filesystem',
    name: 'Filesystem',
    desc: 'Read/write local files',
    config: {
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
      transport: 'stdio',
    },
  },
  {
    id: 'brave',
    name: 'Brave Search',
    desc: 'Web search via Brave',
    config: {
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-brave-search'],
      transport: 'stdio',
      env: {BRAVE_API_KEY: ''},
    },
  },
  {
    id: 'github',
    name: 'GitHub',
    desc: 'Repos, issues, PRs',
    config: {
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-github'],
      transport: 'stdio',
      env: {GITHUB_PERSONAL_ACCESS_TOKEN: ''},
    },
  },
  {
    id: 'postgres',
    name: 'Postgres',
    desc: 'Query PostgreSQL',
    config: {
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-postgres', 'postgresql://user:pass@localhost/db'],
      transport: 'stdio',
    },
  },
  {
    id: 'fetch',
    name: 'Fetch',
    desc: 'HTTP fetch & extract',
    config: {
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-fetch'],
      transport: 'stdio',
    },
  },
  {
    id: 'puppeteer',
    name: 'Puppeteer',
    desc: 'Headless browser',
    config: {
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-puppeteer'],
      transport: 'stdio',
    },
  },
  {
    id: 'memory',
    name: 'Memory',
    desc: 'Knowledge graph',
    config: {
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-memory'],
      transport: 'stdio',
    },
  },
  {
    id: 'sqlite',
    name: 'SQLite',
    desc: 'Local SQLite DB',
    config: {
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-sqlite', '--db-path', '/tmp/db.sqlite'],
      transport: 'stdio',
    },
  },
];

export const DEFAULT_ADVANCED_JSON =
  '{\n  "command": "npx",\n  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],\n  "transport": "stdio"\n}';

export function emptyForm(): McpFormState {
  return {
    name: '',
    transport: 'stdio',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
    env: [],
    url: '',
    headers: [],
    advancedJson: DEFAULT_ADVANCED_JSON,
    useAdvanced: false,
  };
}

export function configToForm(name: string, rawJson: string): McpFormState {
  let cfg: any = {};
  try {
    cfg = JSON.parse(rawJson || '{}');
  } catch {
    cfg = {};
  }
  const transport = (cfg.transport as McpTransport) || (cfg.url ? 'http' : 'stdio');
  let args: string[] = [];
  if (Array.isArray(cfg.args)) args = cfg.args;
  else if (typeof cfg.args === 'string') args = [cfg.args];
  const envObj = cfg.env && typeof cfg.env === 'object' ? cfg.env : {};
  const headersObj = cfg.headers && typeof cfg.headers === 'object' ? cfg.headers : {};
  return {
    name: name || '',
    transport,
    command: cfg.command
      ? Array.isArray(cfg.command)
        ? cfg.command.join(' ')
        : cfg.command
      : 'npx',
    args,
    env: Object.entries(envObj).map(([k, v]) => ({k, v: String(v)})),
    url: cfg.url || '',
    headers: Object.entries(headersObj).map(([k, v]) => ({k, v: String(v)})),
    advancedJson: (() => {
      try {
        return JSON.stringify(cfg, null, 2);
      } catch {
        return rawJson;
      }
    })(),
    useAdvanced: false,
  };
}

export function formToConfigJson(form: McpFormState): string {
  if (form.useAdvanced) return form.advancedJson;
  const out: any = {};
  out.transport = form.transport;
  if (form.transport === 'stdio') {
    const cmd = form.command.trim().split(/\s+/).filter(Boolean);
    const extraArgs = form.args.filter((a) => a.trim());
    if (cmd.length >= 1) out.command = cmd[0];
    const allArgs = [...cmd.slice(1), ...extraArgs];
    if (allArgs.length) out.args = allArgs;
    const env: Record<string, string> = {};
    form.env.forEach(({k, v}) => {
      if (k.trim()) env[k.trim()] = v;
    });
    if (Object.keys(env).length) out.env = env;
  } else {
    out.url = form.url.trim();
    const headers: Record<string, string> = {};
    form.headers.forEach(({k, v}) => {
      if (k.trim()) headers[k.trim()] = v;
    });
    if (Object.keys(headers).length) out.headers = headers;
  }
  return JSON.stringify(out);
}

export function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}
