#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import signal
import socketserver
import sys
import threading
from urllib.parse import parse_qs, urlparse

from . import config
from . import __version__
from .client import CollectorClient


LOGGER = logging.getLogger('npu_console')


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NPU 集群状态</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#0b1220;color:#e5e7eb}
main{max-width:1400px;margin:auto;padding:24px}.head{display:flex;justify-content:space-between;align-items:end}
.muted{color:#94a3b8}.cards{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:12px;margin:20px 0}
.tile,.panel{background:#111c30;border:1px solid #24324a;border-radius:10px;padding:16px}.value{font-size:28px;font-weight:700;margin-top:6px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #24324a}th{color:#94a3b8}
.online{color:#4ade80}.degraded,.stale{color:#fbbf24}.offline{color:#f87171}canvas{width:100%;height:260px}
@media(max-width:800px){.cards{grid-template-columns:repeat(2,1fr)}.table-wrap{overflow:auto}}
</style></head><body><main>
<div class="head"><div><h1>NPU 集群状态</h1><div class="muted">Console 独立查询服务</div></div><div id="updated" class="muted">加载中…</div></div>
<div class="cards">
 <div class="tile"><div class="muted">节点</div><div id="nodes" class="value">-</div></div>
 <div class="tile"><div class="muted">在线 / 异常</div><div id="online" class="value">-</div></div>
 <div class="tile"><div class="muted">登记容量</div><div id="registered" class="value">-</div></div>
 <div class="tile"><div class="muted">新鲜样本 / 容量</div><div id="fresh" class="value">-</div></div>
 <div class="tile"><div class="muted">集群利用率</div><div id="util" class="value">-</div></div>
 <div class="tile"><div class="muted">HBM</div><div id="hbm" class="value">-</div></div>
</div>
<div id="freshness" class="panel muted">正在读取数据新鲜度…</div>
<div class="panel"><h2>最近 24 小时利用率</h2><canvas id="chart" width="1200" height="260"></canvas></div>
<div class="panel" style="margin-top:12px"><h2>节点明细</h2><div class="table-wrap"><table><thead><tr><th>节点</th><th>状态</th><th>最近卡数</th><th>新鲜利用率</th><th>新鲜 HBM</th><th>数据年龄</th></tr></thead><tbody id="rows"></tbody></table></div></div>
</main><script>
const pct=v=>v==null?'-':v.toFixed(2)+'%';
async function loadSnapshot(){const s=await fetch('/api/snapshot',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error(r.status);return r.json()});
 const registered=s.registered_expected_cards!=null?s.registered_expected_cards:s.expected_cards;
 const fresh=s.fresh_collected_cards!=null?s.fresh_collected_cards:s.active_collected_cards;
 const lastKnown=s.last_known_collected_cards!=null?s.last_known_collected_cards:fresh;
 const fleet=s.fleet_freshness_coverage_percent!=null?s.fleet_freshness_coverage_percent:s.coverage_percent;
 const reporting=s.reporting_sample_coverage_percent;
 document.querySelector('#updated').textContent='更新：'+s.generated_at;document.querySelector('#nodes').textContent=s.total_nodes;
 document.querySelector('#online').textContent=(s.node_counts.online+s.node_counts.degraded)+' / '+(s.node_counts.stale+s.node_counts.offline);
 document.querySelector('#registered').textContent=registered+' cards';document.querySelector('#fresh').textContent=fresh+' / '+registered;document.querySelector('#util').textContent=pct(s.utilization_avg);document.querySelector('#hbm').textContent=pct(s.hbm_percent);
 document.querySelector('#freshness').textContent=`Fleet freshness: ${pct(fleet)} · Reporting completeness: ${pct(reporting)} · Last known cards: ${lastKnown} · Stale capacity: ${s.stale_expected_cards||0} · Offline capacity: ${s.offline_expected_cards||0}`;
 document.querySelector('#rows').innerHTML=s.nodes.map(n=>{const freshUtil='fresh_utilization_avg'in n?n.fresh_utilization_avg:((n.state==='online'||n.state==='degraded')?n.utilization_avg:null);const freshHbm='fresh_hbm_percent'in n?n.fresh_hbm_percent:((n.state==='online'||n.state==='degraded')?n.hbm_percent:null);return `<tr><td>${n.node_id}</td><td class="${n.state}">${n.state}</td><td>${n.collected_cards}/${n.expected_cards}</td><td>${pct(freshUtil)}</td><td>${pct(freshHbm)}</td><td>${n.age_seconds}s</td></tr>`}).join('');}
function chart(points){const c=document.querySelector('#chart'),x=c.getContext('2d'),w=c.width,h=c.height,p=35;x.clearRect(0,0,w,h);x.strokeStyle='#334155';x.fillStyle='#94a3b8';x.font='12px sans-serif';
 for(let v=0;v<=100;v+=25){let y=h-p-(h-2*p)*v/100;x.beginPath();x.moveTo(p,y);x.lineTo(w-p,y);x.stroke();x.fillText(v+'%',2,y+4)}if(!points.length)return;
 x.strokeStyle='#38bdf8';x.lineWidth=2;x.beginPath();points.forEach((q,i)=>{let xx=p+(w-2*p)*i/Math.max(1,points.length-1),yy=h-p-(h-2*p)*q.utilization_avg/100;i?x.lineTo(xx,yy):x.moveTo(xx,yy)});x.stroke();}
async function loadHistory(){const h=await fetch('/api/history?hours=24&bucket=300',{cache:'no-store'}).then(r=>r.json());chart(h.points||[])}
async function refresh(){try{await Promise.all([loadSnapshot(),loadHistory()])}catch(e){document.querySelector('#updated').textContent='读取失败：'+e}}refresh();setInterval(refresh,10000);
</script></body></html>'''


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(client):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_text, *args):
            LOGGER.info('%s - %s', self.client_address[0], format_text % args)

        def send_bytes(self, status, content_type, payload):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

        def send_json(self, status, value):
            self.send_bytes(
                status, 'application/json; charset=utf-8',
                (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
            )

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path == '/':
                    self.send_bytes(200, 'text/html; charset=utf-8', INDEX_HTML.encode('utf-8'))
                elif parsed.path == '/health':
                    upstream = client.health()
                    self.send_json(200, {
                        'status': 'healthy', 'console_version': __version__,
                        'collector': upstream,
                    })
                elif parsed.path == '/api/snapshot':
                    self.send_json(200, client.snapshot())
                elif parsed.path == '/api/history':
                    query = parse_qs(parsed.query)
                    hours = max(1, min(int(query.get('hours', ['24'])[0]), 24 * 180))
                    bucket = int(query.get('bucket', ['300'])[0])
                    node_id = query.get('node_id', [None])[0]
                    from datetime import datetime, timedelta, timezone
                    end = datetime.now(timezone.utc)
                    start = end - timedelta(hours=hours)
                    self.send_json(200, client.series(
                        start.isoformat(timespec='seconds'), end.isoformat(timespec='seconds'),
                        bucket, node_id,
                    ))
                else:
                    self.send_json(404, {'error': 'not found'})
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
