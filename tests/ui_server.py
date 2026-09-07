"""Isolated manual browser-test workspace. Never touches the user's product database."""
import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Application, ThreadingHTTPServer, handler_for

if __name__ == '__main__':
    directory = Path(__file__).resolve().parents[1] / 'test-results' / 'browser'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'fixture.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
    app = Application(directory / 'data', directory / 'exports')
    server = ThreadingHTTPServer(('127.0.0.1', 8766), handler_for(app))
    print('Isolated browser test server: http://127.0.0.1:8766', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
