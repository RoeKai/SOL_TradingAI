'use strict';
const base = document.body.dataset.base;
const byId = id => document.getElementById(id);
const number = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits: digits, minimumFractionDigits: digits});
const signed = value => value == null ? '—' : `${Number(value) > 0 ? '+' : ''}${number(value)}`;
const element = (tag, text, className = '') => { const node = document.createElement(tag); node.textContent = text; node.className = className; return node; };
const color = value => Number(value) >= 0 ? 'positive' : 'negative';
const entries = data => Array.isArray(data) ? data.map((value, i) => [value.symbol || value.name || String(i), value]) : Object.entries(data || {});
const timeText = value => { if (!value) return '—'; const date = new Date(typeof value === 'number' && value < 1e11 ? value * 1000 : value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', {hour12:false}); };
async function api(path, method = 'GET') {
  const response = await fetch(base + path, {method, credentials:'same-origin'});
  if (response.status === 401) { location.assign(base + '/login'); throw new Error('会话已过期'); }
  if (!response.ok) throw new Error(`请求失败 ${response.status}，请检查后端日志`);
  return response;
}
function row(values) { const tr = document.createElement('tr'); values.forEach(value => tr.append(element('td', value))); return tr; }
function table(id, rows, columns) { const target = byId(id); target.replaceChildren(); if (rows.length) rows.forEach(item => target.append(row(item))); else {const td = element('td','暂无记录','empty');td.colSpan=columns; const tr=document.createElement('tr');tr.append(td);target.append(tr);} }
function render(data) {
  const portfolio = data.portfolio || {};
  const risk = data.risk || {};
  const positions = entries(data.positions || portfolio.positions).map(([, value]) => value);
  const dry = data.dry_run ?? data.mode !== 'live';
  byId('mode').textContent = dry ? 'DRY RUN · 模拟' : 'LIVE · 实盘';
  byId('mode').className = `badge${dry ? '' : ' live'}`;
  byId('equity').textContent = number(portfolio.equity ?? portfolio.balance ?? data.balance);
  byId('pnl').textContent = signed(portfolio.daily_realized_pnl ?? portfolio.today_pnl ?? risk.daily_pnl ?? data.today_pnl);
  byId('position-count').textContent = String(positions.length);
  byId('risk-state').textContent = risk.halted || data.halted || risk.halt_reason || data.status === 'halted' ? '已熔断' : data.paused || risk.paused || data.status === 'paused' ? '已暂停' : '风控运行中';
  byId('risk-detail').textContent = risk.reason || risk.halt_reason || (risk.active_limits || []).join(' / ') || `今日交易 ${portfolio.today_trades ?? risk.daily_trades ?? risk.trades_today ?? '—'} / ${risk.limits?.max_trades_per_day ?? risk.max_daily_trades ?? 3}`;
  byId('feed-state').textContent = data.ready === false ? '预热 / 行情缺失 / 禁止新增开仓' : (data.data || data.data_status)?.connected === false ? '连接中断 / 禁止新增开仓' : 'WebSocket · 行情驱动';
  byId('safety').textContent = data.safety ? `账本 ${data.safety.ledger_mode} · 私有客户端 ${data.safety.private_client_constructed ? '异常' : '未构造'} · 实盘入口 ${data.safety.production_live_sealed ? '硬关闭' : '异常'} · 真实订单 ${data.safety.real_orders}（以隔离验收为证，计数非交易所对账）` : '等待安全状态';
  byId('updated').textContent = `更新 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;
  const markets = byId('markets'); markets.replaceChildren();
  for (const [symbol, market] of entries(data.markets || data.market_state || data.market)) {
    const card = element('div', '', 'market');
    const change = market.returns?.['3m'] ?? market.return_3m_pct ?? market.change_3m;
    card.append(element('span', symbol, 'symbol'), element('strong', number(market.price ?? market.last_price, 3)), element('small', `3m ${signed(change)}%`, color(change)));
    markets.append(card);
  }
  if (!markets.children.length) markets.append(element('p', '等待行情预热；此时不会开仓。', 'muted'));
  const strategies = byId('strategies'); strategies.replaceChildren();
  for (const [name, strategy] of entries(data.strategies)) {
    const item = element('div', '', 'strategy');
    const labels = {trend_breakout:'趋势突破',pullback_entry:'回踩承接',panic_rebound:'急跌反弹',fake_breakout_reverse:'假突破反向'};
    const description = element('div', labels[name] || name); description.append(element('small',strategy.reason || strategy.state || '策略独立开关由 config.yaml 管理'));
    item.append(description, element('span',strategy.enabled === false ? '关闭' : `${number(strategy.score, 1)} 分${strategy.selected ? ' · 已选中' : strategy.eligible ? ' · 候选' : ''}`,strategy.enabled === false ? 'muted':'positive'));strategies.append(item);
  }
  table('positions', positions.map(p => [p.symbol, p.side, number(p.quantity ?? p.qty, 5), number(p.entry_price, 4), number(p.stop_price ?? p.stop_loss, 4), signed(p.unrealized_pnl), `${p.strategy || '—'} / ${p.stop_status || p.status || '持仓中'}`]), 7);
  table('trades', (data.trades || []).slice(0,30).map(t => [timeText(t.closed_at || t.opened_at || t.timestamp),`${t.symbol || '—'} / ${t.strategy || '—'}`,t.status || t.reason || '—',signed(t.net_pnl ?? t.realized_pnl ?? t.pnl)]), 4);
  const logs = byId('logs'); logs.replaceChildren();
  for (const log of (data.logs || []).slice(-50).reverse()) { const item = element('div', '', 'log');item.append(element('time',timeText(log.timestamp || log.time)),element('span', `${log.event || log.level || ''} ${JSON.stringify(log)}`));logs.append(item); }
  if (!logs.children.length) logs.append(element('p','暂无日志','muted'));
}
let refreshing = false;
async function refresh() { if (refreshing) return; refreshing = true;try {render(await (await api('/api/snapshot')).json());} catch (error) {byId('message').textContent=error.message;}finally{refreshing=false;} }
for (const action of ['pause','resume']) byId(action).addEventListener('click', async () => {
  byId(action).disabled=true;try {const result=await(await api(`/api/${action}`,'POST')).json();byId('message').textContent=result.ok ? (action==='pause'?'已暂停新增开仓；持仓保护继续。':'已请求恢复，仍受全部风控约束。') : '当前不满足恢复条件，请检查风控状态。';await refresh();}catch(error){byId('message').textContent=error.message;}finally{byId(action).disabled=false;}
});
byId('logout').addEventListener('click',async()=>{await api('/logout','POST');location.assign(base+'/login');});
byId('review').addEventListener('click',async()=>{try{byId('report').textContent=await(await api('/api/report')).text();}catch(error){byId('message').textContent=error.message;}});
refresh();setInterval(refresh,5000);
