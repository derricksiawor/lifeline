import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Recorded:
    def __init__(self, method, path, headers, body):
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body)


class FakeAnthropic:
    def __init__(self):
        self.requests = []
        self._scripts = {}
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                with outer._lock:
                    outer.requests.append(Recorded(self.command, self.path, headers, body))
                    queue = outer._scripts.get(self.path)
                    if not queue:
                        status, extra, payload, delay, raw, claimed = 404, {}, b"", 0, None, None
                    elif len(queue) > 1:
                        status, extra, payload, delay, raw, claimed = queue.pop(0)
                    else:
                        status, extra, payload, delay, raw, claimed = queue[0]
                if delay:
                    time.sleep(delay)
                if raw is not None:
                    try:
                        self.wfile.write(raw)
                        self.wfile.flush()
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                try:
                    self.send_response(status)
                    for key, value in extra.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload) if claimed is None else claimed))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

            do_GET = _serve
            do_POST = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)

    @property
    def base(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def script(self, path, status=200, headers=None, body=None, delay=0, raw=None, claimed_length=None):
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode()
        elif isinstance(body, str):
            payload = body.encode()
        else:
            payload = body or b""
        with self._lock:
            self._scripts.setdefault(path, []).append((status, dict(headers or {}), payload, delay, raw, claimed_length))

    def requests_to(self, path):
        with self._lock:
            return [r for r in self.requests if r.path == path]
