import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {createFileRoute} from '@tanstack/react-router';
import {useEffect, useMemo, useState} from 'react';

import type {NotificationChannelInput} from '../lib/api';
import {commitCreateNotificationChannel} from '../relay/CreateNotificationChannelMutation';
import {commitDeleteNotificationChannel} from '../relay/DeleteNotificationChannelMutation';
import {fetchNotificationChannels} from '../relay/NotificationChannelsQuery';
import {commitUpdateNotificationChannel} from '../relay/UpdateNotificationChannelMutation';
import {useToast} from '../lib/toast';
import type {
  NotificationChannel,
  NotificationChannelReference,
  NotificationChannelType,
} from '../lib/types';

export const Route = createFileRoute('/settings')({component: SettingsPage});

type SettingsTab = 'notifications' | 'mcp' | 'models';

function SettingsPage() {
  const [tab, setTab] = useState<SettingsTab>(() => {
    const saved = localStorage.getItem('settings-tab') as SettingsTab | null;
    if (saved && ['notifications','mcp','models'].includes(saved)) return saved;
    return 'mcp';
  });

  useEffect(() => {
    localStorage.setItem('settings-tab', tab);
  }, [tab]);

  const {data: notifCountData} = useQuery({
    queryKey: ['notification-channels'],
    queryFn: fetchNotificationChannels,
  });

  const {data: mcpData} = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: async () => {
      const {fetchMcpServers} = await import('../relay/McpServersQuery');
      return fetchMcpServers();
    },
  });

  const {data: modelsData} = useQuery({
    queryKey: ['models-count'],
    queryFn: async () => {
      const {fetchQuery} = await import('relay-runtime');
      const {graphql} = await import('react-relay');
      const {environment} = await import('../relay/environment');
      const q = graphql`
        query settingsModelsCountQuery {
          models {
            default
            available {
              id
              label
              provider
            }
          }
        }
      `;
      const res: any = await (fetchQuery as any)(environment, q, {}, {fetchPolicy: 'store-or-network'}).toPromise();
      return res?.models;
    },
  });
  const modelsCount = (modelsData as any)?.available?.length ?? 0;

  return (
    <div className="settings-page">
      <div className="settings-header">
        <h1>Settings</h1>
        <div className="settings-header-sub">
          {tab === 'notifications' && 'Define notification channels once here, then pick them by name when configuring automations or workflows.'}
          {tab === 'mcp' && 'Connect external tools via Model Context Protocol. Servers run as subprocesses or HTTP endpoints and expose tools to the agent.'}
          {tab === 'models' && 'Available language models from all providers. Add custom models via CLI or pick defaults here.'}
        </div>
      </div>

      <div className="settings-layout">
        <nav className="settings-nav">
          <div className="settings-nav-label">Workspace</div>
          <button className={`settings-nav-item ${tab==='notifications'?'settings-nav-item--active':''}`} onClick={()=>setTab('notifications')}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-6 9-6 9h16s-6-2-6-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
            Notifications
            <span className="settings-nav-count">{notifCountData?.length ?? 0}</span>
          </button>
          <button className={`settings-nav-item ${tab==='mcp'?'settings-nav-item--active':''}`} onClick={()=>setTab('mcp')}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
            MCP Servers
            <span className="settings-nav-count">{mcpData?.servers?.length ?? 0}</span>
          </button>

          <div className="settings-nav-label" style={{marginTop:12}}>System</div>
          <button className={`settings-nav-item ${tab==='models'?'settings-nav-item--active':''}`} onClick={()=>setTab('models')}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2a10 10 0 1 0 10 10H12V2z"/><path d="M12 2a10 10 0 0 1 10 10"/><path d="M12 12L2.69 8.5"/><path d="M12 12v10"/></svg>
            Models
            <span className="settings-nav-count">{modelsCount}</span>
          </button>
        </nav>

        <div className="settings-content">
          {tab === 'notifications' && <NotificationsTab />}
          {tab === 'mcp' && <McpTab />}
          {tab === 'models' && <ModelsTab />}
        </div>
      </div>
    </div>
  );
}

/* ── Notifications Tab (polished) ─────────────────────────────────────── */

function NotificationsTab() {
  const queryClient = useQueryClient();
  const {data: channels = [], isLoading} = useQuery({
    queryKey: ['notification-channels'],
    queryFn: fetchNotificationChannels,
  });
  const [draft, setDraft] = useState<NotificationChannelInput | null>(null);
  const toast = useToast();

  function refresh() {
    queryClient.invalidateQueries({queryKey: ['notification-channels']});
  }

  return (
    <div style={{display:'flex', flexDirection:'column', gap:18, maxWidth:780}}>
      <div className="settings-card">
        <div className="settings-card-header">
          <div>
            <div className="settings-card-title">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-6 9-6 9h16s-6-2-6-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
              Notification Channels
            </div>
            <div className="settings-card-desc">Telegram and Discord targets for automation/workflow outcomes. Referenced by name in automations.</div>
          </div>
          <button className="mcp-btn mcp-btn--primary" onClick={()=>setDraft({name:'', type:'telegram', target:''})} disabled={draft!==null}>
            + New channel
          </button>
        </div>

        <div className="settings-card-body">
          {isLoading && <div style={{color:'var(--text-dim)', fontSize:'0.85rem'}}>Loading channels…</div>}

          {!isLoading && channels.length===0 && draft===null && (
            <div className="notif-empty">
              <div style={{width:44,height:44, borderRadius:10, background:'var(--surface2)', border:'1px solid var(--border)', display:'flex', alignItems:'center', justifyContent:'center'}}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--text-dim)" strokeWidth="1.8"><path d="M18 8A6 6 0 0 0 6 8c0 7-6 9-6 9h16s-6-2-6-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
              </div>
              <div>
                <div style={{fontWeight:600, color:'var(--text)', marginBottom:4}}>No channels yet</div>
                <div style={{fontSize:'0.8rem'}}>Create a channel to get notified when automations finish, fail, or detect changes.</div>
              </div>
              <button className="mcp-btn" onClick={()=>setDraft({name:'', type:'telegram', target:''})}>+ Create first channel</button>
            </div>
          )}

          <div className="notif-grid">
            {channels.map(ch => (
              <ChannelRow key={ch.id} channel={ch} onChanged={refresh} />
            ))}
            {draft && (
              <DraftRow draft={draft} onChange={setDraft} onCancel={()=>setDraft(null)} onSaved={()=>{ setDraft(null); refresh(); toast.push('Channel created','success'); }} />
            )}
          </div>
        </div>
      </div>

      <div style={{fontSize:'0.76rem', color:'var(--text-faint)', lineHeight:1.5, padding:'0 4px'}}>
        Tip: Use <code style={{background:'var(--surface2)', border:'1px solid var(--border)', padding:'1px 5px', borderRadius:4, fontSize:'0.72rem'}}>Telegram chat ID</code> from @userinfobot or Discord channel ID (enable Developer Mode → right-click channel → Copy ID).
      </div>
    </div>
  );
}

/* ── Models Tab ───────────────────────────────────────────────────────── */

function ModelsTab() {
  const {data, isLoading} = useQuery({
    queryKey: ['models-catalog'],
    queryFn: async () => {
      const {fetchQuery} = await import('relay-runtime');
      const {graphql} = await import('react-relay');
      const {environment} = await import('../relay/environment');
      const q = graphql`
        query settingsModelsQuery {
          models {
            default
            available {
              id
              label
              provider
            }
          }
        }
      `;
      const res: any = await (fetchQuery as any)(environment, q, {}, {fetchPolicy: 'network-only'}).toPromise();
      return res?.models as {default:string; available:{id:string; label:string; provider:string}[]} | undefined;
    },
  });

  const [filter, setFilter] = useState('');
  const [providerFilter, setProviderFilter] = useState<string>('all');

  const providers = useMemo(() => {
    const set = new Set<string>();
    data?.available?.forEach(m => set.add(m.provider));
    return Array.from(set).sort();
  }, [data]);

  const filtered = useMemo(() => {
    const q = filter.toLowerCase().trim();
    return (data?.available ?? []).filter(m => {
      if (providerFilter !== 'all' && m.provider !== providerFilter) return false;
      if (!q) return true;
      return m.id.toLowerCase().includes(q) || m.label.toLowerCase().includes(q) || m.provider.toLowerCase().includes(q);
    });
  }, [data, filter, providerFilter]);

  return (
    <div style={{display:'flex', flexDirection:'column', gap:16, maxWidth:900}}>
      <div className="settings-card">
        <div className="settings-card-header">
          <div>
            <div className="settings-card-title">Model Catalog</div>
            <div className="settings-card-desc">{isLoading ? 'Loading…' : `${data?.available?.length ?? 0} models available.`} Default: <code style={{fontFamily:'var(--font-mono)', fontSize:'0.78rem', background:'var(--surface2)', padding:'2px 6px', borderRadius:4}}>{data?.default ?? '—'}</code></div>
          </div>
        </div>
        <div className="settings-card-body" style={{display:'flex', flexDirection:'column', gap:12}}>
          <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
            <div className="mcp-search" style={{flex:'1 1 220px'}}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
              <input placeholder="Search models…" value={filter} onChange={e=>setFilter(e.target.value)} />
              {filter && <button className="mcp-btn mcp-btn--ghost" style={{padding:'2px 6px', fontSize:'0.7rem'}} onClick={()=>setFilter('')}>✕</button>}
            </div>
            <select className="mcp-form-select" style={{flex:'0 0 150px'}} value={providerFilter} onChange={e=>setProviderFilter(e.target.value)}>
              <option value="all">All providers</option>
              {providers.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>

          <div className="settings-models-grid">
            {filtered.map(m => (
              <div key={m.id} className="model-card">
                <div className="model-card-provider">{m.provider}</div>
                <div className="model-card-id">{m.id}</div>
                <div className="model-card-label">{m.label}</div>
                {m.id === data?.default && <span className="mcp-badge mcp-badge--live" style={{width:'fit-content'}}>default</span>}
              </div>
            ))}
          </div>
          {filtered.length===0 && !isLoading && <div style={{color:'var(--text-dim)', fontSize:'0.85rem', textAlign:'center', padding:20}}>No models match filter.</div>}
          {isLoading && <div style={{color:'var(--text-dim)', fontSize:'0.85rem', textAlign:'center', padding:20}}>Loading models…</div>}
        </div>
      </div>

      <div className="settings-card">
        <div className="settings-card-body" style={{fontSize:'0.8rem', color:'var(--text-dim)', lineHeight:1.6}}>
          <div style={{fontWeight:600, color:'var(--text)', marginBottom:6}}>How to add custom models</div>
          <div>Run in terminal: <code style={{fontFamily:'var(--font-mono)', background:'var(--surface2)', padding:'2px 6px', borderRadius:4, border:'1px solid var(--border)'}}>uv run python main.py model add provider:model_id "Label"</code></div>
          <div style={{marginTop:6}}>Example: <code style={{fontFamily:'var(--font-mono)', background:'var(--surface2)', padding:'2px 6px', borderRadius:4}}>uv run python main.py model add ollama:llama3.1 "Llama 3.1 (local)"</code></div>
          <div style={{marginTop:8, fontSize:'0.75rem', color:'var(--text-faint)'}}>Supported providers: ollama, google_genai, bedrock, anthropic, meta. Web UI picks them up automatically via GraphQL.</div>
        </div>
      </div>
    </div>
  );
}

/* ── MCP Tab (full redesign) ─────────────────────────────────────────── */

type McpTransport = 'stdio' | 'http' | 'sse' | 'streamable-http';

interface McpFormState {
  name: string;
  transport: McpTransport;
  command: string;
  args: string[];
  env: {k:string; v:string}[];
  url: string;
  headers: {k:string; v:string}[];
  advancedJson: string;
  useAdvanced: boolean;
}

interface McpPreset {
  id: string;
  name: string;
  desc: string;
  icon: string;
  config: any;
}

const MCP_PRESETS: McpPreset[] = [
  {id:'filesystem', name:'Filesystem', desc:'Read/write local files', icon:'📁', config:{command:'npx', args:['-y','@modelcontextprotocol/server-filesystem','/tmp'], transport:'stdio'}},
  {id:'brave', name:'Brave Search', desc:'Web search via Brave', icon:'🔍', config:{command:'npx', args:['-y','@modelcontextprotocol/server-brave-search'], transport:'stdio', env:{BRAVE_API_KEY:''}}},
  {id:'github', name:'GitHub', desc:'Repos, issues, PRs', icon:'🐙', config:{command:'npx', args:['-y','@modelcontextprotocol/server-github'], transport:'stdio', env:{GITHUB_PERSONAL_ACCESS_TOKEN:''}}},
  {id:'postgres', name:'Postgres', desc:'Query PostgreSQL', icon:'🐘', config:{command:'npx', args:['-y','@modelcontextprotocol/server-postgres','postgresql://user:pass@localhost/db'], transport:'stdio'}},
  {id:'fetch', name:'Fetch', desc:'HTTP fetch & extract', icon:'🌐', config:{command:'npx', args:['-y','@modelcontextprotocol/server-fetch'], transport:'stdio'}},
  {id:'puppeteer', name:'Puppeteer', desc:'Headless browser', icon:'🎭', config:{command:'npx', args:['-y','@modelcontextprotocol/server-puppeteer'], transport:'stdio'}},
  {id:'memory', name:'Memory', desc:'Knowledge graph', icon:'🧠', config:{command:'npx', args:['-y','@modelcontextprotocol/server-memory'], transport:'stdio'}},
  {id:'sqlite', name:'SQLite', desc:'Local SQLite DB', icon:'🗄️', config:{command:'npx', args:['-y','@modelcontextprotocol/server-sqlite','--db-path','/tmp/db.sqlite'], transport:'stdio'}},
];

function configToForm(name:string, rawJson:string): McpFormState {
  let cfg: any = {};
  try { cfg = JSON.parse(rawJson || '{}'); } catch { cfg = {}; }
  const transport = (cfg.transport as McpTransport) || (cfg.url ? 'http' : 'stdio');
  let args: string[] = [];
  if (Array.isArray(cfg.args)) args = cfg.args;
  else if (typeof cfg.args === 'string') args = [cfg.args];
  const envObj = cfg.env && typeof cfg.env === 'object' ? cfg.env : {};
  const headersObj = cfg.headers && typeof cfg.headers === 'object' ? cfg.headers : {};
  return {
    name: name || '',
    transport: transport as McpTransport,
    command: cfg.command ? (Array.isArray(cfg.command) ? cfg.command.join(' ') : cfg.command) : 'npx',
    args: args.length ? args : [],
    env: Object.entries(envObj).map(([k,v])=>({k, v:String(v)})),
    url: cfg.url || '',
    headers: Object.entries(headersObj).map(([k,v])=>({k, v:String(v)})),
    advancedJson: (() => { try { return JSON.stringify(cfg, null, 2); } catch { return rawJson; } })(),
    useAdvanced: false,
  };
}

function formToConfigJson(form: McpFormState): string {
  if (form.useAdvanced) {
    return form.advancedJson;
  }
  const out: any = {};
  out.transport = form.transport;
  if (form.transport === 'stdio') {
    const cmd = form.command.trim().split(/\s+/).filter(Boolean);
    if (cmd.length === 1) out.command = cmd[0];
    else if (cmd.length > 1) {
      out.command = cmd[0];
      const remaining = cmd.slice(1);
      const allArgs = [...remaining, ...form.args.filter(a=>a.trim())];
      if (allArgs.length) out.args = allArgs;
    } else {
      if (form.args.filter(a=>a.trim()).length) out.args = form.args.filter(a=>a.trim());
    }
    if (form.args.filter(a=>a.trim()).length && !out.args) out.args = form.args.filter(a=>a.trim());
    const env: Record<string,string> = {};
    form.env.forEach(({k,v})=>{ if(k.trim()) env[k.trim()] = v; });
    if (Object.keys(env).length) out.env = env;
  } else {
    out.url = form.url.trim();
    const headers: Record<string,string> = {};
    form.headers.forEach(({k,v})=>{ if(k.trim()) headers[k.trim()] = v; });
    if (Object.keys(headers).length) out.headers = headers;
  }
  return JSON.stringify(out);
}

function McpTab() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const {data, isLoading} = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: async () => {
      const {fetchMcpServers} = await import('../relay/McpServersQuery');
      return fetchMcpServers();
    },
  });

  const [search, setSearch] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [editing, setEditing] = useState<{name:string; config:string} | null>(null);

  const servers = data?.servers ?? [];
  const tools = data?.tools ?? [];

  const filtered = useMemo(() => {
    const q = search.toLowerCase().trim();
    if (!q) return servers;
    return servers.filter((s:any) => {
      const hay = `${s.name} ${s.transport} ${s.command||''} ${s.url||''} ${s.config||''}`.toLowerCase();
      return hay.includes(q);
    });
  }, [servers, search]);

  async function refresh() {
    await queryClient.invalidateQueries({queryKey: ['mcp-servers']});
  }

  const addMut = useMutation({
    mutationFn: async (form: McpFormState) => {
      const json = formToConfigJson(form);
      // validate JSON
      JSON.parse(json);
      const {commitAddMcpServer} = await import('../relay/AddMcpServerMutation');
      await commitAddMcpServer(form.name.trim(), json);
    },
    onSuccess: () => { toast.push('MCP server added','success'); setShowAdd(false); refresh(); },
    onError: (e:any) => toast.push(e.message || String(e), 'error'),
  });

  const updateMut = useMutation({
    mutationFn: async (form: McpFormState) => {
      if (!editing) return;
      const json = formToConfigJson(form);
      JSON.parse(json);
      const {commitUpdateMcpServer} = await import('../relay/UpdateMcpServerMutation');
      await commitUpdateMcpServer(editing.name, json);
    },
    onSuccess: () => { toast.push('MCP server updated','success'); setEditing(null); refresh(); },
    onError: (e:any) => toast.push(e.message || String(e), 'error'),
  });

  const removeMut = useMutation({
    mutationFn: async (name:string) => {
      const {commitRemoveMcpServer} = await import('../relay/RemoveMcpServerMutation');
      await commitRemoveMcpServer(name);
    },
    onSuccess: () => { toast.push('MCP server removed','success'); refresh(); },
    onError: (e:any) => toast.push(e.message || String(e), 'error'),
  });

  const reloadMut = useMutation({
    mutationFn: async () => {
      const {commitReloadMcpServers} = await import('../relay/ReloadMcpServersMutation');
      await commitReloadMcpServers();
    },
    onSuccess: () => { toast.push('MCP reloaded','success'); refresh(); },
    onError: (e:any) => toast.push(e.message,'error'),
  });

  return (
    <div style={{display:'flex', flexDirection:'column', gap:16, maxWidth:900}}>
      {/* Top stats + actions */}
      <div className="settings-card">
        <div className="settings-card-body">
          <div className="mcp-topbar">
            <div className="mcp-stats">
              <span className="mcp-stat-pill"><strong>{servers.length}</strong> servers</span>
              <span className="mcp-stat-pill mcp-stat-pill--live"><strong>{tools.length}</strong> tools loaded</span>
              {tools.length>0 && <span className="mcp-stat-pill" style={{maxWidth:340, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap'}}>{tools.slice(0,3).join(', ')}{tools.length>3?` +${tools.length-3}`:''}</span>}
            </div>
            <div style={{display:'flex', gap:8, alignItems:'center'}}>
              <div className="mcp-search">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
                <input placeholder="Filter servers…" value={search} onChange={e=>setSearch(e.target.value)} />
              </div>
              <button className="mcp-btn" onClick={()=>reloadMut.mutate()} disabled={reloadMut.isPending} title="Reload MCP connections">
                {reloadMut.isPending ? 'Reloading…' : '↻ Reload'}
              </button>
              <button className="mcp-btn mcp-btn--primary" onClick={()=>setShowAdd(true)}>+ Add server</button>
            </div>
          </div>

          {isLoading && <div style={{fontSize:'0.85rem', color:'var(--text-dim)', marginTop:12}}>Loading servers…</div>}

          {!isLoading && servers.length===0 && (
            <div className="mcp-empty" style={{marginTop:12}}>
              <div style={{width:48,height:48, borderRadius:12, background:'var(--surface2)', border:'1px solid var(--border)', display:'flex', alignItems:'center', justifyContent:'center', fontSize:22}}>🔌</div>
              <div>
                <div style={{fontWeight:600, color:'var(--text)', marginBottom:4}}>No MCP servers configured</div>
                <div style={{fontSize:'0.8rem', maxWidth:380, lineHeight:1.5}}>MCP servers extend the agent with external tools. Start with a preset or add your own stdio/HTTP server. Config can come from env <code>JARVIS_MCP_SERVERS</code>, file <code>~/.jarvis/mcp.json</code>, or DB (this UI).</div>
              </div>
              <div style={{display:'flex', gap:8, flexWrap:'wrap', justifyContent:'center'}}>
                <button className="mcp-btn mcp-btn--primary" onClick={()=>setShowAdd(true)}>+ Add your first server</button>
                <button className="mcp-btn" onClick={()=>window.open('https://modelcontextprotocol.io/docs','_blank')}>Docs →</button>
              </div>
            </div>
          )}

          {filtered.length>0 && (
            <div className="mcp-grid" style={{marginTop:14}}>
              {filtered.map((s:any) => {
                const isExp = expanded===s.name;
                const isLive = s.toolCount>0;
                return (
                  <div key={s.name} className={`mcp-card ${isExp?'mcp-card--expanded':''}`}>
                    <div className="mcp-card-main" onClick={()=>setExpanded(isExp?null:s.name)}>
                      <div className="mcp-card-icon">
                        {s.transport==='stdio' ? '⚙️' : s.transport==='http' ? '🌐' : '📡'}
                      </div>
                      <div className="mcp-card-meta">
                        <div className="mcp-card-title-row">
                          <span className="mcp-card-name">{s.name}</span>
                          <span className={`mcp-badge mcp-badge--${s.transport==='stdio'?'stdio':s.transport==='http'?'http':'sse'}`}>{s.transport}</span>
                          {isLive ? <span className="mcp-badge mcp-badge--live">{s.toolCount} tools</span> : <span className="mcp-badge mcp-badge--off">not loaded</span>}
                        </div>
                        <div className="mcp-card-preview">{s.command || s.url || 'custom config'}</div>
                      </div>
                      <div className="mcp-card-actions">
                        <button className="mcp-btn mcp-btn--ghost" style={{padding:'6px 10px'}} onClick={(e)=>{ e.stopPropagation(); setEditing({name:s.name, config:s.config}); }}>Edit</button>
                        <button className="mcp-btn mcp-btn--ghost" style={{padding:'6px 8px'}} onClick={(e)=>{ e.stopPropagation(); if(confirm(`Delete ${s.name}? This removes DB override; file/env sources remain.`)) removeMut.mutate(s.name); }}>🗑</button>
                        <span style={{color:'var(--text-faint)', fontSize:'0.7rem', marginLeft:4}}>{isExp?'▲':'▼'}</span>
                      </div>
                    </div>

                    {isExp && (
                      <div className="mcp-card-expand">
                        <div className="mcp-detail-grid">
                          <div className="mcp-detail-section">
                            <div className="mcp-detail-label">Tools {isLive&&`(${s.toolCount})`}</div>
                            {(() => {
                              const related = (() => {
                                if (servers.length===1) return tools;
                                // heuristic filter
                                const matching = tools.filter((t:string)=> t.toLowerCase().includes(s.name.toLowerCase()));
                                return matching.length?matching:tools.slice(0,10);
                              })();
                              if (related.length===0) return <div style={{fontSize:'0.78rem', color:'var(--text-dim)'}}>{isLive?'No tool names matched heuristic':'Server not loaded — click Reload after adding. Tools appear after successful connection.'}</div>;
                              return <div className="mcp-tools-list">{related.map((t:string)=><span key={t} className="mcp-tool-chip">{t}</span>)}{related.length<tools.length && tools.length>related.length && <span className="mcp-tool-chip">+{tools.length-related.length} more</span>}</div>;
                            })()}
                          </div>
                          <div className="mcp-detail-section">
                            <div className="mcp-detail-label">Configuration</div>
                            <pre className="mcp-config-pre">{(() => { try { return JSON.stringify(JSON.parse(s.config), null, 2); } catch { return s.config; } })()}</pre>
                          </div>
                        </div>

                        <div style={{display:'flex', gap:8, justifyContent:'flex-end'}}>
                          <button className="mcp-btn" onClick={()=>{ navigator.clipboard.writeText(s.config); toast.push('Config copied','success'); }}>Copy JSON</button>
                          <button className="mcp-btn" onClick={()=>setEditing({name:s.name, config:s.config})}>Edit</button>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {filtered.length===0 && servers.length>0 && (
            <div style={{textAlign:'center', color:'var(--text-dim)', fontSize:'0.85rem', padding:20}}>No servers match “{search}”.</div>
          )}
        </div>
      </div>

      {/* Add / Edit modals */}
      {showAdd && (
        <McpServerModal
          title="Add MCP Server"
          initial={null}
          onClose={()=>setShowAdd(false)}
          onSubmit={(f)=>addMut.mutate(f)}
          submitting={addMut.isPending}
        />
      )}

      {editing && (
        <McpServerModal
          title={`Edit ${editing.name}`}
          initial={editing}
          onClose={()=>setEditing(null)}
          onSubmit={(f)=>updateMut.mutate(f)}
          submitting={updateMut.isPending}
        />
      )}
    </div>
  );
}

function McpServerModal({title, initial, onClose, onSubmit, submitting}:{title:string; initial:{name:string; config:string}|null; onClose:()=>void; onSubmit:(f:McpFormState)=>void; submitting:boolean}) {
  const [form, setForm] = useState<McpFormState>(() => {
    if (initial) return configToForm(initial.name, initial.config);
    return {
      name:'',
      transport:'stdio',
      command:'npx',
      args:['-y','@modelcontextprotocol/server-filesystem','/tmp'],
      env:[],
      url:'',
      headers:[],
      advancedJson:'{\n  "command": "npx",\n  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],\n  "transport": "stdio"\n}',
      useAdvanced:false,
    };
  });

  const [presetFilter, setPresetFilter] = useState('');

  const visiblePresets = useMemo(() => {
    const q = presetFilter.toLowerCase();
    if (!q) return MCP_PRESETS;
    return MCP_PRESETS.filter(p=> `${p.name} ${p.desc}`.toLowerCase().includes(q));
  }, [presetFilter]);

  function applyPreset(p:McpPreset) {
    const cfg = p.config;
    const args = Array.isArray(cfg.args) ? cfg.args : [];
    const env = cfg.env ? Object.entries(cfg.env).map(([k,v])=>({k, v:String(v)})) : [];
    setForm(f=>({
      ...f,
      name: f.name || p.id,
      transport: (cfg.transport as McpTransport) || 'stdio',
      command: cfg.command || f.command,
      args: args.length ? args : f.args,
      env: env.length ? env : f.env,
      advancedJson: JSON.stringify(cfg, null, 2),
    }));
  }

  function update<K extends keyof McpFormState>(key:K, val:McpFormState[K]) {
    setForm(f=>({...f, [key]:val}));
  }

  function addArg() { setForm(f=>({...f, args:[...f.args, '']})); }
  function updateArg(i:number, v:string) { setForm(f=>{ const a=[...f.args]; a[i]=v; return {...f, args:a}; }); }
  function removeArg(i:number) { setForm(f=>({...f, args:f.args.filter((_,idx)=>idx!==i)})); }

  function addEnv() { setForm(f=>({...f, env:[...f.env, {k:'', v:''}]})); }
  function updateEnv(i:number, field:'k'|'v', val:string) { setForm(f=>{ const e=[...f.env]; e[i]={...e[i], [field]:val}; return {...f, env:e}; }); }
  function removeEnv(i:number) { setForm(f=>({...f, env:f.env.filter((_,idx)=>idx!==i)})); }

  function addHeader() { setForm(f=>({...f, headers:[...f.headers, {k:'', v:''}]})); }
  function updHeader(i:number, field:'k'|'v', val:string) { setForm(f=>{ const h=[...f.headers]; h[i]={...h[i], [field]:val}; return {...f, headers:h}; }); }
  function remHeader(i:number) { setForm(f=>({...f, headers:f.headers.filter((_,idx)=>idx!==i)})); }

  const canSubmit = useMemo(() => {
    if (!form.name.trim()) return false;
    if (form.useAdvanced) {
      try { JSON.parse(form.advancedJson); return true; } catch { return false; }
    }
    if (form.transport==='stdio') return !!form.command.trim();
    return !!form.url.trim();
  }, [form]);

  function handleSubmit() {
    if (!canSubmit) return;
    onSubmit(form);
  }

  return (
    <div className="mcp-modal-backdrop" onClick={e=>{ if(e.target===e.currentTarget) onClose(); }}>
      <div className="mcp-modal">
        <div className="mcp-modal-header">
          <div className="mcp-modal-title">{title}</div>
          <button className="mcp-btn mcp-btn--ghost" onClick={onClose}>✕</button>
        </div>

        <div className="mcp-modal-body">
          {!initial && (
            <>
              <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
                <div className="mcp-detail-label">Quick presets</div>
                <div className="mcp-search" style={{minWidth:160, padding:'4px 8px'}}>
                  <input placeholder="Filter presets…" value={presetFilter} onChange={e=>setPresetFilter(e.target.value)} style={{fontSize:'0.78rem'}} />
                </div>
              </div>
              <div className="mcp-preset-strip">
                {visiblePresets.map(p=>(
                  <div key={p.id} className="mcp-preset" onClick={()=>applyPreset(p)}>
                    <div style={{fontSize:'1rem'}}>{p.icon}</div>
                    <div className="mcp-preset-name">{p.name}</div>
                    <div className="mcp-preset-desc">{p.desc}</div>
                  </div>
                ))}
              </div>
            </>
          )}

          <div className="mcp-form">
            <div className="mcp-form-row">
              <div className="mcp-form-group">
                <div className="mcp-form-label">Name *</div>
                <input className="mcp-form-input mcp-form-input--mono" placeholder="e.g. filesystem" value={form.name} onChange={e=>update('name', e.target.value)} disabled={!!initial} />
                {initial && <div style={{fontSize:'0.7rem', color:'var(--text-faint)'}}>Name cannot be changed when editing (delete & recreate to rename).</div>}
              </div>
              <div className="mcp-form-group" style={{flex:'0 0 160px'}}>
                <div className="mcp-form-label">Transport *</div>
                <select className="mcp-form-select" value={form.transport} onChange={e=>update('transport', e.target.value as McpTransport)} disabled={form.useAdvanced}>
                  <option value="stdio">stdio</option>
                  <option value="http">http</option>
                  <option value="sse">sse</option>
                  <option value="streamable-http">streamable-http</option>
                </select>
              </div>
            </div>

            {!form.useAdvanced && form.transport==='stdio' && (
              <>
                <div className="mcp-form-group">
                  <div className="mcp-form-label">Command *</div>
                  <input className="mcp-form-input mcp-form-input--mono" placeholder="npx or python or /path/to/binary" value={form.command} onChange={e=>update('command', e.target.value)} />
                </div>

                <div className="mcp-form-group">
                  <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
                    <div className="mcp-form-label">Arguments</div>
                    <button className="mcp-btn mcp-btn--ghost" style={{padding:'2px 8px', fontSize:'0.72rem'}} onClick={addArg}>+ Add arg</button>
                  </div>
                  <div className="mcp-kv-list">
                    {form.args.map((a,i)=>(
                      <div key={i} className="mcp-kv-row">
                        <input className="mcp-form-input mcp-form-input--mono" value={a} onChange={e=>updateArg(i, e.target.value)} placeholder={`arg ${i+1}`} />
                        <button className="mcp-btn mcp-btn--ghost" style={{padding:'4px 8px'}} onClick={()=>removeArg(i)}>✕</button>
                      </div>
                    ))}
                    {form.args.length===0 && <div style={{fontSize:'0.78rem', color:'var(--text-dim)'}}>No args. <button className="mcp-btn mcp-btn--ghost" style={{padding:'0 4px', fontSize:'0.78rem', textDecoration:'underline'}} onClick={addArg}>Add one</button></div>}
                  </div>
                </div>

                <div className="mcp-form-group">
                  <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
                    <div className="mcp-form-label">Environment variables</div>
                    <button className="mcp-btn mcp-btn--ghost" style={{padding:'2px 8px', fontSize:'0.72rem'}} onClick={addEnv}>+ Add var</button>
                  </div>
                  <div className="mcp-kv-list">
                    {form.env.map((pair,i)=>(
                      <div key={i} className="mcp-kv-row">
                        <input className="mcp-form-input mcp-form-input--mono" value={pair.k} onChange={e=>updateEnv(i,'k',e.target.value)} placeholder="KEY" style={{flex:'0 0 140px'}} />
                        <input className="mcp-form-input mcp-form-input--mono" value={pair.v} onChange={e=>updateEnv(i,'v',e.target.value)} placeholder="value" />
                        <button className="mcp-btn mcp-btn--ghost" style={{padding:'4px 8px'}} onClick={()=>removeEnv(i)}>✕</button>
                      </div>
                    ))}
                    {form.env.length===0 && <div style={{fontSize:'0.78rem', color:'var(--text-dim)'}}>No env vars. Secrets like API keys go here.</div>}
                  </div>
                </div>
              </>
            )}

            {!form.useAdvanced && form.transport!=='stdio' && (
              <>
                <div className="mcp-form-group">
                  <div className="mcp-form-label">URL *</div>
                  <input className="mcp-form-input mcp-form-input--mono" placeholder="https://example.com/mcp" value={form.url} onChange={e=>update('url', e.target.value)} />
                </div>
                <div className="mcp-form-group">
                  <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
                    <div className="mcp-form-label">Headers</div>
                    <button className="mcp-btn mcp-btn--ghost" style={{padding:'2px 8px', fontSize:'0.72rem'}} onClick={addHeader}>+ Add header</button>
                  </div>
                  <div className="mcp-kv-list">
                    {form.headers.map((pair,i)=>(
                      <div key={i} className="mcp-kv-row">
                        <input className="mcp-form-input mcp-form-input--mono" value={pair.k} onChange={e=>updHeader(i,'k',e.target.value)} placeholder="Authorization" style={{flex:'0 0 160px'}} />
                        <input className="mcp-form-input mcp-form-input--mono" value={pair.v} onChange={e=>updHeader(i,'v',e.target.value)} placeholder="Bearer ..." />
                        <button className="mcp-btn mcp-btn--ghost" style={{padding:'4px 8px'}} onClick={()=>remHeader(i)}>✕</button>
                      </div>
                    ))}
                    {form.headers.length===0 && <div style={{fontSize:'0.78rem', color:'var(--text-dim)'}}>Optional. For auth tokens etc.</div>}
                  </div>
                </div>
              </>
            )}

            <label className="mcp-switch">
              <input type="checkbox" checked={form.useAdvanced} onChange={e=>update('useAdvanced', e.target.checked)} />
              Advanced — edit raw JSON
            </label>

            {form.useAdvanced && (
              <div className="mcp-form-group">
                <div className="mcp-form-label">Raw config JSON</div>
                <textarea className="mcp-form-textarea" style={{minHeight:180}} value={form.advancedJson} onChange={e=>update('advancedJson', e.target.value)} />
                {(() => { try { JSON.parse(form.advancedJson); return null; } catch (err:any) { return <div style={{color:'var(--error-text)', fontSize:'0.75rem', marginTop:4}}>{String(err.message || err)}</div>; } })()}
              </div>
            )}

            {!form.useAdvanced && (
              <div className="mcp-detail-section">
                <div className="mcp-detail-label">Preview JSON</div>
                <pre className="mcp-config-pre" style={{maxHeight:140}}>{(() => { try { return JSON.stringify(JSON.parse(formToConfigJson(form)), null, 2); } catch { return formToConfigJson(form); } })()}</pre>
              </div>
            )}
          </div>
        </div>

        <div className="mcp-modal-footer">
          <button className="mcp-btn" onClick={onClose} disabled={submitting}>Cancel</button>
          <button className="mcp-btn mcp-btn--primary" onClick={handleSubmit} disabled={!canSubmit || submitting}>{submitting?'Saving…': initial?'Save changes':'Add server'}</button>
        </div>
      </div>
    </div>
  );
}

/* ── Existing notification rows (polished) ───────────────────────────── */

interface ChannelRowProps {
  channel: NotificationChannel;
  onChanged: () => void;
}

function ChannelRow({channel, onChanged}: ChannelRowProps) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(channel.name);
  const [type, setType] = useState<NotificationChannelType>(channel.type);
  const [target, setTarget] = useState(channel.target);
  const [refsInUse, setRefsInUse] = useState<NotificationChannelReference[] | null>(null);

  const updateMut = useMutation({
    mutationFn: () =>
      commitUpdateNotificationChannel(channel.id, {name: name.trim(), type, target: target.trim()}),
    onSuccess: () => {
      toast.push('Channel updated', 'success');
      setEditing(false);
      onChanged();
    },
    onError: (e: Error) => toast.push(e.message, 'error'),
  });

  const deleteMut = useMutation({
    mutationFn: () => commitDeleteNotificationChannel(channel.id),
    onSuccess: (result) => {
      if (result.ok) {
        toast.push('Channel deleted', 'success');
        onChanged();
      } else {
        setRefsInUse(result.references ?? []);
      }
    },
    onError: (e: Error) => toast.push(e.message, 'error'),
  });

  if (!editing) {
    return (
      <div className="notif-row">
        <div className={`notif-row-icon notif-row-icon--${channel.type}`}>
          {channel.type==='telegram' ? (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 2L11 13"/><path d="M22 2L15 22l-4-9-9-4 20-7z"/></svg>
          ) : (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 8a3 3 0 0 0-3-3H9a3 3 0 0 0-3 3v8a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3V8z"/><path d="M9 12a3 3 0 1 0 6 0a3 3 0 0 0-6 0z"/></svg>
          )}
        </div>
        <div className="notif-row-main">
          <div className="notif-row-name">{channel.name}</div>
          <div className="notif-row-target">{channel.type} — {channel.target}</div>
          {refsInUse && refsInUse.length>0 && (
            <div style={{fontSize:'0.74rem', color:'var(--error-text)', marginTop:4}}>
              Cannot delete — used by: {refsInUse.map((r,i)=><span key={`${r.kind}:${r.id}`}>{i>0&&', '}{r.kind} "{r.name}"</span>)}
            </div>
          )}
        </div>
        <button className="mcp-btn mcp-btn--ghost" onClick={()=>setEditing(true)}>Edit</button>
        <button className="mcp-btn mcp-btn--ghost" onClick={()=>deleteMut.mutate()} disabled={deleteMut.isPending}>{deleteMut.isPending?'…':'Delete'}</button>
      </div>
    );
  }

  return (
    <div className="mcp-form" style={{padding:12}}>
      <div className="mcp-form-row">
        <div className="mcp-form-group">
          <div className="mcp-form-label">Name</div>
          <input className="mcp-form-input" value={name} onChange={e=>setName(e.target.value)} placeholder="team-discord" />
        </div>
        <div className="mcp-form-group" style={{flex:'0 0 140px'}}>
          <div className="mcp-form-label">Type</div>
          <select className="mcp-form-select" value={type} onChange={e=>setType(e.target.value as NotificationChannelType)}>
            <option value="telegram">Telegram</option>
            <option value="discord">Discord</option>
          </select>
        </div>
      </div>
      <div className="mcp-form-group">
        <div className="mcp-form-label">Target {type==='telegram'?'(chat ID)':'(channel ID)'}</div>
        <input className="mcp-form-input" value={target} onChange={e=>setTarget(e.target.value)} placeholder={type==='telegram'?'123456789':'123456789012345678'} />
      </div>
      <div style={{display:'flex', gap:8, justifyContent:'flex-end'}}>
        <button className="mcp-btn" onClick={()=>{ setName(channel.name); setType(channel.type); setTarget(channel.target); setEditing(false); }}>Cancel</button>
        <button className="mcp-btn mcp-btn--primary" onClick={()=>updateMut.mutate()} disabled={updateMut.isPending || !name.trim() || !target.trim()}>{updateMut.isPending?'Saving…':'Save'}</button>
      </div>
    </div>
  );
}

interface DraftRowProps {
  draft: NotificationChannelInput;
  onChange: (next: NotificationChannelInput) => void;
  onCancel: () => void;
  onSaved: () => void;
}

function DraftRow({draft, onChange, onCancel, onSaved}: DraftRowProps) {
  const toast = useToast();
  const createMut = useMutation({
    mutationFn: () =>
      commitCreateNotificationChannel({
        name: draft.name.trim(),
        type: draft.type,
        target: draft.target.trim(),
      }),
    onSuccess: () => {
      toast.push('Channel created', 'success');
      onSaved();
    },
    onError: (e: Error) => toast.push(e.message, 'error'),
  });

  const disabled = !draft.name.trim() || !draft.target.trim() || createMut.isPending;

  return (
    <div className="mcp-form" style={{padding:12}}>
      <div className="mcp-form-row">
        <div className="mcp-form-group">
          <div className="mcp-form-label">Name *</div>
          <input className="mcp-form-input" value={draft.name} onChange={e=>onChange({...draft, name:e.target.value})} placeholder="Channel name (e.g. team-discord)" />
        </div>
        <div className="mcp-form-group" style={{flex:'0 0 140px'}}>
          <div className="mcp-form-label">Type</div>
          <select className="mcp-form-select" value={draft.type} onChange={e=>onChange({...draft, type:e.target.value as NotificationChannelType})}>
            <option value="telegram">Telegram</option>
            <option value="discord">Discord</option>
          </select>
        </div>
      </div>
      <div className="mcp-form-group">
        <div className="mcp-form-label">Target *</div>
        <input className="mcp-form-input" value={draft.target} onChange={e=>onChange({...draft, target:e.target.value})} placeholder={draft.type==='telegram'?'Telegram chat ID':'Discord channel ID'} />
      </div>
      <div style={{display:'flex', gap:8, justifyContent:'flex-end'}}>
        <button className="mcp-btn" onClick={onCancel} disabled={createMut.isPending}>Cancel</button>
        <button className="mcp-btn mcp-btn--primary" onClick={()=>createMut.mutate()} disabled={disabled}>{createMut.isPending?'Creating…':'Create'}</button>
      </div>
    </div>
  );
}
