#!/usr/bin/env python3
"""
Pre-compile static/app.jsx → static/app.js using Babel (no Node required).

We drive a headless Chrome via the DevTools protocol, load @babel/standalone
once, then ask it to transform the JSX source. Output is written compact
(whitespace stripped, no comments) so the browser ships plain JS — no
in-browser Babel, no compile-on-load, no layout flash.

Usage:  python3 build.py
Run this after every edit to static/app.jsx, then commit both files.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC  = os.path.join(HERE, "static", "app.jsx")
OUT  = os.path.join(HERE, "static", "app.js")

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT   = 9333
PROFILE = "/tmp/loantrack-build-profile"

BUILD_HTML = """<!DOCTYPE html><html><head>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
</head><body>build</body></html>"""


def _free(port):
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port)); s.close(); return False
    except OSError:
        return True


def main():
    if not os.path.exists(SRC):
        sys.exit(f"❌ source not found: {SRC}")
    src = open(SRC, encoding="utf-8").read()
    print(f"• source: {len(src):,} bytes")

    # Serve the build page over a tiny local HTTP server so Babel loads.
    import http.server, threading
    build_dir = os.path.join(HERE, ".build")
    os.makedirs(build_dir, exist_ok=True)
    open(os.path.join(build_dir, "index.html"), "w").write(BUILD_HTML)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), lambda *a, **k:
        http.server.SimpleHTTPRequestHandler(*a, directory=build_dir, **k))
    http_port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # Launch headless Chrome pointed at the build page.
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
        f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={PROFILE}",
        f"http://127.0.0.1:{http_port}/index.html",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        # Wait for the debugger endpoint
        ws_url = None
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                for t in tabs:
                    if t.get("type") == "page":
                        ws_url = t["webSocketDebuggerUrl"]; break
                if ws_url: break
            except Exception:
                pass
            time.sleep(0.2)
        if not ws_url:
            sys.exit("❌ could not reach Chrome devtools")

        import websocket  # pip install websocket-client
        ws = websocket.create_connection(ws_url, max_size=64 * 1024 * 1024)
        mid = 0

        def call(method, params, timeout=60):
            nonlocal mid
            mid += 1
            ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            ws.settimeout(timeout)
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    return msg

        call("Runtime.enable", {})

        # Wait until Babel global is ready
        for _ in range(50):
            r = call("Runtime.evaluate", {"expression": "typeof Babel"})
            if r["result"]["result"].get("value") == "object":
                break
            time.sleep(0.2)
        else:
            sys.exit("❌ Babel did not load")

        # Inject the source as a global, then transform it.
        call("Runtime.evaluate", {
            "expression": "window.__src = " + json.dumps(src) + "; 'ok'",
        })
        print("• compiling via Babel…")
        r = call("Runtime.evaluate", {
            "expression": (
                "Babel.transform(window.__src, {"
                "  presets: [['react', {}]],"
                "  compact: true, comments: false, retainLines: false"
                "}).code"
            ),
            "returnByValue": True,
        }, timeout=120)

        if "exceptionDetails" in r.get("result", {}):
            sys.exit("❌ Babel error: " + json.dumps(r["result"]["exceptionDetails"])[:500])
        code = r["result"]["result"]["value"]
        if not code:
            sys.exit("❌ empty compile output")

        open(OUT, "w", encoding="utf-8").write(code)
        print(f"✓ wrote {OUT}: {len(code):,} bytes")
        ws.close()
    finally:
        proc.terminate()
        httpd.shutdown()


if __name__ == "__main__":
    main()
