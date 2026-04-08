import http.server
import socketserver
import webbrowser
import os

PORT = 8000
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Serving UI Dashboard at http://localhost:{PORT}")
    try:
        webbrowser.open(f'http://localhost:{PORT}/index.html')
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
