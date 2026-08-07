"""Live monitoring dashboard — stdlib-only HTTP server.

Runs in a background thread next to the live scheduler so the paper account
can be watched from a browser (Railway public domain -> service $PORT).

Endpoints:
  /              HTML dashboard (auto-refreshing)
  /api/state     JSON engine state
  /api/trades    JSON trade ledger
  /api/summary   JSON {summary, state, live-mark}
  /api/health    {"status": "ok"}
"""
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from paper_engine import PTS_PER_USD

PORT = int(os.getenv("PORT", "8000"))


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_lines(path):
    if not path.exists():
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except Exception:
        pass
    return out


class Monitor:
    """Reads persisted paper data and (optionally) pokes the engine for a live mark."""

    def __init__(self, results_dir: str, engine=None):
        self.dir = Path(results_dir)
        self.state_file = self.dir / "paper_state.json"
        self.trades_file = self.dir / "paper_trades.jsonl"
        self.engine = engine

    def state(self) -> dict:
        return _load_json(self.state_file)

    def trades(self) -> list:
        return _load_lines(self.trades_file)

    def live_mark(self) -> dict:
        """Current option mark + unrealized P&L for an open position (if any)."""
        eng = self.engine
        if eng is None or eng.position is None:
            return None
        try:
            p = eng.position
            now = datetime.now(timezone.utc)
            mark = eng.feed.option_price_at(eng.cfg.opt_type, p["strike"], p["expiry"], now)
            fut = eng.feed.futures_price_at(now)
            return {
                "mark": round(mark, 2) if mark is not None else None,
                "futures": round(fut, 2) if fut is not None else None,
                "entry_fill": round(p["entry_fill"], 2),
                "stop_px": round(eng.cfg.stop_mult * p["entry_px"], 2),
                "limit_px": eng.cfg.limit_price,
                "lots": p["lots"],
                "unreal_usd": round((p["entry_fill"] - mark) * p["lots"] * 0.001, 2) if mark is not None else None,
                "close_ts": p["close_ts"].isoformat(),
            }
        except Exception:
            return {"mark": None, "error": "feed_unavailable"}

    def summary(self, state, trades) -> dict:
        # closed trades only: OPEN (entry) and SKIP records carry no net_usd
        trades = [t for t in trades if not t.get("skip")
                  and isinstance(t.get("net_usd"), (int, float))]
        n = len(trades)
        start = 1000.0
        balance = state.get("balance", start)
        if not n:
            return {"n_trades": 0, "balance": balance, "start_balance": start,
                    "net_usd": 0.0, "total_return_pct": 0.0, "win_rate_pct": 0.0,
                    "profit_factor": None, "n_stops": 0, "n_limits": 0}
        net = sum(t.get("net_usd", 0) for t in trades)
        wins = [t for t in trades if t.get("net_usd", 0) > 0]
        gp = sum(t["net_usd"] for t in wins)
        gl = -sum(t["net_usd"] for t in trades if t["net_usd"] < 0)
        return {
            "n_trades": n,
            "balance": balance,
            "start_balance": start,
            "net_usd": round(net, 2),
            "total_return_pct": round(100 * net / start, 2),
            "win_rate_pct": round(100 * len(wins) / n, 2),
            "profit_factor": round(gp / gl, 2) if gl > 0 else None,
            "n_stops": sum(1 for t in trades if t.get("exit_type") == "stop"),
            "n_limits": sum(1 for t in trades if t.get("exit_type") == "limit"),
        }


class Handler(BaseHTTPRequestHandler):
    monitor: Monitor = None

    def log_message(self, fmt, *args):  # keep logs clean
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self._route()
        except Exception as e:
            try:
                self._send(500, json.dumps({"error": str(e)}))
            except Exception:
                pass

    def _route(self):
        m = self.monitor
        path = self.path.split("?")[0]
        if path == "/api/state":
            return self._send(200, json.dumps(m.state(), indent=2))
        if path == "/api/trades":
            return self._send(200, json.dumps(m.trades(), indent=2))
        if path == "/api/summary":
            st, tr = m.state(), m.trades()
            payload = {"summary": m.summary(st, tr), "state": st, "live": m.live_mark()}
            return self._send(200, json.dumps(payload, indent=2))
        if path == "/api/health":
            return self._send(200, json.dumps({"status": "ok"}))
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        return self._send(404, json.dumps({"error": "not found"}))


def start_monitor(results_dir: str, engine=None, port: int = None) -> ThreadingHTTPServer:
    """Start the dashboard server in a daemon thread. Returns the httpd handle."""
    Handler.monitor = Monitor(results_dir, engine)
    httpd = ThreadingHTTPServer(("0.0.0.0", port or PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>r3 Paper Bot — Live</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { --bg:#0d1117; --card:#161b22; --line:#30363d; --txt:#e6edf3; --dim:#8b949e;
          --green:#3fb950; --red:#f85149; --amber:#d29922; --blue:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:16px 24px; border-bottom:1px solid var(--line); display:flex;
           align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; }
  header h1 { margin:0; font-size:18px; font-weight:600; }
  header .sub { color:var(--dim); font-size:12px; }
  .wrap { padding:20px 24px; max-width:1100px; margin:0 auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .card .label { color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
  .card .value { font-size:22px; font-weight:700; margin-top:4px; }
  .card .sub { color:var(--dim); font-size:12px; margin-top:2px; }
  .pos { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:20px; }
  .pos h2 { margin:0 0 10px; font-size:14px; color:var(--dim); text-transform:uppercase; letter-spacing:.04em; }
  .pos .row { display:flex; flex-wrap:wrap; gap:22px; }
  .pos .kv b { display:block; color:var(--dim); font-size:11px; font-weight:500; }
  .pos .kv span { font-size:16px; font-weight:600; }
  .panel { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:20px; }
  .panel h2 { margin:0 0 10px; font-size:14px; color:var(--dim); text-transform:uppercase; letter-spacing:.04em; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--dim); font-weight:500; }
  .up { color:var(--green); } .dn { color:var(--red); }
  .badge { display:inline-block; padding:2px 8px; border-radius:20px; font-size:11px; font-weight:600; }
  .b-open { background:#1f6feb33; color:var(--blue); border:1px solid #1f6feb55; }
  .b-stop { background:#f8514933; color:var(--red); border:1px solid #f8514955; }
  .b-limit { background:#3fb95033; color:var(--green); border:1px solid #3fb95055; }
  .b-flat { background:#30363d; color:var(--dim); }
  .pill { display:inline-block; padding:2px 8px; border-radius:20px; font-size:11px; font-weight:600;
          background:#30363d; color:var(--dim); margin-left:6px; }
  .g { color:var(--green);} .r { color:var(--red);} .a { color:var(--amber);}
  #lastUpd { color:var(--dim); font-size:12px; text-align:center; margin-top:16px; }
  .muted { color:var(--dim); }
</style>
</head>
<body>
<header>
  <div>
    <h1>r3 Paper Bot <span class="pill">0DTE short put</span></h1>
    <div class="sub">Delta Exchange India · live paper trading · refresh 10s</div>
  </div>
  <div id="header-right" class="sub"></div>
</header>
<div class="wrap">
  <div class="cards">
    <div class="card"><div class="label">Balance</div><div class="value" id="bal">—</div><div class="sub" id="balSub"></div></div>
    <div class="card"><div class="label">Total Return</div><div class="value" id="ret">—</div><div class="sub" id="retSub"></div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value" id="win">—</div><div class="sub" id="winSub"></div></div>
    <div class="card"><div class="label">Profit Factor</div><div class="value" id="pf">—</div><div class="sub" id="pfSub"></div></div>
    <div class="card"><div class="label">Trades</div><div class="value" id="tr">—</div><div class="sub" id="trSub"></div></div>
  </div>

  <div class="pos">
    <h2>Open Position</h2>
    <div id="posBody" class="muted">No position open.</div>
  </div>

  <div class="panel"><h2>Equity Curve</h2><canvas id="eq" height="70"></canvas></div>

  <div class="panel"><h2>Recent Trades</h2><div style="overflow-x:auto"><table id="tbl">
    <thead><tr><th>Date</th><th>Symbol</th><th>Entry</th><th>Lots</th><th>Exit</th><th>Net</th><th>Balance</th></tr></thead>
    <tbody></tbody>
  </table></div></div>

  <div id="lastUpd"></div>
</div>

<script>
const FMT = new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'});
let chart = null;
function badge(t){ return '<span class="badge b-'+t+'">'+t+'</span>'; }
function cls(v){ return v>0 ? 'up' : (v<0 ? 'dn' : 'muted'); }
function fmtPts(p){ return (p===null||p===undefined)?'—':(p.toFixed ? p.toFixed(2) : p); }

async function load(){
  let data;
  try { data = await (await fetch('/api/summary')).json(); } catch(e){ return; }
  const s = data.summary, st = data.state||{}, lv = data.live;
  const pos = st.position;
  document.getElementById('bal').textContent = FMT.format(s.balance);
  document.getElementById('balSub').innerHTML = 'start '+FMT.format(s.start_balance);
  document.getElementById('ret').textContent = ((s.total_return_pct??0)>0?'+':'')+(s.total_return_pct??0)+'%';
  document.getElementById('ret').className = (s.total_return_pct??0)>=0 ? 'value g' : 'value r';
  document.getElementById('retSub').innerHTML = FMT.format(s.net_usd??0)+' net';
  document.getElementById('win').textContent = (s.win_rate_pct??0)+'%';
  document.getElementById('winSub').textContent = (s.n_stops??0)+' stops · '+(s.n_limits??0)+' limits';
  document.getElementById('pf').textContent = s.profit_factor ?? '∞';
  document.getElementById('tr').textContent = s.n_trades;
  document.getElementById('trSub').textContent = st.skip_next ? 'skip-next armed' : '';

  // position card
  const pb = document.getElementById('posBody');
  if (!pos){
    pb.className='muted'; pb.textContent = 'No position open — next entry 17:00 IST.';
  } else {
    pb.className=''; pb.innerHTML = '<div class="row">'
      +'<div class="kv"><b>Symbol</b><span>'+pos.symbol+'</span></div>'
      +'<div class="kv"><b>Strike</b><span>'+pos.strike+'</span></div>'
      +'<div class="kv"><b>Expiry</b><span>'+pos.expiry+'</span></div>'
      +'<div class="kv"><b>Lots</b><span>'+pos.lots+'</span></div>'
      +'<div class="kv"><b>Entry fill</b><span>'+fmtPts(pos.entry_fill)+'</span></div>'
      +(lv && lv.mark!=null
        ? '<div class="kv"><b>Live mark</b><span>'+fmtPts(lv.mark)+'</span></div>'
          +'<div class="kv"><b>Unrealized</b><span class="'+(lv.unreal_usd>=0?'g':'r')+'">'+FMT.format(lv.unreal_usd)+'</span></div>'
          +'<div class="kv"><b>Stop / Limit</b><span>'+fmtPts(lv.stop_px)+' / '+fmtPts(lv.limit_px)+'</span></div>'
          +'<div class="kv"><b>Futures</b><span>'+fmtPts(lv.futures)+'</span></div>'
        : '<div class="kv"><b>Stop / Limit</b><span>—</span></div>')
      +'<div class="kv"><b>Closes</b><span class="a">'+ (pos.close_ts||'').replace('T',' ').slice(0,16) +' IST</span></div>'
      +'</div>';
  }

  // trades table + equity (closed trades only)
  const trades = await (await fetch('/api/trades')).json();
  const closed = trades.filter(t=>!t.skip && t.net_usd !== undefined);
  const tbody = document.querySelector('#tbl tbody');
  tbody.innerHTML = closed.slice(-20).reverse().map(t=>
    '<tr><td>'+t.date+'</td><td>P-'+t.strike+'</td><td>'+t.entry_fill+'</td><td>'+t.lots+'</td>'
    +'<td>'+badge(t.exit_type||'open')+'</td>'
    +'<td class="'+cls(t.net_usd)+'">'+(t.net_usd>=0?'+':'')+FMT.format(t.net_usd)+'</td>'
    +'<td>'+FMT.format(t.balance)+'</td></tr>').join('');
  const bal = closed.map(t=>({x:t.date, y:t.balance}));
  if (bal.length){
    if (chart){ chart.data.labels=bal.map(b=>b.x); chart.data.datasets[0].data=bal.map(b=>b.y); chart.update(); }
    else {
      chart = new Chart(document.getElementById('eq'), {
        type:'line',
        data:{ labels:bal.map(b=>b.x), datasets:[{ data:bal.map(b=>b.y),
          borderColor:'#58a6ff', backgroundColor:'#58a6ff22', fill:true, tension:.25,
          pointRadius:0, borderWidth:2 }]},
        options:{ responsive:true, plugins:{legend:{display:false}},
          scales:{ y:{ grid:{color:'#21262d'}, ticks:{color:'#8b949e'} },
                   x:{ grid:{display:false}, ticks:{color:'#8b949e', maxTicksLimit:8} } } }
      });
    }
  }
  document.getElementById('lastUpd').textContent = 'Updated ' + new Date().toLocaleTimeString();
}
load(); setInterval(load, 10000);
</script>
</body>
</html>
"""
