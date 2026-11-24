import { useEffect, useState, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { Chart, lines } from './Chart';
import { Profiling } from './Profiling';
import type { Commit, Evidence, Finding, Gate, Investigation } from './types';
import './styles.css';

type Page = 'overview' | 'commits' | 'diagnosis' | 'evidence' | 'profiling';

const navigation: Record<Page, { text: string; symbol: string }> = {
  overview: { text: 'Performance', symbol: '≈' },
  commits: { text: 'Commit explorer', symbol: '⑂' },
  diagnosis: { text: 'Root cause', symbol: '◎' },
  profiling: { text: 'Profiling', symbol: '≋' },
  evidence: { text: 'Run evidence', symbol: '≡' },
};
const caseNames: Record<string, string> = {
  controlled_fixture: 'Synthetic fixture', controlled_injection: 'Controlled engine change',
  hardware: 'Hardware measurement', local_smoke: 'Local protocol smoke test',
};
const names: Record<string, string> = {
  ttft_ms_p95: 'TTFT p95', tpot_ms_p95: 'TPOT p95', e2e_ms_p95: 'End-to-end p95',
  output_tokens_per_second: 'Output throughput', requests_per_second: 'Request throughput',
  itl_ms_p95: 'Inter-token latency p95', queue_ms_mean: 'Queue time mean',
  prefill_ms_mean: 'Prefill time mean', decode_ms_mean: 'Decode time mean',
  kv_cache_usage_snapshot: 'KV cache occupancy', preemptions: 'Preemptions',
};
const nice = (name: string) => names[name] ?? name;
const fmt = (n: number | null | undefined, digits = 1) =>
  n == null ? '—' : new Intl.NumberFormat('en', { maximumFractionDigits: digits }).format(n);
const pct = (n: number | null | undefined) => n == null ? '—' : (n > 0 ? '+' : '') + (n * 100).toFixed(1) + '%';
const sha = (id: string) => id.slice(0, 8);
const unit = (key: string) => key.includes('_ms') ? 'ms' : key.includes('tokens_per_second') ? 'tok/s' :
  key.includes('requests_per_second') ? 'req/s' : '';
const currentPage = (): Page => {
  const page = window.location.hash.slice(1).split('?', 1)[0];
  return Object.hasOwn(navigation, page) ? page as Page : 'overview';
};

function Status({ value }: { value: string }) {
  return <span className={'status state-' + value.toLowerCase()}><i />{value.replaceAll('_', ' ')}</span>;
}

function Panel({ title, extra, children }: { title: string; extra?: ReactNode; children: ReactNode }) {
  return <section className="panel"><div className="panel-title"><h2>{title}</h2>{extra}</div>{children}</section>;
}

function MetricCard({ metric, row, higherIsBetter }: { metric: string; row?: Gate; higherIsBetter: boolean }) {
  const change = row?.delta == null ? null : row.delta * (higherIsBetter ? -1 : 1);
  const tone = row?.decision === 'FAIL' ? 'down' : row?.delta != null && row.delta < 0 ? 'up' : '';
  return <section className="metric-card">
    <div className="metric-label">{nice(metric)}</div>
    <div className="metric-main"><strong>{fmt(row?.candidate)}</strong><span>{unit(metric)}</span></div>
    <div className="metric-foot"><span>Baseline {fmt(row?.baseline)}</span>
      <b className={tone} title="Relative change from baseline">{pct(change)}</b></div>
  </section>;
}

const outcome: Record<string, string> = {
  FAIL: 'Performance regression detected',
  PASS: 'Performance within threshold',
  INCONCLUSIVE: 'Regression status inconclusive',
  INVALID: 'Measurements are not comparable',
};

function Interval({ gate }: { gate: Gate }) {
  if (!gate.interval || gate.delta == null) return null;
  const [low, high] = gate.interval;
  const min = Math.min(0, low, gate.threshold) - .02;
  const max = Math.max(high, gate.threshold, 0) + .02;
  const position = (value: number) => 100 * ((value - min) / (max - min)) + '%';
  return <div className="interval" role="img"
    aria-label={`${pct(low)} to ${pct(high)} confidence interval, ${pct(gate.threshold)} threshold`}>
    <div className="interval-track">
      <span className="interval-band" style={{ left: position(gate.threshold) }} />
      <span className="interval-threshold" style={{ left: position(gate.threshold) }} />
      <span className="interval-range" style={{ left: position(low), right: `calc(100% - ${position(high)})` }} />
      <span className="interval-dot" style={{ left: position(gate.delta) }} />
    </div>
    <div className="interval-legend"><span>◆ Estimated change</span><span>┆ Threshold</span></div>
  </div>;
}

function Overview({ data }: { data: Investigation }) {
  const [scenario, setScenario] = useState(data.config.gate_scenario);
  const [metric, setMetric] = useState(data.config.gate_metric);
  const chartRun = (side: string) => Object.values(data.experiments)
    .filter(row => row.scenario === scenario && row.id.startsWith('endpoints/' + side + '/'))
    .sort((a, b) => a.pair - b.pair);
  const a = chartRun('baseline'), b = chartRun('candidate');
  const rounds = [...new Set([...a, ...b].map(run => run.pair))].sort((x, y) => x - y);
  const source = Object.values(data.experiments)[0];
  const activeScenario = data.config.scenarios.find(row => row.id === scenario);
  const gate = data.gate;
  const activeMetrics = [...new Set([...data.metrics.map(row => row.metric), data.config.gate_metric])];
  return <>
    <div className="section-heading">
      <div><h1>Performance overview</h1><p>Baseline and candidate under the same workload.</p></div>
      <label className="inline-control">Workload<select value={scenario} onChange={event => setScenario(event.target.value)}>
        {data.config.scenarios.map(row => <option key={row.id}>{row.id}</option>)}</select></label>
    </div>
    <section className={'gate-banner gate-' + (gate?.decision ?? 'pending').toLowerCase()}>
      <div className="gate-content"><div className="gate-label"><span>Regression gate</span>
        <Status value={gate?.decision ?? 'PENDING'} /></div>
        <h2>{outcome[gate?.decision ?? ''] ?? 'Awaiting benchmark results'}</h2>
        <p>{nice(data.config.gate_metric)} · {data.config.gate_scenario}{gate?.pairs ? ' · ' + gate.pairs + ' paired rounds' : ''}</p></div>
      {gate?.delta != null && <div className="gate-result"><strong>{pct(gate.delta)}</strong>
        <span>{gate.delta < 0 ? 'improvement' : 'relative degradation'}</span></div>}
    </section>
    {(data.error || gate?.warnings?.length) ? <p className="notice">{data.error ?? gate?.warnings?.join(' · ')}</p> : null}
    <div className="metrics-grid">{['ttft_ms_p95', 'tpot_ms_p95', 'output_tokens_per_second'].map(name =>
      <MetricCard key={name} metric={name} row={data.metrics.find(row => row.metric === name && row.scenario === scenario)}
        higherIsBetter={data.config.metrics?.some(row => row.name === name && row.direction === 'higher') ?? name === 'output_tokens_per_second'} />)}</div>
    <div className="overview-grid">
      <Panel title="Paired benchmark rounds" extra={<select aria-label="Chart metric" value={metric}
        onChange={event => setMetric(event.target.value)}>{activeMetrics.map(item => <option key={item} value={item}>
          {nice(item)}</option>)}</select>}>
          {rounds.length ? <Chart title={'Paired benchmark rounds for ' + nice(metric)}
            options={lines(rounds.map(round => 'Round ' + (round + 1)),
              rounds.map(round => a.find(run => run.pair === round)?.metrics[metric] ?? null),
              rounds.map(round => b.find(run => run.pair === round)?.metrics[metric] ?? null), unit(metric))} />
            : <p className="empty">No completed endpoint rounds.</p>}
      </Panel>
      <Panel title="Decision evidence">{gate && <Interval gate={gate} />}<dl className="detail">
        <div><dt>{gate?.confidence ? fmt(gate.confidence * 100) + '% confidence interval' : 'Confidence interval'}</dt>
          <dd>{gate?.interval ? pct(gate.interval[0]) + ' to ' + pct(gate.interval[1]) : '—'}</dd></div>
        <div><dt>Regression threshold</dt><dd>{pct(gate?.threshold)}</dd></div>
        <div><dt>Baseline SHA</dt><dd className="mono" title={data.good}>{sha(data.good)}</dd></div>
        <div><dt>Candidate SHA</dt><dd className="mono" title={data.bad}>{sha(data.bad)}</dd></div>
        <div><dt>GPU</dt><dd>{source?.environment.gpu.map(item => item.name).join(', ') || 'Unavailable'}</dd></div>
      </dl><details className="method"><summary>How the gate decides</summary><p>{data.method}</p></details></Panel>
    </div>
    <Panel title="Workload and environment"><dl className="detail detail-grid">
      <div><dt>Model</dt><dd title={data.config.model_revision}>{data.config.model}</dd></div>
      <div><dt>Tokenizer</dt><dd title={data.config.tokenizer_revision}>{data.config.tokenizer}</dd></div>
      <div><dt>Prompt / output tokens</dt><dd>{activeScenario?.prompt_tokens} / {activeScenario?.output_tokens}</dd></div>
      <div><dt>Concurrency / requests</dt><dd>{activeScenario?.concurrency} / {activeScenario?.requests}</dd></div>
      <div><dt>GuideLLM version</dt><dd>{source?.environment.runtime.guidellm ?? '—'}</dd></div>
      <div><dt>Environment fingerprint</dt><dd className="mono">{source?.environment.fingerprint.slice(0, 12) ?? '—'}</dd></div>
    </dl></Panel>
  </>;
}

function CommitExplorer({ data }: { data: Investigation }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState('ALL');
  const bound = data.localization;
  const filtered = data.commits.filter(row => filter === 'ALL' || row.status === filter);
  const relevant = selected ? data.decisions.filter(row => row.sha === selected) : data.decisions;
  const decision = (row: Commit) => row.reason || (row.gate
    ? pct(row.gate.delta) + (row.gate.interval ? ' · CI ' + row.gate.interval.map(pct).join(' / ') : '')
    : 'Awaiting measurement');
  const culprit = data.commits.find(row => row.sha === bound?.first_fail);
  return <>
    <div className="section-heading"><div><h1>Commit explorer</h1><p>Follow the search from known good to the first measured failure.</p></div>
      {bound && <Status value={bound.status} />}</div>
    {bound && <section className="search-results">
      <div><span>Last measured pass</span><button className="text-button mono" onClick={() => setSelected(bound.last_pass)}>{sha(bound.last_pass)}</button></div>
      <div className="search-culprit"><span>{bound.status === 'LOCALIZED' ? 'First failing commit' : 'Earliest measured failure'}</span>
        <button className="text-button mono" onClick={() => setSelected(bound.first_fail)}>{sha(bound.first_fail)}</button>
        {culprit?.subject && <small>{culprit.subject}</small>}</div>
      <div><span>Candidates in interval</span><strong>{bound.candidates.length}</strong></div>
    </section>}
    <div className="commit-grid">
      <Panel title="First-parent history" extra={<select aria-label="Filter commits" value={filter}
        onChange={event => { setFilter(event.target.value); setSelected(null); }}>
          <option value="ALL">All results</option>{[...new Set(data.commits.map(row => row.status))].map(status =>
            <option key={status}>{status}</option>)}</select>}>
        <div className="timeline" aria-label="Commit timeline">{data.commits.map(row =>
          <button key={row.sha} className={'timeline-node state-' + row.status.toLowerCase() +
            (row.sha === bound?.first_fail ? ' first-fail' : '')}
            aria-label={sha(row.sha) + ' ' + row.status} aria-pressed={row.sha === selected}
            title={row.subject} onClick={() => { setFilter('ALL'); setSelected(row.sha === selected ? null : row.sha); }}>
            <i /><code>{sha(row.sha).slice(0, 6)}</code></button>)}</div>
        <div className="table-wrap"><table><thead><tr><th>Commit</th><th>Subject</th><th>Result</th></tr></thead>
          <tbody>{filtered.map(row => <tr key={row.sha} className={row.sha === selected ? 'selected' : ''}>
            <td><button className="text-button mono" onClick={() => setSelected(row.sha === selected ? null : row.sha)}>
              {sha(row.sha)}</button></td><td>{row.subject}</td><td><Status value={row.status} /></td></tr>)}</tbody></table></div>
        {bound && <p className="panel-note">{bound.assumption}.</p>}
      </Panel>
      <Panel title={selected ? 'Decisions for ' + sha(selected) : 'Bisect decision log'}
        extra={selected ? <button className="text-button" onClick={() => setSelected(null)}>Show all</button>
          : <span className="counter">{data.decisions.length} decisions</span>}>
        {relevant.length ? <ol className="decision-list">{relevant.map((row, index) =>
          <li key={row.sha + index}><div><code>{sha(row.sha)}</code><Status value={row.status} /></div>
            <p>{decision(row)}</p></li>)}</ol>
          : <p className="empty">No decisions have been recorded for this selection.</p>}
      </Panel>
    </div>
  </>;
}

function EvidenceRows({ title, rows }: { title: string; rows: (Evidence | string)[] }) {
  return <div className="evidence-group"><h3>{title}</h3>
    {rows.length ? rows.map((row, i) => typeof row === 'string' ? <p key={i}>{row}</p> :
      <details key={i} className="evidence-row">
        <summary><span>{nice(row.metric)} <small>{row.scenario}</small></span>
          <span className="mono">{fmt(row.baseline, 3)} → {fmt(row.candidate, 3)} <b>{pct(row.relative_delta)}</b></span></summary>
        <div className="evidence-refs">
          {([{ label: 'Baseline', ids: row.baseline_ids }, { label: 'Candidate', ids: row.candidate_ids }]).map(side =>
            <div key={side.label}><strong>{side.label}</strong>
              {side.ids.map(id => <a key={id} href={'#evidence?run=' + encodeURIComponent(id)}>{id}</a>)}</div>)}
        </div>
      </details>) : <p className="muted">None recorded.</p>}
  </div>;
}

function VerificationPanel({ data }: { data: Investigation }) {
  return <Panel title="Independent verification">{data.verifications.length ? data.verifications.map((row, i) =>
    <div className="verification" key={i}><div className="verification-meta"><Status value={row.status} />
      <code>{row.hypothesis} {row.commit && sha(row.commit)}</code></div>
      {row.evidence.map((gate, index) => <div className="verification-row" key={index}>
        <div><span>{index === 0 ? 'Good → reverted' : 'Bad → reverted'}</span><strong>{pct(gate.delta)}</strong></div>
        <small>{fmt(gate.baseline)} → {fmt(gate.candidate)} {unit(gate.metric)}</small>
        <small>CI {gate.interval?.map(pct).join(' to ') ?? '—'}</small>
      </div>)}<p>{row.reason}</p></div>)
    : <p className="empty">No independent intervention is recorded.</p>}</Panel>;
}

function FindingCard({ finding }: { finding: Finding }) {
  return <Panel title={finding.category} extra={<Status value={finding.verification} />}>
    <div className="finding">
      <div className="hypothesis"><code>{finding.id}</code><h3>{finding.summary}</h3></div>
      <EvidenceRows title="Supporting observations" rows={finding.support} />
      {!!finding.against.length && <EvidenceRows title="Counterevidence" rows={finding.against} />}
      {!!finding.missing.length && <EvidenceRows title="Missing evidence" rows={finding.missing} />}
      {!!finding.files.length && <EvidenceRows title="Related source files" rows={finding.files} />}
      {finding.verification !== 'SUPPORTED' &&
        <div className="next"><strong>Proposed experiment</strong><p>{finding.next}</p></div>}
    </div>
  </Panel>;
}

function Diagnosis({ data }: { data: Investigation }) {
  const [tokenMetric, setTokenMetric] = useState('itl_ms');
  const primary = data.findings.find(finding => finding.verification === 'SUPPORTED') ?? data.findings[0];
  const alternatives = data.findings.filter(finding => finding !== primary);
  const picked = data.diagnostic_comparison?.candidate;
  const group = picked && picked !== data.bad ? 'commit-' + picked.slice(0, 12) : 'endpoints';
  const distribution = (side: string, key: string) => {
    const values = Object.values(data.experiments).filter(row =>
      row.id.startsWith(group + '/' + side + '/') && row.scenario === data.config.gate_scenario)
      .flatMap(row => row.distributions[tokenMetric]?.[key] ?? []);
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  };
  const percentiles = ['p50', 'p90', 'p95', 'p99'];
  const a = percentiles.map(key => distribution('baseline', key));
  const b = percentiles.map(key => distribution('candidate', key));
  return <>
    <div className="section-heading"><div><h1>Root cause investigation</h1><p>Observed signals, source changes and independent verification.</p></div></div>
    {data.profiling && <p className="profile-jump"><a href="#profiling">Open linked Trace and stack comparison</a></p>}
    <div className="diagnosis-grid">
      {primary ? <FindingCard finding={primary} /> : <Panel title="Hypotheses">
        <p className="empty">No hypothesis reached the diagnostic trigger.</p></Panel>}
      <VerificationPanel data={data} />
    </div>
    {data.source_diff && <Panel title="Source changes" extra={<code className="muted">{data.source_diff_ref}</code>}>
      <pre className="diff">{data.source_diff.split('\n').map((line, i) => <span key={i}
        className={line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : ''}>{line || ' '}</span>)}</pre>
    </Panel>}
    {!!alternatives.length && <details className="alternatives">
      <summary>{alternatives.length} other {alternatives.length === 1 ? 'hypothesis' : 'hypotheses'}</summary>
      <div>{alternatives.map(finding => <FindingCard key={finding.id} finding={finding} />)}</div>
    </details>}
    <Panel title="Token latency distribution" extra={<select aria-label="Token latency metric" value={tokenMetric}
      onChange={event => setTokenMetric(event.target.value)}><option value="itl_ms">ITL</option><option value="tpot_ms">TPOT</option>
      <option value="ttft_ms">TTFT</option></select>}>
      {a.some(value => value != null) && b.some(value => value != null) ?
        <Chart title="Token latency percentiles averaged across runs" options={lines(percentiles, a, b, 'ms')} /> :
        <p className="empty">This metric is unavailable for the comparison.</p>}
      <p className="panel-note">Mean of per-run percentiles for {data.config.gate_scenario}. ITL uses emitted streaming events.</p>
    </Panel>
  </>;
}

function download(name: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 500);
}

function RunEvidence({ data, onCopy }: { data: Investigation; onCopy: () => void }) {
  const [selected, setSelected] = useState(() => {
    const linked = new URLSearchParams(window.location.hash.split('?')[1]).get('run');
    return linked && Object.hasOwn(data.experiments, linked)
      ? linked : data.gate?.candidate_ids?.[0] ?? Object.keys(data.experiments)[0] ?? '';
  });
  const run = data.experiments[selected];
  const paths = run ? Object.entries(run.artifacts) : [];
  return <>
    <div className="section-heading"><div><h1>Run evidence</h1><p>Inspect the benchmark runs and reproduce this investigation.</p></div></div>
    <div className="evidence-grid">
      <Panel title="Run record" extra={<span className="counter">{Object.keys(data.experiments).length} recorded runs</span>}>
        {run ? <><label className="run-picker">Benchmark run<select value={selected} onChange={event => setSelected(event.target.value)}>
          {Object.keys(data.experiments).map(id => <option key={id}>{id}</option>)}</select></label>
          <dl className="detail">
            <div><dt>Commit</dt><dd className="mono" title={run.commit}>{sha(run.commit)}</dd></div>
            <div><dt>Workload</dt><dd>{run.scenario}</dd></div>
            <div><dt>Paired round</dt><dd>{run.pair + 1}</dd></div>
            {Object.entries(run.metrics).slice(0, 7).map(([name, value]) =>
              <div key={name}><dt>{nice(name)}</dt><dd>{fmt(value, 3)} {unit(name)}</dd></div>)}
          </dl>
          {!!run.warnings.length && <ul className="run-warnings">{run.warnings.map(warning => <li key={warning}>{warning}</li>)}</ul>}
        </> : <p className="empty">No benchmark rounds were recorded.</p>}
      </Panel>
      <div>
        <Panel title="Reproduce locally" extra={<button className="compact" onClick={onCopy}>Copy command</button>}>
          <pre className="command"><code>{data.reproduction}</code></pre>
        </Panel>
        <Panel title="Artifact paths">{paths.length ? <dl className="artifact-paths">{paths.map(([name, path]) =>
          <div key={name}><dt>{name}</dt><dd><code>{path}</code></dd></div>)}</dl>
          : <p className="empty">No artifacts are available for this run.</p>}</Panel>
      </div>
    </div>
  </>;
}

function App() {
  const embedded = document.getElementById('investigation-data');
  const markdown = document.getElementById('investigation-markdown');
  const [data, setData] = useState<Investigation | null>(() => embedded ? JSON.parse(embedded.textContent ?? '') : null);
  const [items, setItems] = useState<{ id: string; state: string }[]>([]);
  const [selected, setSelected] = useState('');
  const [page, setPage] = useState<Page>(currentPage);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => {
    const onHash = () => setPage(currentPage());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  useEffect(() => {
    if (embedded) return;
    fetch('/api/investigations').then(response => {
      if (!response.ok) throw new Error();
      return response.json();
    }).then((results: { id: string; state: string }[]) => {
      setItems(results);
      if (results.length) setSelected((results.find(row => row.id === 'regression') ?? results[0]).id);
      else setError('The local store has no investigations. Run the CLI to create one.');
    }).catch(() => setError('The local investigation API is unavailable.'));
  }, [embedded]);
  useEffect(() => {
    if (!selected || embedded) return;
    const request = new AbortController();
    fetch('/api/investigations/' + encodeURIComponent(selected), { signal: request.signal })
      .then(response => {
        if (!response.ok) throw new Error();
        return response.json();
      }).then((record: Investigation) => { setData(record); setError(''); })
      .catch(() => { if (!request.signal.aborted) setError('This investigation could not be loaded.'); });
    return () => request.abort();
  }, [selected, embedded]);

  async function copyCommand() {
    if (!data) return;
    try {
      await navigator.clipboard.writeText(data.reproduction);
      setNotice('Command copied');
    } catch {
      download('reproduce.sh', data.reproduction + '\n', 'text/plain');
      setNotice('Command downloaded');
    }
  }

  return <div className="app">
    <aside className="sidebar">
      <a href="#overview" className="brand"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><span>RegressLens</span></a>
      <nav aria-label="Investigation views">{(Object.entries(navigation) as [Page, { text: string; symbol: string }][]).map(([key, nav]) =>
        <a key={key} href={'#' + key} aria-current={key === page ? 'page' : undefined}
          className={page === key ? 'nav-active' : ''}><span aria-hidden="true">{nav.symbol}</span>{nav.text}</a>)}</nav>
      <div className="sidebar-bottom"><span className="status-dot" />{embedded ? 'Offline report' : 'Local workspace'}</div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <div className="top-left"><span>Investigation</span>
          {embedded ? <code>{data?.id}</code> : <select aria-label="Investigation" value={selected}
            onChange={event => setSelected(event.target.value)}>{items.map(row => <option key={row.id}>{row.id}</option>)}</select>}
          {data && <span className="origin">{caseNames[data.case_type] ?? data.case_type}</span>}</div>
        {data && <div className="top-actions">
          <button onClick={copyCommand}>Copy command</button>
          <details className="exports"><summary className="primary">Export report</summary><div>{embedded ? <>
            {markdown && <button onClick={() => download(data.id + '.md', JSON.parse(markdown.textContent ?? '""'), 'text/markdown')}>Markdown</button>}
            <button onClick={() => download(data.id + '.json', JSON.stringify(data, null, 2), 'application/json')}>JSON</button>
          </> : ['html', 'markdown', 'json'].map(format =>
            <a key={format} href={'/api/investigations/' + encodeURIComponent(data.id) + '/report?format=' + format}>{format.toUpperCase()}</a>)}</div></details>
        </div>}
      </header>
      {notice && <div className="toast" role="status"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="Dismiss">×</button></div>}
      <main>{error && <p className="notice" role="alert">{error}</p>}
        {!data && !error && <p role="status">Loading investigations…</p>}
        {data && (page === 'overview' ? <Overview key={data.id} data={data} /> :
          page === 'commits' ? <CommitExplorer key={data.id} data={data} /> :
            page === 'diagnosis' ? <Diagnosis key={data.id} data={data} /> :
              page === 'profiling' ? <Profiling key={data.id} data={data} offline={!!embedded} /> :
              <RunEvidence key={data.id} data={data} onCopy={copyCommand} />)}
      </main>
    </div>
  </div>;
}

createRoot(document.getElementById('root')!).render(<App />);
