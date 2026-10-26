"""Tiny stand-ins for Groq and Ollama, used by the browser tests.

They run on local ports so the real server can be exercised end to end
without network access or an API key.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PARTS = [
    "Okay, so this is a quick note about the website launch. The design team will finish the landing page "
    "by Friday, and marketing still needs the final copy before the newsletter goes out.",
    "We also talked about the budget. The ad budget stays the same as last month, and Sarah will prepare "
    "the analytics report so we can review it together next week.",
    "Finally, we agreed to meet every Monday morning. The meeting should stay short and focus on blockers "
    "only. If someone is stuck, they say so early instead of waiting for the next meeting.",
]

NOTES = {
    "title": "Website launch planning",
    "summary": "The team is preparing the website launch. The landing page is due on Friday, the ad budget "
               "is unchanged, and a short Monday meeting will track blockers.",
    "sections": [
        {"heading": "Launch tasks", "bullets": [
            "Design finishes the landing page by Friday.",
            "Marketing needs the final copy before the newsletter is sent."]},
        {"heading": "Budget and reporting", "bullets": [
            "The ad budget stays the same as last month.",
            "Sarah prepares the analytics report for next week's review."]},
        {"heading": "Team rhythm", "bullets": [
            "Meet every Monday morning.",
            "Keep the meeting short and focused on blockers.",
            "Raise problems early instead of waiting."]},
    ],
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": self.server.model}]})
        else:
            self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.endswith("/audio/transcriptions"):
            if self.headers.get("Authorization", "") != "Bearer " + self.server.expected_key:
                return self._send(401, {"error": "bad key"})
            time.sleep(self.server.delay)
            with self.server.lock:
                i = self.server.count
                self.server.count += 1
            return self._send(200, {"text": PARTS[i % len(PARTS)]})
        if self.path == "/api/chat":
            time.sleep(self.server.delay)
            return self._send(200, {"message": {"content": json.dumps(NOTES)}})
        self._send(404, {})


def start_fake(delay: float = 0.0, model: str = "qwen2.5:1.5b", key: str = "test-key-not-real"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.delay = delay
    server.model = model
    server.expected_key = key
    server.count = 0
    server.lock = threading.Lock()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
