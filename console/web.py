#!/usr/bin/env python3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import signal
import socketserver
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlparse

from . import config
from . import __version__
from .client import CollectorClient
from .export import create_export, resolve_range


LOGGER = logging.getLogger('npu_console')


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NPU 集群监控</title>
<style>
:root{color-scheme:dark;--bg:#08111f;--panel:#111c30;--line:#24324a;--text:#e5e7eb;--muted:#94a3b8;--blue:#38bdf8}
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;margin:0;background:var(--bg);color:var(--text)}
main{max-width:1500px;margin:auto;padding:24px}.head{display:flex;justify-content:space-between;align-items:end;gap:16px;flex-wrap:wrap}
.muted{color:var(--muted)}.toolbar,.filters{display:flex;gap:10px;align-items:end;flex-wrap:wrap}.filters{margin:18px 0}
label{font-size:12px;color:var(--muted);display:grid;gap:5px}input,select,button{border:1px solid var(--line);border-radius:7px;background:#0d1728;color:var(--text);padding:8px 10px}
button{cursor:pointer;background:#164e63}button.secondary{background:#172033}.cards{display:grid;grid-template-columns:repeat(7,minmax(125px,1fr));gap:12px;margin:18px 0}
.tile,.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:15px}.value{font-size:25px;font-weight:700;margin-top:6px}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-top:12px}.grid>*{min-width:0}.panel h2{font-size:17px;margin:0 0 12px}.table-wrap{overflow:auto;max-height:430px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);position:sticky;top:0;background:var(--panel)}
.online,.resolved{color:#4ade80}.degraded,.stale,.warning{color:#fbbf24}.offline,.critical,.active{color:#f87171}canvas{width:100%;height:270px}
.pager{display:flex;justify-content:space-between;align-items:center;margin-top:10px}.wide{grid-column:1/-1}.error{color:#f87171}
@media(max-width:1300px){.cards{grid-template-columns:repeat(4,1fr)}.grid{grid-template-columns:1fr}}
@media(max-width:700px){main{padding:14px}.cards{grid-template-columns:repeat(2,1fr)}}
</style></head><body><main>
<div class="head"><div><h1>NPU 集群监控</h1><div class="muted">查询、趋势、告警与报表导出</div></div><div id="updated" class="muted">加载中…</div></div>
<div class="filters">
 <label>集群<select id="cluster"><option value="">全部集群</option></select></label>
 <label>开始时间<input id="start" type="datetime-local"></label>
 <label>结束时间<input id="end" type="datetime-local"></label>
 <label>聚合粒度<select id="bucket"><option value="300">5分钟</option><option value="900">15分钟</option><option value="3600">1小时</option><option value="86400">1天</option></select></label>
 <label>节点<input id="historyNode" placeholder="node_id"></label>
 <label>卡号<input id="card" type="number" min="0" placeholder="全部"></label>
 <button onclick="refreshAll()">查询</button><button class="secondary" onclick="exportData('csv')">导出 CSV</button><button class="secondary" onclick="exportData('xlsx')">导出 XLSX</button>
</div>
<div class="cards">
 <div class="tile"><div class="muted">集群</div><div id="clusterCount" class="value">-</div></div>
 <div class="tile"><div class="muted">节点</div><div id="nodes" class="value">-</div></div>
 <div class="tile"><div class="muted">在线 / 异常</div><div id="online" class="value">-</div></div>
 <div class="tile"><div class="muted">登记卡数</div><div id="registered" class="value">-</div></div>
 <div class="tile"><div class="muted">新鲜卡数</div><div id="fresh" class="value">-</div></div>
 <div class="tile"><div class="muted">利用率</div><div id="util" class="value">-</div></div>
 <div class="tile"><div class="muted">HBM</div><div id="hbm" class="value">-</div></div>
</div>
<div id="freshness" class="panel muted">正在读取数据新鲜度…</div>
<div class="grid">
 <div class="panel"><h2>利用率趋势</h2><canvas id="chart" width="1100" height="270"></canvas></div>
 <div class="panel"><h2>集群分组</h2><div class="table-wrap"><table><thead><tr><th>集群</th><th>节点</th><th>卡数</th><th>利用率</th></tr></thead><tbody id="clusterRows"></tbody></table></div></div>
 <div class="panel wide"><div class="head"><h2>节点明细</h2><div class="toolbar"><input id="nodeSearch" placeholder="搜索节点"><select id="nodeState"><option value="">全部状态</option><option>online</option><option>degraded</option><option>stale</option><option>offline</option></select><button onclick="nodePage=1;loadNodes()">筛选</button></div></div><div class="table-wrap"><table><thead><tr><th>集群</th><th>节点</th><th>状态</th><th>卡数</th><th>利用率</th><th>HBM</th><th>数据年龄</th><th>最近采样</th></tr></thead><tbody id="nodeRows"></tbody></table></div><div class="pager"><button class="secondary" onclick="changeNodePage(-1)">上一页</button><span id="nodePager" class="muted"></span><button class="secondary" onclick="changeNodePage(1)">下一页</button></div></div>
 <div class="panel wide"><h2>告警记录</h2><div class="table-wrap"><table><thead><tr><th>开始时间</th><th>集群</th><th>节点</th><th>类型</th><th>级别</th><th>状态</th><th>信息</th><th>恢复时间</th></tr></thead><tbody id="alertRows"></tbody></table></div></div>
 <div class="panel wide"><h2>原始采样记录</h2><div class="table-wrap"><table><thead><tr><th>采样时间</th><th>集群</th><th>节点</th><th>状态</th><th>卡覆盖</th><th>缺失卡</th><th>接收时间</th></tr></thead><tbody id="sampleRows"></tbody></table></div></div>
</div>
<script>
const $=s=>document.querySelector(s),pct=v=>v==null?'-':Number(v).toFixed(2)+'%',esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let nodePage=1,nodePages=1;
function iso(id){const v=$(id).value;return v?new Date(v).toISOString():''}function params(extra={}){const p=new URLSearchParams();const values={start:iso('#start'),end:iso('#end'),bucket:$('#bucket').value,cluster_id:$('#cluster').value,node_id:$('#historyNode').value.trim(),card_id:$('#card').value,...extra};for(const[k,v]of Object.entries(values))if(v!==''&&v!=null)p.set(k,v);return p}
async function get(path,p){const r=await fetch(path+(p?'?'+p:''),{cache:'no-store'});const body=await r.json();if(!r.ok)throw Error(body.error||r.status);return body}
async function loadClusters(){const data=await get('/api/clusters');const selected=$('#cluster').value;$('#cluster').innerHTML='<option value="">全部集群</option>'+data.items.map(c=>`<option value="${esc(c.cluster_id)}">${esc(c.cluster_id)}</option>`).join('');$('#cluster').value=selected;$('#clusterRows').innerHTML=data.items.map(c=>`<tr><td>${esc(c.cluster_id)}</td><td>${c.total_nodes}</td><td>${c.fresh_collected_cards}/${c.registered_expected_cards}</td><td>${pct(c.utilization_avg)}</td></tr>`).join('');$('#clusterCount').textContent=data.items.length}
async function loadSnapshot(){const p=new URLSearchParams();if($('#cluster').value)p.set('cluster_id',$('#cluster').value);const s=await get('/api/snapshot',p);const registered=s.registered_expected_cards??s.expected_cards,fresh=s.fresh_collected_cards??s.active_collected_cards,fleet=s.fleet_freshness_coverage_percent??s.coverage_percent;$('#updated').textContent='更新：'+s.generated_at;$('#nodes').textContent=s.total_nodes;$('#online').textContent=(s.node_counts.online+s.node_counts.degraded)+' / '+(s.node_counts.stale+s.node_counts.offline);$('#registered').textContent=registered;$('#fresh').textContent=fresh+' / '+registered;$('#util').textContent=pct(s.utilization_avg);$('#hbm').textContent=pct(s.hbm_percent);$('#freshness').textContent=`集群新鲜度：${pct(fleet)} · 当前上报完整率：${pct(s.reporting_sample_coverage_percent)} · 最近已知卡数：${s.last_known_collected_cards} · 活跃告警：${s.active_alert_count??'-'}`}
function draw(points){const c=$('#chart'),x=c.getContext('2d'),w=c.width,h=c.height,p=38;x.clearRect(0,0,w,h);x.strokeStyle='#334155';x.fillStyle='#94a3b8';x.font='12px sans-serif';for(let v=0;v<=100;v+=25){const y=h-p-(h-2*p)*v/100;x.beginPath();x.moveTo(p,y);x.lineTo(w-p,y);x.stroke();x.fillText(v+'%',3,y+4)}const usable=points.filter(q=>q.utilization_avg!=null);if(!usable.length){x.fillText('当前范围没有数据',p+10,p+10);return}x.strokeStyle='#38bdf8';x.lineWidth=2;x.beginPath();let begun=false;points.forEach((q,i)=>{if(q.utilization_avg==null){begun=false;return}const xx=p+(w-2*p)*i/Math.max(1,points.length-1),yy=h-p-(h-2*p)*q.utilization_avg/100;if(begun)x.lineTo(xx,yy);else{x.moveTo(xx,yy);begun=true}});x.stroke()}
async function loadHistory(){const data=await get('/api/history',params());draw(data.points||[])}
async function loadNodes(){const p=new URLSearchParams({page:nodePage,page_size:50});if($('#cluster').value)p.set('cluster_id',$('#cluster').value);if($('#nodeState').value)p.set('state',$('#nodeState').value);if($('#nodeSearch').value.trim())p.set('q',$('#nodeSearch').value.trim());const d=await get('/api/nodes',p);nodePages=Math.max(1,d.pages);if(nodePage>nodePages){nodePage=nodePages;return loadNodes()}$('#nodeRows').innerHTML=d.items.map(n=>`<tr><td>${esc(n.cluster_id)}</td><td>${esc(n.node_id)}</td><td class="${esc(n.state)}">${esc(n.state)}</td><td>${n.collected_cards}/${n.expected_cards}</td><td>${pct(n.fresh_utilization_avg)}</td><td>${pct(n.fresh_hbm_percent)}</td><td>${n.age_seconds}s</td><td>${esc(n.last_collected_at||'-')}</td></tr>`).join('');$('#nodePager').textContent=`第 ${d.page}/${Math.max(1,d.pages)} 页，共 ${d.total} 个节点`}
function changeNodePage(delta){const next=Math.min(nodePages,Math.max(1,nodePage+delta));if(next!==nodePage){nodePage=next;loadNodes()}}
async function loadAlerts(){const p=params({page:1,page_size:100});const d=await get('/api/alerts',p);$('#alertRows').innerHTML=d.items.map(a=>`<tr><td>${esc(a.started_at)}</td><td>${esc(a.cluster_id)}</td><td>${esc(a.node_id)}</td><td>${esc(a.alert_type)}</td><td class="${esc(a.severity)}">${esc(a.severity)}</td><td class="${esc(a.status)}">${esc(a.status)}</td><td>${esc(a.message)}</td><td>${esc(a.resolved_at||'-')}</td></tr>`).join('')}
async function loadSamples(){const p=params({page:1,page_size:100});p.delete('bucket');p.delete('card_id');const d=await get('/api/samples',p);$('#sampleRows').innerHTML=d.items.map(s=>`<tr><td>${esc(s.collected_at)}</td><td>${esc(s.cluster_id)}</td><td>${esc(s.node_id)}</td><td>${esc(s.status)}</td><td>${s.collected_cards}/${s.expected_cards} (${pct(s.coverage_percent)})</td><td>${esc((s.missing_card_ids||[]).join(','))}</td><td>${esc(s.received_at)}</td></tr>`).join('')}
function exportData(format){const p=params({format});p.delete('bucket');location.href='/api/export?'+p}
async function refreshAll(){try{$('#updated').classList.remove('error');await Promise.all([loadClusters(),loadSnapshot(),loadHistory(),loadNodes(),loadAlerts(),loadSamples()])}catch(e){$('#updated').textContent='查询失败：'+e.message;$('#updated').classList.add('error')}}
function setDefaults(){const end=new Date(),start=new Date(end-24*3600*1000);const local=d=>new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,16);$('#start').value=local(start);$('#end').value=local(end)}setDefaults();refreshAll();setInterval(()=>{loadSnapshot().catch(()=>{})},10000);
</script></main></body></html>'''


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _first(query, name):
    value = query.get(name, [None])[0]
    return value if value not in ('', None) else None


def _int(query, name, default, minimum=0, maximum=1000000):
    value = int(query.get(name, [str(default)])[0])
    if value < minimum or value > maximum:
        raise ValueError('{} must be between {} and {}'.format(
            name, minimum, maximum
        ))
    return value


def _optional_int(query, name, minimum=0, maximum=63):
    raw = _first(query, name)
    if raw is None:
        return None
    value = int(raw)
    if value < minimum or value > maximum:
        raise ValueError('{} must be between {} and {}'.format(
            name, minimum, maximum
        ))
    return value


def _choice(query, name, choices):
    value = _first(query, name)
    if value is not None and value not in choices:
        raise ValueError('invalid {}'.format(name))
    return value


def _range_text(query, max_days):
    start, end = resolve_range(
        _first(query, 'start'), _first(query, 'end'),
        hours=_int(query, 'hours', 24, 1, 24 * max_days),
        max_days=max_days,
    )
    return (
        start.isoformat(timespec='seconds'), end.isoformat(timespec='seconds')
    )


def make_handler(client):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_text, *args):
            LOGGER.info('%s - %s', self.client_address[0], format_text % args)

        def send_bytes(self, status, content_type, payload, disposition=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            if disposition:
                self.send_header('Content-Disposition', disposition)
            self.end_headers()
            self.wfile.write(payload)

        def send_json(self, status, value):
            self.send_bytes(
                status, 'application/json; charset=utf-8',
                (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
            )

        def send_file(self, path, content_type, filename):
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(size))
            self.send_header('Content-Disposition', 'attachment; filename="{}"'.format(filename))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with open(path, 'rb') as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == '/':
                    self.send_bytes(200, 'text/html; charset=utf-8', INDEX_HTML.encode('utf-8'))
                elif parsed.path == '/health':
                    self.send_json(200, {
                        'status': 'healthy', 'console_version': __version__,
                        'collector': client.health(),
                    })
                elif parsed.path == '/api/snapshot':
                    self.send_json(200, client.snapshot(_first(query, 'cluster_id')))
                elif parsed.path == '/api/clusters':
                    self.send_json(200, client.clusters())
                elif parsed.path == '/api/nodes':
                    self.send_json(200, client.nodes(
                        _int(query, 'page', 1, 1),
                        _int(query, 'page_size', 50, 1, 500),
                        _choice(query, 'state', ('online', 'degraded', 'stale', 'offline')),
                        _first(query, 'cluster_id'),
                        _first(query, 'q'),
                    ))
                elif parsed.path == '/api/history':
                    start, end = _range_text(query, 180)
                    self.send_json(200, client.series(
                        start, end, _int(query, 'bucket', 300, 60, 86400),
                        _first(query, 'node_id'), _first(query, 'cluster_id'),
                        _optional_int(query, 'card_id'),
                    ))
                elif parsed.path == '/api/samples':
                    start, end = _range_text(query, 180)
                    self.send_json(200, client.samples(
                        start, end, _int(query, 'page', 1, 1),
                        _int(query, 'page_size', 100, 1, 1000),
                        _first(query, 'node_id'), _first(query, 'cluster_id'),
                        _choice(query, 'status', ('complete', 'partial', 'failed')),
                        _first(query, 'q'),
                    ))
                elif parsed.path == '/api/alerts':
                    start, end = _range_text(query, 180)
                    self.send_json(200, client.alerts(
                        start, end, _int(query, 'page', 1, 1),
                        _int(query, 'page_size', 100, 1, 1000),
                        _first(query, 'node_id'), _first(query, 'cluster_id'),
                        _choice(query, 'severity', ('warning', 'critical')),
                        _choice(query, 'status', ('active', 'resolved')),
                        _first(query, 'type'),
                    ))
                elif parsed.path == '/api/export':
                    output_format = _first(query, 'format') or 'csv'
                    if output_format not in ('csv', 'xlsx'):
                        raise ValueError('format must be csv or xlsx')
                    start, end = _range_text(query, config.MAX_EXPORT_DAYS)
                    suffix = '.' + output_format
                    descriptor, path = tempfile.mkstemp(prefix='npu-export-', suffix=suffix)
                    os.close(descriptor)
                    try:
                        create_export(
                            client, path, output_format, start, end,
                            _first(query, 'node_id'), _first(query, 'cluster_id'),
                            _optional_int(query, 'card_id'),
                            _choice(query, 'status', ('complete', 'partial', 'failed')),
                        )
                        filename = 'npu-export-{}{}'.format(
                            datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'), suffix
                        )
                        content_type = (
                            'text/csv; charset=utf-8' if output_format == 'csv'
                            else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                        )
                        self.send_file(path, content_type, filename)
                    finally:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                else:
                    self.send_json(404, {'error': 'not found'})
            except ValueError as exc:
                self.send_json(400, {'error': str(exc)})
            except Exception as exc:
                LOGGER.exception('console request failed')
                self.send_json(502, {'error': 'collector unavailable: {}'.format(exc)})
    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    client = CollectorClient(config.COLLECTOR_URL, config.HTTP_TIMEOUT)
    server = ThreadingHTTPServer((config.HOST, config.PORT), make_handler(client))

    def stop_server(signum, _frame):
        LOGGER.info('received signal %s, stopping', signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    LOGGER.info('console listening on %s:%s', config.HOST, config.PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
