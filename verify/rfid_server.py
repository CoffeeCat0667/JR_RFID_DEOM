"""RFID EPC 接收演示服务器（单文件，仅标准库）。

用法：
    python verify/rfid_server.py [端口]      # 默认 8000

接口：
    POST /api/epc   请求体为对象数组，例如 [{"epc":"E200...","user":"0000..."}]
    GET  /api/data  当前统计数据（JSON）
    GET  /          可视化页面（每秒自动刷新）
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RFID EPC 接收监控</title>
<style>
  :root{--bg:#12151a;--card:#1b2028;--line:#2b3340;--text:#e6e9ef;--muted:#8b95a7;--accent:#4da3ff;--ok:#3ddc97;--bad:#ff6b6b}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Microsoft YaHei",system-ui,sans-serif}
  header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px}
  h1{font-size:18px;margin:0}
  #status{font-size:13px;color:var(--muted);white-space:nowrap}
  #status.ok{color:var(--ok)} #status.bad{color:var(--bad)}
  main{padding:20px 24px;max-width:1200px;margin:0 auto}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .card span{color:var(--muted);font-size:12px}
  .card b{display:block;font-size:26px;margin-top:4px;font-weight:600}
  section{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:18px;overflow:hidden}
  h2{font-size:14px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);color:var(--muted);font-weight:normal}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:9px 16px;border-bottom:1px solid var(--line);font-size:13px}
  th{color:var(--muted);font-weight:normal;background:#171b22}
  tr:last-child td{border-bottom:none}
  td.epc{font-family:Consolas,monospace;letter-spacing:.4px;word-break:break-all}
  .count{width:96px}
  .track{background:#0f1319;border-radius:3px;height:6px;margin-top:4px;overflow:hidden}
  .bar{height:6px;background:var(--accent);min-width:2px}
  .empty{padding:24px;text-align:center;color:var(--muted)}
  .badge{display:inline-block;padding:1px 8px;border-radius:10px;background:#243040;color:#9dc4ff;font-size:12px}
  @media(max-width:800px){.cards{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<header>
  <h1>RFID EPC 接收监控</h1>
  <div id="status">连接中…</div>
</header>
<main>
  <div class="cards">
    <div class="card"><span>累计读取（条）</span><b id="total">0</b></div>
    <div class="card"><span>唯一 EPC（个）</span><b id="unique">0</b></div>
    <div class="card"><span>接收批次</span><b id="batches">0</b></div>
    <div class="card"><span>最近接收</span><b id="last" style="font-size:18px">—</b></div>
  </div>
  <section>
    <h2>标签统计（按最后接收时间，最多显示 200 个）</h2>
    <table>
      <thead><tr><th>EPC</th><th class="count">次数</th><th style="width:190px">最后接收</th></tr></thead>
      <tbody id="tagRows"><tr><td colspan="3" class="empty">等待数据…</td></tr></tbody>
    </table>
  </section>
  <section>
    <h2>最近批次（最多 50 批）</h2>
    <table>
      <thead><tr><th style="width:190px">时间</th><th style="width:80px">数量</th><th>EPC</th></tr></thead>
      <tbody id="batchRows"><tr><td colspan="3" class="empty">等待数据…</td></tr></tbody>
    </table>
  </section>
</main>
<script>
  const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

  async function refresh(){
    const status = document.getElementById('status');
    try{
      const response = await fetch('/api/data', {cache: 'no-store'});
      if(!response.ok) throw new Error('HTTP ' + response.status);
      const d = await response.json();

      status.className = 'ok';
      status.textContent = '已连接 · ' + d.server_time;
      document.getElementById('total').textContent = d.total;
      document.getElementById('unique').textContent = d.unique;
      document.getElementById('batches').textContent = d.batch_total;
      document.getElementById('last').textContent = d.last_time || '—';

      const max = Math.max(1, ...d.items.map(i => i.count));
      document.getElementById('tagRows').innerHTML = d.items.length
        ? d.items.map(i => `<tr>
            <td class="epc">${esc(i.epc)}</td>
            <td class="count">${i.count}<div class="track"><div class="bar" style="width:${i.count / max * 100}%"></div></div></td>
            <td>${esc(i.last)}</td>
          </tr>`).join('')
        : '<tr><td colspan="3" class="empty">等待数据…</td></tr>';

      document.getElementById('batchRows').innerHTML = d.batches.length
        ? d.batches.map(b => `<tr>
            <td>${esc(b.time)}</td>
            <td><span class="badge">${b.epcs.length}</span></td>
            <td class="epc">${esc(b.epcs.join(', '))}</td>
          </tr>`).join('')
        : '<tr><td colspan="3" class="empty">等待数据…</td></tr>';
    }catch(e){
      status.className = 'bad';
      status.textContent = '服务器未连接';
    }
  }

  refresh();
  setInterval(refresh, 1000);
</script>
</body>
</html>
"""

_lock = threading.Lock()
_counts: dict[str, list] = {}
_total = 0
_batch_total = 0
_last_time = ""
_batches: deque = deque(maxlen=50)
_last_print = 0.0


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _record(epcs: list[str]) -> None:
    global _total, _batch_total, _last_time, _last_print
    now = _now()
    with _lock:
        for epc in epcs:
            entry = _counts.get(epc)
            if entry is None:
                _counts[epc] = [1, now]
            else:
                entry[0] += 1
                entry[1] = now
        _total += len(epcs)
        _batch_total += 1
        _last_time = now
        _batches.appendleft({"time": now, "epcs": epcs})
        should_print = time.monotonic() - _last_print >= 2
        if should_print:
            _last_print = time.monotonic()
        summary = (len(epcs), _total, len(_counts))
    if should_print:
        print(
            f"[{now}] 收到 {summary[0]} 条 | 累计 {summary[1]} 条 | 唯一 {summary[2]} 个",
            flush=True,
        )


def _snapshot() -> dict:
    with _lock:
        items = sorted(_counts.items(), key=lambda kv: kv[1][1], reverse=True)[:200]
        return {
            "total": _total,
            "unique": len(_counts),
            "batch_total": _batch_total,
            "last_time": _last_time,
            "server_time": datetime.now().strftime("%H:%M:%S"),
            "items": [{"epc": epc, "count": value[0], "last": value[1]} for epc, value in items],
            "batches": list(_batches),
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif self.path == "/api/data":
            self._json(_snapshot())
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self):  # noqa: N802
        if self.path != "/api/epc":
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._json({"ok": False, "error": f"invalid json: {exc}"}, status=400)
            return
        if not isinstance(data, list):
            self._json(
                {"ok": False, "error": "body must be a JSON array of objects"},
                status=400,
            )
            return
        epcs: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                self._json(
                    {"ok": False, "error": "each item must be an object with epc/user"},
                    status=400,
                )
                return
            epc = item.get("epc")
            user = item.get("user", "")
            if not isinstance(epc, str) or not isinstance(user, str):
                self._json(
                    {"ok": False, "error": "epc and user must be strings"},
                    status=400,
                )
                return
            if epc.strip():
                epcs.append(epc.strip().upper())
        if not epcs:
            self._json({"ok": False, "error": "empty array"}, status=400)
            return
        _record(epcs)
        self._json({"ok": True, "received": len(epcs)})

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):  # noqa: A002
        pass


def main() -> None:
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    except ValueError:
        print("端口必须是数字，例如：python verify/rfid_server.py 8000")
        return
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"页面地址: http://127.0.0.1:{port}/")
    print(f"上报地址: http://<本机IP>:{port}/api/epc")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
