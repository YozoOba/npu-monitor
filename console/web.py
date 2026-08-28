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
from urllib.error import HTTPError

from . import config
from . import __version__
from .client import CollectorClient
from .export import create_export, resolve_range
from .utilization_report import (
    build_utilization_report, report_filename, resolve_report_range,
)


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
button{cursor:pointer;background:#164e63}button.secondary{background:#172033}button.danger{background:#7f1d1d;border-color:#b91c1c}button:disabled{opacity:.45;cursor:not-allowed}.cards{display:grid;grid-template-columns:repeat(7,minmax(125px,1fr));gap:12px;margin:18px 0}
.tile,.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:15px}.value{font-size:25px;font-weight:700;margin-top:6px}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-top:12px}.grid>*{min-width:0}.panel h2{font-size:17px;margin:0 0 12px}.table-wrap{overflow:auto;max-height:430px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);position:sticky;top:0;background:var(--panel)}
.online,.resolved{color:#4ade80}.degraded,.stale,.warning{color:#fbbf24}.offline,.critical,.active{color:#f87171}canvas{width:100%;height:270px}
.pager{display:flex;justify-content:space-between;align-items:center;margin-top:10px}.wide{grid-column:1/-1}.error{color:#f87171}.success{color:#4ade80}.admin-grid{display:grid;grid-template-columns:repeat(3,minmax(260px,1fr));gap:12px}.admin-box{border:1px solid var(--line);border-radius:8px;padding:12px}.admin-box h3{margin:0 0 10px;font-size:14px}.admin-box label{margin-bottom:8px}.admin-box input,.admin-box select{width:100%}.admin-box input[type=checkbox]{width:auto}pre{white-space:pre-wrap;word-break:break-word;background:#08111f;border-radius:7px;padding:10px;max-height:300px;overflow:auto}
@media(max-width:1300px){.cards{grid-template-columns:repeat(4,1fr)}.grid{grid-template-columns:1fr}}
@media(max-width:900px){.admin-grid{grid-template-columns:1fr}}@media(max-width:700px){main{padding:14px}.cards{grid-template-columns:repeat(2,1fr)}}
</style></head><body><main>
<div class="head"><div><h1>NPU 集群监控</h1><div class="muted">查询、趋势、告警、数据修正与报表导出</div></div><div id="updated" class="muted">加载中…</div></div>
<div class="filters">
 <label>集群<select id="cluster"><option value="">全部集群</option></select></label>
 <label>开始时间<input id="start" type="datetime-local" step="1"></label>
 <label>结束时间<input id="end" type="datetime-local" step="1"></label>
 <label>聚合粒度<select id="bucket"><option value="300">5分钟</option><option value="900">15分钟</option><option value="3600">1小时</option><option value="86400">1天</option></select></label>
 <label>节点<input id="historyNode" placeholder="node_id"></label>
 <label>卡号<input id="card" type="number" min="0" placeholder="全部"></label>
 <button onclick="refreshAll()">查询</button><button class="secondary" onclick="exportData('csv')">导出明细 CSV</button><button class="secondary" onclick="exportData('xlsx')">导出明细 XLSX</button><button class="secondary" onclick="utilizationReport('day')">今日汇聚报表</button><button class="secondary" onclick="utilizationReport('month')">本月汇聚报表</button><button class="secondary" onclick="utilizationReport('custom')">当前时间范围汇聚报表</button>
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
 <div class="panel wide"><div class="head"><h2>节点明细</h2><div class="toolbar"><input id="nodeSearch" placeholder="搜索节点"><select id="nodeState"><option value="">全部状态</option><option>online</option><option>degraded</option><option>stale</option><option>offline</option></select><select id="nodePageSize"><option>25</option><option selected>50</option><option>100</option><option>500</option></select><button onclick="nodePage=1;loadNodes()">筛选</button></div></div><div class="table-wrap"><table><thead><tr><th>集群</th><th>节点</th><th>名称</th><th>状态</th><th>卡数</th><th>利用率</th><th>HBM</th><th>数据年龄</th><th>最近采样</th><th>管理</th></tr></thead><tbody id="nodeRows"></tbody></table></div><div class="pager"><button class="secondary" onclick="changePage('node',-1)">上一页</button><span id="nodePager" class="muted"></span><button class="secondary" onclick="changePage('node',1)">下一页</button></div></div>
 <div class="panel wide"><div class="head"><h2>告警记录</h2><div class="toolbar"><select id="alertSeverity"><option value="">全部级别</option><option>warning</option><option>critical</option></select><select id="alertStatus"><option value="">全部状态</option><option>active</option><option>resolved</option></select><input id="alertType" placeholder="告警类型"><select id="alertPageSize"><option>25</option><option selected>100</option><option>500</option><option>1000</option></select><button onclick="alertPage=1;loadAlerts()">筛选</button></div></div><div class="table-wrap"><table><thead><tr><th>开始时间</th><th>集群</th><th>节点</th><th>类型</th><th>级别</th><th>状态</th><th>信息</th><th>恢复时间</th></tr></thead><tbody id="alertRows"></tbody></table></div><div class="pager"><button class="secondary" onclick="changePage('alert',-1)">上一页</button><span id="alertPager" class="muted"></span><button class="secondary" onclick="changePage('alert',1)">下一页</button></div></div>
 <div class="panel wide"><div class="head"><h2>原始采样记录</h2><div class="toolbar"><select id="sampleStatus"><option value="">全部状态</option><option>complete</option><option>partial</option><option>failed</option></select><input id="sampleSearch" placeholder="搜索节点"><select id="samplePageSize"><option>25</option><option selected>100</option><option>500</option><option>1000</option></select><button onclick="samplePage=1;loadSamples()">筛选</button></div></div><div class="table-wrap"><table><thead><tr><th>采样时间</th><th>集群</th><th>节点</th><th>状态</th><th>卡覆盖</th><th>缺失卡</th><th>接收时间</th><th>管理</th></tr></thead><tbody id="sampleRows"></tbody></table></div><div class="pager"><button class="secondary" onclick="changePage('sample',-1)">上一页</button><span id="samplePager" class="muted"></span><button class="secondary" onclick="changePage('sample',1)">下一页</button></div></div>
 <div class="panel wide"><h2>错误节点与错误数据管理</h2><div class="muted">先修正或停止对应 Agent，再预览影响范围。执行前 Collector 会自动备份热库和涉及的归档库。</div><div class="admin-grid" style="margin-top:12px"><div class="admin-box"><h3>修改节点信息</h3><label>节点 ID<input id="adminNodeId" placeholder="从节点表选择或手动输入"></label><label>新节点名称<input id="adminNodeName" placeholder="留空表示不修改"></label><label>新集群 ID<input id="adminClusterId" placeholder="留空表示不修改"></label><button onclick="previewNodeUpdate()">预览修改</button></div><div class="admin-box"><h3>删除错误节点</h3><div class="muted">删除登记节点及其全部热数据；Agent 若继续使用该 ID 上报，节点会重新出现。</div><label><input id="adminNodeArchive" type="checkbox" checked> 同时删除归档数据</label><button class="danger" onclick="previewNodeDelete()">预览删除节点</button></div><div class="admin-box"><h3>删除错误时间段数据</h3><label>节点 ID<input id="deleteNodeId" placeholder="节点或集群至少填写一项"></label><label>集群 ID<input id="deleteClusterId" placeholder="节点或集群至少填写一项"></label><label>采样状态<select id="deleteStatus"><option value="">全部状态</option><option>complete</option><option>partial</option><option>failed</option></select></label><label><input id="deleteArchive" type="checkbox" checked> 同时删除归档数据</label><label><input id="deleteAlerts" type="checkbox" checked> 同时删除重叠告警</label><button class="danger" onclick="previewDataDelete()">按顶部时间范围预览</button></div></div><div class="admin-grid" style="margin-top:12px"><div class="admin-box" style="grid-column:span 2"><h3>操作预览</h3><pre id="adminPreview">尚未预览</pre><button id="executeAdmin" class="danger" disabled onclick="executeManagement()">确认执行</button></div><div class="admin-box"><h3>最近管理记录</h3><div id="adminHistory" class="muted">正在加载…</div></div></div></div>
</div>
<script>
const $=s=>document.querySelector(s),pct=v=>v==null?'-':Number(v).toFixed(2)+'%',esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let nodePage=1,nodePages=1,alertPage=1,alertPages=1,samplePage=1,samplePages=1,nodeCache={},pendingManagement=null;
function iso(id){const v=$(id).value;return v?new Date(v).toISOString():''}function params(extra={}){const p=new URLSearchParams();const values={start:iso('#start'),end:iso('#end'),bucket:$('#bucket').value,cluster_id:$('#cluster').value,node_id:$('#historyNode').value.trim(),card_id:$('#card').value,...extra};for(const[k,v]of Object.entries(values))if(v!==''&&v!=null)p.set(k,v);return p}
async function request(path,options={}){const r=await fetch(path,{cache:'no-store',...options});let body;try{body=await r.json()}catch(e){body={error:'响应不是有效 JSON'}}if(!r.ok)throw Error(body.error||r.status);return body}async function get(path,p){return request(path+(p?'?'+p:''))}async function post(path,value){return request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)})}
async function loadClusters(){const data=await get('/api/clusters');const selected=$('#cluster').value;$('#cluster').innerHTML='<option value="">全部集群</option>'+data.items.map(c=>`<option value="${esc(c.cluster_id)}">${esc(c.cluster_id)}</option>`).join('');$('#cluster').value=selected;$('#clusterRows').innerHTML=data.items.map(c=>`<tr><td>${esc(c.cluster_id)}</td><td>${c.total_nodes}</td><td>${c.fresh_collected_cards}/${c.registered_expected_cards}</td><td>${pct(c.utilization_avg)}</td></tr>`).join('');$('#clusterCount').textContent=data.items.length}
async function loadSnapshot(){const p=new URLSearchParams();if($('#cluster').value)p.set('cluster_id',$('#cluster').value);const s=await get('/api/snapshot',p);const registered=s.registered_expected_cards??s.expected_cards,fresh=s.fresh_collected_cards??s.active_collected_cards,fleet=s.fleet_freshness_coverage_percent??s.coverage_percent;$('#updated').textContent='更新：'+s.generated_at;$('#nodes').textContent=s.total_nodes;$('#online').textContent=(s.node_counts.online+s.node_counts.degraded)+' / '+(s.node_counts.stale+s.node_counts.offline);$('#registered').textContent=registered;$('#fresh').textContent=fresh+' / '+registered;$('#util').textContent=pct(s.utilization_avg);$('#hbm').textContent=pct(s.hbm_percent);$('#freshness').textContent=`集群新鲜度：${pct(fleet)} · 当前上报完整率：${pct(s.reporting_sample_coverage_percent)} · 最近已知卡数：${s.last_known_collected_cards} · 活跃告警：${s.active_alert_count??'-'}`}
function draw(points){const c=$('#chart'),x=c.getContext('2d'),w=c.width,h=c.height,p=38;x.clearRect(0,0,w,h);x.strokeStyle='#334155';x.fillStyle='#94a3b8';x.font='12px sans-serif';for(let v=0;v<=100;v+=25){const y=h-p-(h-2*p)*v/100;x.beginPath();x.moveTo(p,y);x.lineTo(w-p,y);x.stroke();x.fillText(v+'%',3,y+4)}const usable=points.filter(q=>q.utilization_avg!=null);if(!usable.length){x.fillText('当前范围没有数据',p+10,p+10);return}x.strokeStyle='#38bdf8';x.lineWidth=2;x.beginPath();let begun=false;points.forEach((q,i)=>{if(q.utilization_avg==null){begun=false;return}const xx=p+(w-2*p)*i/Math.max(1,points.length-1),yy=h-p-(h-2*p)*q.utilization_avg/100;if(begun)x.lineTo(xx,yy);else{x.moveTo(xx,yy);begun=true}});x.stroke();if(usable.length===1){const i=points.indexOf(usable[0]),xx=p+(w-2*p)*i/Math.max(1,points.length-1),yy=h-p-(h-2*p)*usable[0].utilization_avg/100;x.fillStyle='#38bdf8';x.beginPath();x.arc(xx,yy,4,0,Math.PI*2);x.fill()}}
async function loadHistory(){const data=await get('/api/history',params());draw(data.points||[])}
async function loadNodes(){const p=new URLSearchParams({page:nodePage,page_size:$('#nodePageSize').value});if($('#cluster').value)p.set('cluster_id',$('#cluster').value);if($('#nodeState').value)p.set('state',$('#nodeState').value);if($('#nodeSearch').value.trim())p.set('q',$('#nodeSearch').value.trim());const d=await get('/api/nodes',p);nodePages=Math.max(1,d.pages);if(nodePage>nodePages){nodePage=nodePages;return loadNodes()}nodeCache=Object.fromEntries(d.items.map(n=>[n.node_id,n]));$('#nodeRows').innerHTML=d.items.map(n=>`<tr><td>${esc(n.cluster_id)}</td><td>${esc(n.node_id)}</td><td>${esc(n.node_name)}</td><td class="${esc(n.state)}">${esc(n.state)}</td><td>${n.collected_cards}/${n.expected_cards}</td><td>${pct(n.fresh_utilization_avg)}</td><td>${pct(n.fresh_hbm_percent)}</td><td>${n.age_seconds}s</td><td>${esc(n.last_collected_at||'-')}</td><td><button class="secondary" onclick="selectNode('${n.node_id}')">选择</button></td></tr>`).join('');$('#nodePager').textContent=`第 ${d.page}/${Math.max(1,d.pages)} 页，共 ${d.total} 个节点`}
function changePage(kind,delta){let page=kind==='node'?nodePage:kind==='alert'?alertPage:samplePage,pages=kind==='node'?nodePages:kind==='alert'?alertPages:samplePages,next=Math.min(pages,Math.max(1,page+delta));if(next===page)return;if(kind==='node'){nodePage=next;loadNodes()}else if(kind==='alert'){alertPage=next;loadAlerts()}else{samplePage=next;loadSamples()}}
async function loadAlerts(){const p=params({page:alertPage,page_size:$('#alertPageSize').value,severity:$('#alertSeverity').value,status:$('#alertStatus').value,type:$('#alertType').value.trim()});p.delete('bucket');p.delete('card_id');const d=await get('/api/alerts',p);alertPages=Math.max(1,d.pages);if(alertPage>alertPages){alertPage=alertPages;return loadAlerts()}$('#alertRows').innerHTML=d.items.map(a=>`<tr><td>${esc(a.started_at)}</td><td>${esc(a.cluster_id)}</td><td>${esc(a.node_id)}</td><td>${esc(a.alert_type)}</td><td class="${esc(a.severity)}">${esc(a.severity)}</td><td class="${esc(a.status)}">${esc(a.status)}</td><td>${esc(a.message)}</td><td>${esc(a.resolved_at||'-')}</td></tr>`).join('');$('#alertPager').textContent=`第 ${d.page}/${Math.max(1,d.pages)} 页，共 ${d.total} 条告警`}
async function loadSamples(){const p=params({page:samplePage,page_size:$('#samplePageSize').value,status:$('#sampleStatus').value,q:$('#sampleSearch').value.trim()});p.delete('bucket');p.delete('card_id');const d=await get('/api/samples',p);samplePages=Math.max(1,d.pages);if(samplePage>samplePages){samplePage=samplePages;return loadSamples()}$('#sampleRows').innerHTML=d.items.map(s=>`<tr><td>${esc(s.collected_at)}</td><td>${esc(s.cluster_id)}</td><td>${esc(s.node_id)}</td><td>${esc(s.status)}</td><td>${s.collected_cards}/${s.expected_cards} (${pct(s.coverage_percent)})</td><td>${esc((s.missing_card_ids||[]).join(','))}</td><td>${esc(s.received_at)}</td><td><button class="danger" onclick="previewSingleSample('${s.sample_id}')">删除</button></td></tr>`).join('');$('#samplePager').textContent=`第 ${d.page}/${Math.max(1,d.pages)} 页，共 ${d.total} 条采样`}
function exportData(format){const p=params({format,status:$('#sampleStatus').value});p.delete('bucket');location.href='/api/export?'+p}
function utilizationReport(period){const p=new URLSearchParams({period});if($('#cluster').value)p.set('cluster_id',$('#cluster').value);if($('#historyNode').value.trim())p.set('node_id',$('#historyNode').value.trim());if(period==='custom'){p.set('start',iso('#start'));p.set('end',iso('#end'))}location.href='/api/utilization-report?'+p}
function selectNode(id){const n=nodeCache[id];$('#adminNodeId').value=id;$('#deleteNodeId').value=id;if(n){$('#adminNodeName').value=n.node_name;$('#adminClusterId').value=n.cluster_id}document.querySelector('#adminNodeId').scrollIntoView({behavior:'smooth',block:'center'})}
async function previewManagement(value){try{pendingManagement=null;$('#executeAdmin').disabled=true;$('#adminPreview').className='';$('#adminPreview').textContent='正在计算影响范围…';const p=await post('/api/admin/preview',value);pendingManagement={...value,confirmation_token:p.confirmation_token};$('#adminPreview').textContent=JSON.stringify({警告:p.warning,条件:p.criteria,影响:p.impact},null,2);$('#executeAdmin').disabled=false}catch(e){$('#adminPreview').textContent='预览失败：'+e.message;$('#adminPreview').className='error'}}
function previewNodeUpdate(){previewManagement({operation:'update_node',node_id:$('#adminNodeId').value.trim(),node_name:$('#adminNodeName').value.trim()||null,cluster_id:$('#adminClusterId').value.trim()||null})}
function previewNodeDelete(){previewManagement({operation:'delete_node',node_id:$('#adminNodeId').value.trim(),include_archive:$('#adminNodeArchive').checked})}
function previewDataDelete(){previewManagement({operation:'delete_samples',start:iso('#start'),end:iso('#end'),node_id:$('#deleteNodeId').value.trim()||null,cluster_id:$('#deleteClusterId').value.trim()||null,status:$('#deleteStatus').value||null,include_archive:$('#deleteArchive').checked,delete_alerts:$('#deleteAlerts').checked})}
function previewSingleSample(id){previewManagement({operation:'delete_samples',sample_id:id,include_archive:true,delete_alerts:false});$('#adminPreview').scrollIntoView({behavior:'smooth',block:'center'})}
async function executeManagement(){if(!pendingManagement)return;const impact=JSON.parse($('#adminPreview').textContent);if(!confirm('确认执行此操作？执行前会自动创建数据库备份。\n\n'+JSON.stringify(impact.影响)))return;try{$('#executeAdmin').disabled=true;const result=await post('/api/admin/execute',pendingManagement);pendingManagement=null;$('#adminPreview').className='success';$('#adminPreview').textContent='执行成功\n'+JSON.stringify(result,null,2);await refreshAll();await loadAdminHistory()}catch(e){$('#adminPreview').className='error';$('#adminPreview').textContent='执行失败：'+e.message}}
async function loadAdminHistory(){try{const d=await get('/api/admin/operations',new URLSearchParams({page:1,page_size:10}));$('#adminHistory').innerHTML=d.items.length?d.items.map(x=>`<div style="margin-bottom:8px"><b>${esc(x.operation)}</b><br>${esc(x.created_at)}<br><span class="muted">${esc(x.operation_id.slice(0,12))}</span></div>`).join(''):'暂无操作记录'}catch(e){$('#adminHistory').textContent='读取失败：'+e.message}}
async function refreshAll(){try{$('#updated').classList.remove('error');nodePage=Math.max(1,nodePage);await Promise.all([loadClusters(),loadSnapshot(),loadHistory(),loadNodes(),loadAlerts(),loadSamples()])}catch(e){$('#updated').textContent='查询失败：'+e.message;$('#updated').classList.add('error')}}
function setDefaults(){const end=new Date(),start=new Date(end-24*3600*1000);const local=d=>new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,19);$('#start').value=local(start);$('#end').value=local(end)}setDefaults();refreshAll();loadAdminHistory();setInterval(()=>{loadSnapshot().catch(()=>{})},10000);
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
                elif parsed.path == '/api/admin/operations':
                    self.send_json(200, client.management_operations(
                        _int(query, 'page', 1, 1),
                        _int(query, 'page_size', 50, 1, 500),
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
                elif parsed.path == '/api/utilization-report':
                    report_range = resolve_report_range(
                        _first(query, 'period') or 'month',
                        _first(query, 'start'), _first(query, 'end'),
                        max_days=config.MAX_REPORT_DAYS,
                    )
                    aggregate = client.utilization_report(
                        report_range['utc_start'], report_range['utc_end'],
                        _first(query, 'node_id'), _first(query, 'cluster_id'),
                        timeout=config.REPORT_TIMEOUT,
                    )
                    descriptor, path = tempfile.mkstemp(
                        prefix='npu-utilization-', suffix='.xlsx'
                    )
                    os.close(descriptor)
                    try:
                        build_utilization_report(path, report_range, aggregate)
                        self.send_file(
                            path,
                            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                            report_filename(report_range),
                        )
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

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ('/api/admin/preview', '/api/admin/execute'):
                self.send_json(404, {'error': 'not found'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > 1024 * 1024:
                    raise ValueError('request body size is invalid')
                request = json.loads(self.rfile.read(length).decode('utf-8'))
                result = (
                    client.management_preview(request)
                    if path.endswith('/preview')
                    else client.management_execute(request)
                )
                self.send_json(200, result)
            except HTTPError as exc:
                try:
                    body = json.loads(exc.read().decode('utf-8'))
                except Exception:
                    body = {'error': 'collector returned HTTP {}'.format(exc.code)}
                self.send_json(exc.code, body)
            except (TypeError, ValueError, UnicodeDecodeError,
                    json.JSONDecodeError) as exc:
                self.send_json(400, {'error': str(exc)})
            except Exception as exc:
                LOGGER.exception('console management request failed')
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
