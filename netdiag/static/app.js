'use strict';
const $ = id => document.getElementById(id);
const queryControls = ['range','probe','target','metric','follow','refresh','start','end','apply','bucket','clear','previous','next'];
const colors = ['#64dac3','#90bfff','#e2acff','#f1c675','#ff9393','#90d788','#f59fc8','#b2bad8'];
let metadata = {targets: []}, series = {series: []}, interval = null, detail = null;
let cursors = [null], page = 0, nextCursor = null, busy = false, pending = false, bounds = [];
const number = new Intl.NumberFormat(undefined, {maximumFractionDigits: 2});
const date = ts => new Date(ts * 1000).toLocaleString(undefined, {hour12: false});
const ms = value => value == null ? '—' : number.format(value) + ' ms';

function cell(row, value, className) {
  const td = row.insertCell();
  td.textContent = value;
  if (className) td.className = className;
  return td;
}
function empty(body, message, columns) {
  body.replaceChildren();
  const td = cell(body.insertRow(), message);
  td.colSpan = columns;
}
async function api(path, params) {
  const response = await fetch('/api/' + path + (params ? '?' + new URLSearchParams(params) : ''), {signal: AbortSignal.timeout(15000)});
  if (!response.ok) throw new Error(response.status === 503 ? 'The dataset is unavailable. Check that collection is running and the dataset can be read.' : `Dashboard request failed (${response.status}).`);
  return response.json();
}
function selectedTargets() {
  return metadata.targets.filter(t => (!$('probe').value || t.probe === $('probe').value) && (!$('target').value || String(t.id) === $('target').value));
}
function params() {
  const query = {start: interval[0], end: interval[1]};
  if ($('probe').value || $('target').value) query.targets = selectedTargets().map(t => t.id).join(',');
  return query;
}
function fillFilters() {
  for (const [id, options] of [['probe', [...new Set(metadata.targets.map(t => t.probe))].map(p => [p,p])], ['target', metadata.targets.map(t => [String(t.id),`${t.name} · ${t.probe}`])]]) {
    const control = $(id), selected = control.value;
    control.replaceChildren(new Option(id === 'probe' ? 'All probe types' : 'All targets', ''));
    for (const [value,label] of options) control.add(new Option(label,value));
    if (options.some(([value]) => value === selected)) control.value = selected;
  }
}
function renderContext() {
  $('storage').textContent = `${number.format(metadata.storage_bytes / 1048576)} MiB on disk · ${metadata.first_ts == null ? 'No measurements yet' : date(metadata.first_ts) + ' → ' + date(metadata.last_ts)}`;
  $('sessions').replaceChildren();
  for (const session of (metadata.sessions || []).slice(0,20)) {
    const li = document.createElement('li');
    li.textContent = `${date(session.started)} → ${session.ended ? date(session.ended) : 'not closed'} · ${session.status || 'recorded'} · session ${session.id}`;
    const configuration = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent = 'Session configuration';
    const pre = document.createElement('pre'); pre.textContent = typeof session.config === 'string' ? session.config : JSON.stringify(session.config, null, 2);
    configuration.append(summary, pre); li.append(configuration); $('sessions').append(li);
  }
  if (!metadata.sessions?.length) { const li = document.createElement('li'); li.textContent = 'No collection sessions recorded.'; $('sessions').append(li); }
}
function renderSeries() {
  const targets = new Map(metadata.targets.map(t => [t.id,t]));
  const streams = series.series.filter(s => selectedTargets().some(t => t.id === s.target_id));
  const points = streams.flatMap(s => s.points);
  const count = points.reduce((n,p) => n + p.count, 0), failures = points.reduce((n,p) => n + p.failures,0);
  $('attempts').textContent = number.format(count);
  $('failures').textContent = number.format(failures);
  $('rate').textContent = `${count ? number.format(100 * failures / count) : '0'}% of measured attempts failed`;
  $('gaps').textContent = number.format(points.reduce((n,p) => n + p.gaps, 0));
  $('peak').textContent = ms(points.reduce((peak,p) => p.latency_max_ms == null ? peak : Math.max(peak ?? 0,p.latency_max_ms),null));
  $('window').textContent = date(interval[0]) + ' → ' + date(interval[1]);
  $('comparison').replaceChildren();
  let reconnects = 0;
  for (const s of streams) {
    const t = targets.get(s.target_id), row = $('comparison').insertRow();
    const attempts = s.points.reduce((n,p) => n + p.count,0), failed = s.points.reduce((n,p) => n + p.failures,0);
    const latencyPoints = s.points.filter(p => p.latency_avg_ms != null);
    const timed = latencyPoints.reduce((n,p) => n + (p.latency_count ?? p.successes),0);
    const p50 = s.points.map(p => p.latency_p50_ms).filter(p => p != null), p95 = s.points.map(p => p.latency_p95_ms).filter(p => p != null);
    cell(row, `${t?.name || s.target_id} / ${t?.probe || ''}`);
    cell(row, number.format(attempts));
    cell(row, `${attempts ? number.format(100 * failed / attempts) : '0'}% (${failed}) ${t?.probe === 'icmp' ? 'no reply' : 'exchange failures'}`, failed ? 'bad' : 'good');
    cell(row, ms(timed ? latencyPoints.reduce((n,p) => n + p.latency_avg_ms * (p.latency_count ?? p.successes),0) / timed : null));
    cell(row, `${p50.length ? ms(Math.min(...p50)) + '–' + ms(Math.max(...p50)) : '—'} / ${p95.length ? ms(Math.min(...p95)) + '–' + ms(Math.max(...p95)) : '—'}`);
    cell(row, number.format(s.points.reduce((n,p) => n + p.gaps,0)));
    reconnects += s.points.reduce((n,p) => n + (p.reconnected || 0),0);
  }
  if (!streams.length) empty($('comparison'), 'No matching probe streams in this interval.',6);
  $('outages').textContent = `${reconnects} reconnects. Observed failure runs are listed below; a failed probe is not proof of a complete network outage. Percentiles are approximate bucket estimates, not a whole-window percentile.`;
  const previous = $('bucket').value;
  bounds = [...new Set(points.map(p => p.ts))].sort((a,b) => a-b);
  $('bucket').replaceChildren(new Option('Entire selected window',''));
  for (const ts of bounds) $('bucket').add(new Option(`${date(ts)} → ${date(Math.min(ts + series.bucket_seconds, interval[1]))}`, String(ts)));
  if (bounds.some(ts => String(ts) === previous)) $('bucket').value = previous;
  drawChart();
}
function drawChart() {
  const svg = $('chart');
  svg.querySelectorAll(':scope > :not(title):not(desc)').forEach(node => node.remove());
  $('legend').replaceChildren();
  const metric = $('metric').value;
  const value = p => metric === 'failure_rate' ? (p.count ? p.failures / p.count * 100 : null) : p[metric];
  const streams = series.series.filter(s => selectedTargets().some(t => t.id === s.target_id));
  const values = streams.flatMap(s => s.points.map(value)).filter(v => v != null);
  const max = values.reduce((peak,v) => Math.max(peak,v),1), span = interval[1] - interval[0];
  function shape(type, attributes) { const node = document.createElementNS('http://www.w3.org/2000/svg', type); for (const [key,v] of Object.entries(attributes)) node.setAttribute(key,v); svg.append(node); return node; }
  for (let i=0; i<=4; i++) {
    shape('line',{x1:55,x2:990,y1:15+i*50,y2:15+i*50,stroke:'#303d4b'});
    shape('text',{x:2,y:19+i*50}).textContent = number.format(max*(1-i/4));
  }
  if (detail) shape('rect',{x:55+(detail[0]-interval[0])/span*935,y:15,width:Math.max(2,(detail[1]-detail[0])/span*935),height:200,fill:'#90bfff',opacity:'.12'});
  streams.forEach((s,index) => {
    const target = metadata.targets.find(t => t.id === s.target_id), label = document.createElement('span');
    label.textContent = `${target?.name || s.target_id} / ${target?.probe || ''}`; label.style.setProperty('--series',colors[index%colors.length]); $('legend').append(label);
    let path = '', last = null;
    for (const p of s.points) {
      const v = value(p);
      if (v == null) {last = null; continue;}
      const x = Math.max(55,55+(p.ts-interval[0])/span*935), y = 215-v/max*200;
      path += `${last === null || p.ts-last > series.bucket_seconds*1.5 ? 'M' : 'L'}${x},${y} `;
      shape('circle',{cx:x,cy:y,r:2,fill:colors[index%colors.length]});
      last = p.ts;
    }
    shape('path',{d:path,fill:'none',stroke:colors[index%colors.length],'stroke-width':2});
  });
  $('chart-note').textContent = values.length ? `${$('metric').selectedOptions[0].text} · ${number.format(series.bucket_seconds)}-second buckets. Blank spans have no measurements. ${series.boundary_buckets_include_full_interval ? 'Boundary buckets include their full summary interval; raw details use exact selected times.' : 'Measurements use the exact selected interval.'}` : 'No measurements match this window. Start collection or choose another timeframe.';
  $('chart-description').textContent = `${$('chart-note').textContent} ${$('attempts').textContent} attempts, ${$('failures').textContent} failures, ${$('gaps').textContent} local measurement gaps.`;
  $('axis-start').textContent = date(interval[0]); $('axis-end').textContent = date(interval[1]);
}
async function renderSamples() {
  const query = params();
  if (detail) [query.start,query.end] = detail;
  if (cursors[page] != null) query.before_id = cursors[page];
  query.limit = 100;
  const result = selectedTargets().length ? await api('samples',query) : {samples:[],next_before_id:null};
  nextCursor = result.next_before_id;
  $('samples').replaceChildren();
  for (const sample of result.samples) {
    const row = $('samples').insertRow(), target = metadata.targets.find(t => t.id === sample.target_id);
    const button = document.createElement('button'); button.type = 'button'; button.textContent = date(sample.ts) + '.' + String(Math.floor(sample.ts*1000)%1000).padStart(3,'0');
    button.addEventListener('click',() => { $('record-body').textContent = JSON.stringify({...sample, timestamp_utc:new Date(sample.ts*1000).toISOString(), target},null,2); $('record').open = true; });
    cell(row,'').append(button);
    cell(row,`${target?.name || sample.target_id} / ${target?.probe || ''}`);
    cell(row,sample.success == null ? `Local gap: ${sample.error || 'not measured'} (${sample.missed || 1} missed)` : sample.success ? (sample.reconnected ? 'OK · reconnected' : 'OK') : sample.error || 'Failed',sample.success ? 'good' : 'bad');
    cell(row,ms(sample.latency_ms)); cell(row,`${ms(sample.dns_ms)} / ${ms(sample.connect_ms)} / ${ms(sample.tls_ms)}`); cell(row,sample.http_status ?? '—');
  }
  if (!result.samples.length) empty($('samples'),'No individual results in this interval.',6);
  $('detail-range').textContent = date(query.start) + ' → ' + date(query.end);
  $('page-status').textContent = `Page ${page+1} · ${result.samples.length} results`;
}
async function refresh(reset = false) {
  if (busy) { pending = true; return; }
  if (reset) { cursors = [null]; page = 0; }
  busy = true;
  let succeeded = false;
  for (const id of queryControls) $(id).disabled = true;
  $('status').textContent = 'Refreshing dataset…';
  try {
    metadata = await api('metadata'); fillFilters(); renderContext();
    if (!interval || ($('follow').checked && $('range').value !== 'custom')) {
      const end = Date.now()/1000;
      interval = [$('range').value === 'all' ? (metadata.first_ts ?? end-3600) : end-Number($('range').value),end];
      if (interval[0] >= interval[1]) interval[0] = interval[1]-1;
    }
    series = selectedTargets().length ? await api('series',params()) : {series:[],bucket_seconds:60};
    renderSeries(); await renderSamples();
    const runs = selectedTargets().length ? await api('outages', {...params(),start:detail?.[0] ?? interval[0],end:detail?.[1] ?? interval[1],limit:100}) : {outages:[]};
    $('outage-list').replaceChildren();
    for (const run of runs.outages) {
      const target = metadata.targets.find(t => t.id === run.target_id), li = document.createElement('li');
      li.textContent = `${target?.name || run.target_id} / ${target?.probe || ''}: ${date(run.started)} → ${run.ended == null ? 'no recovery observed' : date(run.ended)} · ${number.format(run.duration_seconds)} s${run.end_reason === 'recovered' ? ' to observed recovery' : ' observed lower bound (' + (run.end_reason || 'open run') + ')'} · ${run.failures} failed attempts · session ${run.session_id}`;
      $('outage-list').append(li);
    }
    if (!runs.outages.length || runs.truncated) { const li = document.createElement('li'); li.textContent = runs.truncated ? 'Showing the latest 100 runs. Narrow the interval to inspect more.' : 'No observed failure runs in this interval.'; $('outage-list').append(li); }
    succeeded = true;
    $('error').hidden = true; $('status').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    $('error').textContent = error.message + ' Retrying every five seconds; you can also use Refresh now.'; $('error').hidden = false; $('status').textContent = 'Refresh failed · showing last available data';
  } finally {
    busy = false;
    for (const id of queryControls) $(id).disabled = false;
    $('previous').disabled = !succeeded || page === 0;
    $('next').disabled = !succeeded || nextCursor == null;
    if (pending) {pending = false; refresh();}
  }
}
function selectBucket() {
  if (busy) return;
  detail = $('bucket').value ? [Math.max(Number($('bucket').value),interval[0]),Math.min(Number($('bucket').value)+series.bucket_seconds,interval[1])] : null;
  if (detail) $('follow').checked = false;
  refresh(true);
}
$('bucket').addEventListener('change',selectBucket);
$('clear').addEventListener('click',() => { if (busy) return; $('bucket').value = ''; detail = null; refresh(true); });
$('chart').addEventListener('click',event => {
  if (busy || !bounds.length) return;
  const rect = $('chart').getBoundingClientRect();
  const ts = interval[0]+Math.max(0,Math.min(1,((event.clientX-rect.left)/rect.width*1000-55)/935))*(interval[1]-interval[0]);
  const bucket = bounds.reduce((best,t) => Math.abs(t-ts)<Math.abs(best-ts) ? t : best,bounds[0]);
  $('bucket').value = String(bucket); selectBucket();
});
$('metric').addEventListener('change',() => { if (!busy && interval) drawChart(); });
for (const id of ['target','probe']) $(id).addEventListener('change',() => { if (busy) return; detail = null; $('bucket').value = ''; refresh(true); });
$('range').addEventListener('change',() => {
  if (busy) return;
  $('custom').hidden = $('range').value !== 'custom';
  if ($('range').value === 'custom') { $('follow').checked = false; return; }
  const end = Date.now()/1000;
  interval = [$('range').value === 'all' ? (metadata.first_ts ?? end-3600) : end-Number($('range').value),end];
  detail = null; $('bucket').value = ''; refresh(true);
});
$('apply').addEventListener('click',() => {
  if (busy) return;
  const start = new Date($('start').value).getTime()/1000, end = new Date($('end').value).getTime()/1000;
  if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end || start < 0) { $('error').textContent = 'Enter a valid start and end time; the end must be after the start.'; $('error').hidden = false; return; }
  interval = [start,end]; detail = null; $('bucket').value = ''; refresh(true);
});
$('follow').addEventListener('change',() => { if (busy) return; if ($('follow').checked) { $('range').value = $('range').value === 'custom' ? '3600' : $('range').value; $('custom').hidden = true; detail = null; $('bucket').value = ''; } refresh(true); });
$('refresh').addEventListener('click',() => refresh());
$('previous').addEventListener('click',() => { if (!busy && !$('previous').disabled && page>0) {page--; refresh();} });
$('next').addEventListener('click',() => { if (!busy && !$('next').disabled && nextCursor != null) { $('follow').checked = false; cursors[++page] = nextCursor; refresh(); } });
setInterval(() => refresh(),5000);
refresh();
