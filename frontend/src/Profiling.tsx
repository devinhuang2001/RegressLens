import { useMemo, useState } from 'react';
import type { Investigation } from './types';
import './profiling.css';

type Side = 'good' | 'bad';
type Tab = 'timeline' | 'flame' | 'kernels';
type Mode = Side | 'diff';

export type TraceEvent = {
  lane: string; label: string; name: string; category: string; at: number; duration: number;
};
type Trace = { duration: number; count: number; events: TraceEvent[] };
type Frame = { name: string; file: string; parent: number; good: number; bad: number };
type Kernel = { count: number; total_us: number };
export type Profile = {
  source: string; scenario: string; requests?: number | null; commits: Record<Side, string>;
  traces: Partial<Record<Side, Trace>>;
  kernels: { name: string; good: Kernel | null; bad: Kernel | null }[];
  kernel_comparable: boolean;
  flame: { nodes: Frame[]; unit: string; source: string } | null;
  artifacts: Record<string, { name: string; file: string; sha256: string }>;
  warnings: string[];
};

const sideLabels = { good: 'Good', bad: 'Bad' };
const display = (value: number, decimals = 2) =>
  new Intl.NumberFormat('en', { maximumFractionDigits: decimals }).format(value);
const milliseconds = (us: number) => display(us / 1e3, 3);
const signed = (n: number) => (n > 0 ? '+' : '') + display(n, 3);
const gpu = (event: TraceEvent) => /(^|,)\s*(kernel|gpu_kernel|cuda_kernel|hip_kernel)\s*(,|$)/i.test(event.category);
const pct = (n: number) => (n * 100).toFixed(2) + '%';
const pp = (n: number) => signed(n * 100) + ' pp';

function tracks(events: TraceEvent[]) {
  const lanes = new Map<string, { label: string; rows: TraceEvent[][]; ends: number[] }>();
  for (const event of events) {
    let lane = lanes.get(event.lane);
    if (!lane) {
      lane = { label: event.label, rows: [], ends: [] };
      lanes.set(event.lane, lane);
    }
    const free = lane.ends.findIndex(end => end <= event.at);
    const index = free < 0 ? lane.rows.length : free;
    if (!lane.rows[index]) lane.rows[index] = [];
    lane.rows[index].push(event);
    lane.ends[index] = event.at + event.duration;
  }
  return [...lanes.values()];
}

function Timeline({ profile }: { profile: Profile }) {
  const [search, setSearch] = useState('');
  const [zoom, setZoom] = useState(1);
  const [left, setLeft] = useState(0);
  const [picked, setPicked] = useState<{ side: Side; event: TraceEvent } | null>(null);
  const length = Math.max(1, profile.traces.good?.duration ?? 0, profile.traces.bad?.duration ?? 0);
  const span = length / zoom;
  const start = Math.min(left, length - span);
  const end = start + span;
  const plotTicks = [0, .25, .5, .75, 1];
  const query = search.trim().toLocaleLowerCase();
  const filtered = (side: Side) => tracks((profile.traces[side]?.events ?? []).filter(event =>
    event.at < end && event.at + event.duration > start &&
      (!query || (event.name + ' ' + event.category + ' ' + event.label).toLocaleLowerCase().includes(query))));
  const newZoom = (factor: number) => {
    const nextZoom = Math.max(1, Math.min(1024, zoom * factor));
    const center = picked ? picked.event.at + picked.event.duration / 2 : start + span / 2;
    setLeft(Math.max(0, center - length / nextZoom / 2));
    setZoom(nextZoom);
  };
  return <section className="panel">
    <div className="panel-title"><h2>Trace timeline</h2><span className="counter">Time from first captured event</span></div>
    <div className="profile-controls">
      <label>Filter spans<input value={search} onChange={e => setSearch(e.target.value)} placeholder="Event, category or thread" /></label>
      <div className="profile-actions">
        <button onClick={() => newZoom(2)} disabled={zoom === 1024}>Zoom in</button>
        <button onClick={() => newZoom(.5)} disabled={zoom === 1}>Zoom out</button>
        <button onClick={() => { setZoom(1); setLeft(0); setPicked(null); }}>Reset view</button>
      </div>
    </div>
    {zoom > 1 && <label className="trace-pan">Pan time window<input type="range" min={0} max={length - span}
      step={(length - span) / 500} value={start} onChange={e => setLeft(Number(e.target.value))} /></label>}
    <div className="trace-pair">
      {(['good', 'bad'] as const).map(side => {
        const trace = profile.traces[side];
        const lanes = filtered(side);
        return <div className="trace-side" key={side}>
          <div className="trace-side-title"><h3>{sideLabels[side]}</h3><span>{display(trace?.count ?? 0, 0)} spans · {milliseconds(trace?.duration ?? 0)} ms</span></div>
          <div className="trace-scale">{plotTicks.map(tick => <span key={tick}>{milliseconds(start + span * tick)} ms</span>)}</div>
          <div className="trace-tracks">
            {lanes.length ? lanes.map((lane, i) => <div key={i} className="trace-lane">
              <div className="trace-lane-name">{lane.label}</div>
              {lane.rows.map((row, index) => <div key={index} className="trace-row">
                {row.map((event, n) => {
                  const from = Math.max(event.at, start);
                  const to = Math.min(event.at + event.duration, end);
                  return <button key={n} className={'trace-span ' + (gpu(event) ? 'trace-gpu ' : '') +
                    (picked?.event === event ? 'trace-picked' : '')}
                    style={{ left: pct((from - start) / span), width: pct((to - from) / span) }}
                    onClick={() => setPicked({ side, event })}
                    title={event.name + ' · ' + milliseconds(event.duration) + ' ms'}
                    aria-label={event.name + ' ' + sideLabels[side]}><span>{event.name}</span></button>;
                })}
              </div>)}
            </div>) : <p className="empty">No spans match this window and filter.</p>}
          </div>
        </div>;
      })}
    </div>
    {picked && <div className="profile-inspect" aria-live="polite">
      <strong>{sideLabels[picked.side]} · {picked.event.name}</strong>
      <span>{picked.event.category || 'Event'} · {picked.event.label}</span>
      <code>Start {milliseconds(picked.event.at)} ms · Duration {milliseconds(picked.event.duration)} ms</code>
    </div>}
    <p className="panel-note">Both timelines share a scale. Each starts at its first captured event. Blue spans are GPU kernels.</p>
  </section>;
}

function Flame({ profile }: { profile: Profile }) {
  const [mode, setMode] = useState<Mode>('diff');
  const [zoom, setZoom] = useState(0);
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<number | null>(null);
  const nodes = profile.flame?.nodes ?? [];
  const root = nodes[0];
  const paths = useMemo(() => {
    const children = new Map<number, number[]>();
    nodes.forEach((node, i) => {
      const items = children.get(node.parent) ?? [];
      items.push(i);
      children.set(node.parent, items);
    });
    return children;
  }, [nodes]);
  if (!profile.flame || !root) return <section className="panel"><div className="panel-title"><h2>CPU flame graph</h2></div>
    <p className="empty">Attach matching Speedscope profiles or capture nested Python spans on both sides.</p></section>;
  const share = (node: Frame, side: Side) => root[side] > 0 ? node[side] / root[side] : 0;
  const weight = (node: Frame) => mode === 'diff' ? (share(node, 'good') + share(node, 'bad')) / 2 : share(node, mode);
  const boxes: { index: number; depth: number; left: number; width: number }[] = [];
  const total = weight(nodes[zoom]);
  function layout(index: number, depth: number, offset: number) {
    const width = weight(nodes[index]) / total;
    if (width < .0015) return;
    boxes.push({ index, depth, left: offset, width });
    let position = offset;
    for (const child of paths.get(index) ?? []) {
      layout(child, depth + 1, position);
      position += weight(nodes[child]) / total;
    }
  }
  if (total > 0) layout(zoom, 0, 0);
  const depths = Math.max(1, ...boxes.map(box => box.depth + 1));
  const query = search.trim().toLowerCase();
  const active = nodes[selected ?? zoom];
  const importSource = profile.flame.source === 'speedscope' ? 'Speedscope stack weights' : 'Python span time · summed across captured threads';
  const detail = (node: Frame) => `Good ${pct(share(node, 'good'))} · Bad ${pct(share(node, 'bad'))} · Δ ${pp(share(node, 'bad') - share(node, 'good'))}`;
  return <section className="panel">
    <div className="panel-title"><h2>CPU flame graph</h2><span className="counter">{importSource}</span></div>
    <div className="profile-controls">
      <div className="profile-segments" aria-label="Flame graph mode">
        {(['diff', 'good', 'bad'] as const).map(key => <button key={key} aria-pressed={mode === key}
          onClick={() => { setMode(key); setZoom(0); setSelected(null); }}>
          {key === 'diff' ? 'Difference' : sideLabels[key]}</button>)}
      </div>
      <label>Find a frame<input value={search} onChange={e => setSearch(e.target.value)} placeholder="Function or file" /></label>
      <button onClick={() => { setZoom(0); setSelected(null); }}>Reset zoom</button>
    </div>
    <div className="flame-scroll" role="group" aria-label="Interactive call stacks">
      <div className="flame-plot" style={{ height: depths * 28 + 4 }}>
        {boxes.map(({ index, depth, left, width }) => {
          const node = nodes[index];
          const delta = share(node, 'bad') - share(node, 'good');
          const strength = Math.min(Math.abs(delta) * 1.6, 1);
          const background = mode !== 'diff' ? `hsl(219, 76%, ${90 - Math.min(depth, 7) * 1.6}%)` :
            Math.abs(delta) < .005 ? '#e1e8f2' : `hsl(${delta > 0 ? 12 : 157}, 67%, ${91 - 35 * strength}%)`;
          const matched = !query || (node.name + ' ' + node.file).toLowerCase().includes(query);
          return <button key={index} className={'flame-frame' + (matched ? '' : ' flame-dim')}
            style={{ left: pct(left), width: pct(width), bottom: 2 + depth * 28, background }}
            onClick={() => { setSelected(index); setZoom(index); }}
            title={node.name + '\n' + detail(node)} aria-label={node.name + ', ' + detail(node)}>
            <span>{node.name}</span></button>;
        })}
      </div>
    </div>
    <div className="profile-inspect" aria-live="polite"><strong>{active.name}</strong><span>{detail(active)}</span>
      {active.file && <code>{active.file}</code>}
      <small>Inclusive weight · Good {display(active.good)} / Bad {display(active.bad)} {profile.flame.unit === 'none' ? 'sample weight' : profile.flame.unit}</small>
    </div>
    <p className="panel-note">{mode === 'diff'
      ? 'Width shows the mean share across both captures. Red means the stack gains share in Bad; green means it loses share.'
      : 'Width shows inclusive weight as a share of this capture.'} Click a frame to zoom into its stack.</p>
  </section>;
}

function Kernels({ profile }: { profile: Profile }) {
  const [search, setSearch] = useState('');
  const query = search.trim().toLowerCase();
  const rows = profile.kernels.filter(row => row.name.toLowerCase().includes(query));
  const value = (stats: Kernel | null) => stats ? milliseconds(stats.total_us) : '—';
  return <section className="panel"><div className="panel-title"><h2>GPU kernel comparison</h2></div>
    <div className="profile-controls"><label>Filter kernels<input value={search} onChange={e => setSearch(e.target.value)}
      placeholder="Kernel name" /></label><span className="counter">{rows.length} kernel names</span></div>
    {!profile.kernel_comparable && <p className="notice profile-warning">Both traces need GPU kernel events to compute deltas.</p>}
    {rows.length ? <div className="table-wrap"><table className="kernel-table"><thead><tr>
      <th>Kernel</th><th>Good ms</th><th>Bad ms</th><th>Δ total ms</th><th>Calls G / B</th><th>Mean µs G / B</th>
    </tr></thead><tbody>{rows.slice(0, 100).map(row => {
      const delta = ((row.bad?.total_us ?? 0) - (row.good?.total_us ?? 0)) / 1e3;
      return <tr key={row.name}><td><code>{row.name}</code></td><td>{value(row.good)}</td><td>{value(row.bad)}</td>
        <td className={delta > 0 ? 'kernel-added' : delta < 0 ? 'kernel-removed' : ''}>
          {profile.kernel_comparable ? signed(delta) : '—'}</td>
        <td>{row.good?.count ?? '—'} / {row.bad?.count ?? '—'}</td>
        <td>{row.good ? display(row.good.total_us / row.good.count, 3) : '—'} / {row.bad ? display(row.bad.total_us / row.bad.count, 3) : '—'}</td>
      </tr>;
    })}</tbody></table></div> : <p className="empty">No kernel events match this filter.</p>}
    <p className="panel-note">Sorted by absolute change in total kernel duration. Concurrent kernels can overlap.
      {rows.length > 100 ? ' Showing up to 100 names. Use the filter or export JSON for the full list.' : ''}</p>
  </section>;
}

const perfettoOrigin = 'https://ui.perfetto.dev';
const artifactUrl = (investigation: string, kind: string) =>
  `/api/investigations/${encodeURIComponent(investigation)}/profiles/${kind}`;

async function openPerfetto(path: string, title: string, fileName: string) {
  const viewer = window.open(perfettoOrigin);
  if (!viewer) throw new Error('Allow pop-ups to open Perfetto, or download the original trace.');
  await new Promise<void>((resolve, reject) => {
    const start = Date.now();
    const ping = window.setInterval(() => {
      if (viewer.closed || Date.now() - start > 12000) {
        clear();
        reject(new Error('Perfetto did not acknowledge the viewer connection. Download the trace to open it manually.'));
        return;
      }
      viewer.postMessage('PING', perfettoOrigin);
    }, 200);
    function clear() {
      window.clearInterval(ping);
      window.removeEventListener('message', onMessage);
    }
    function onMessage(event: MessageEvent) {
      if (event.source === viewer && event.origin === perfettoOrigin && event.data === 'PONG') {
        clear();
        resolve();
      }
    }
    window.addEventListener('message', onMessage);
  });
  const response = await fetch(path);
  if (!response.ok) throw new Error('The original trace could not be loaded.');
  const buffer = await response.arrayBuffer();
  viewer.postMessage({ perfetto: { buffer, title, fileName } }, perfettoOrigin, [buffer]);
}

export function Profiling({ data, offline }: { data: Investigation; offline: boolean }) {
  const profile = data.profiling;
  const [tab, setTab] = useState<Tab>(() => profile?.traces.good ? 'timeline' : 'flame');
  const [openError, setOpenError] = useState('');
  if (!profile) return <><div className="section-heading"><div><h1>Profiling comparison</h1>
    <p>Trace timelines and call stacks for the versions under investigation.</p></div></div>
    <section className="panel"><div className="panel-title"><h2>Profiling evidence</h2></div>
      <p className="empty">{data.profiling_warning ?? 'No profiling pair is attached.'}</p>
      <pre className="command"><code>{`regresslens profile attach --investigation ${data.id} --good-trace good.json --bad-trace bad.json`}</code></pre>
    </section></>;
  const labels: Record<Tab, string> = { timeline: 'Trace timeline', flame: 'CPU flame graph', kernels: 'GPU kernels' };
  const tabs: Tab[] = profile.traces.good ? ['timeline', 'flame', 'kernels'] : ['flame'];
  return <>
    <div className="section-heading"><div><h1>Profiling comparison</h1><p>Separate profiling runs linked to this investigation.</p></div></div>
    <div className="profile-meta">
      <div><span>Good</span><code title={profile.commits.good}>{profile.commits.good.slice(0, 12)}</code></div>
      <div><span>Bad</span><code title={profile.commits.bad}>{profile.commits.bad.slice(0, 12)}</code></div>
      <div><span>Workload</span><strong>{profile.scenario}{profile.requests ? ` · ${profile.requests} requests` : ''}</strong></div>
      <div><span>Provenance</span><strong>{profile.source === 'vllm_torch_replay' ? 'vLLM Torch replay' : profile.source === 'synthetic_fixture' ? 'Synthetic fixture' : 'Imported artifacts'}</strong></div>
    </div>
    <div className="profile-tabs" aria-label="Profiling views">{tabs.map(key =>
      <button key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{labels[key]}</button>)}</div>
    {!!profile.warnings.length && <ul className="notice profile-warning">{profile.warnings.map(item => <li key={item}>{item}</li>)}</ul>}
    {tab === 'timeline' ? <Timeline profile={profile} /> : tab === 'flame' ? <Flame profile={profile} /> : <Kernels profile={profile} />}
    <details className="alternatives profile-artifacts"><summary>Original profiling artifacts</summary>
      <div>
        <ul>{Object.entries(profile.artifacts).map(([kind, artifact]) => <li key={kind}>
          {offline ? <code>profiles/{artifact.name}</code> :
                        <a href={artifactUrl(data.id, kind)}>Download {artifact.file}</a>}
          {!offline && kind.endsWith('-trace') && <button className="compact" onClick={() => {
            setOpenError('');
            openPerfetto(artifactUrl(data.id, kind), data.id + ' · ' + kind, artifact.name)
              .catch(error => setOpenError(String(error.message)));
          }}>Open {kind.startsWith('good') ? 'Good' : 'Bad'} in Perfetto</button>}
        </li>)}</ul>
        {openError && <p role="alert" className="notice">{openError}</p>}
        <p>Open Chrome traces in <a href="https://ui.perfetto.dev/" target="_blank" rel="noreferrer">Perfetto</a> or stack profiles in <a href="https://www.speedscope.app/" target="_blank" rel="noreferrer">Speedscope</a>.</p>
      </div>
    </details>
  </>;
}
