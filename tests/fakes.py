"""Tiny stand-ins for Groq and Ollama, used by the browser tests.

They run on local ports so the real server can be exercised end to end
without network access or an API key.
"""
import json
import re
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

# Notes with a sequence of steps and several figures, so every kind of diagram appears.
RICH_NOTES = {
    "title": "Quarterly launch plan",
    "summary": "The team plans the quarterly launch in three steps, keeps to a fixed budget and sets growth targets.",
    "sections": [
        {"heading": "Launch steps", "bullets": [
            "First, freeze the content and run the final checks.",
            "Then deploy the site to staging and ask the team to review it.",
            "Finally switch the domain over and watch the dashboards for an hour."]},
        {"heading": "Budget", "bullets": [
            "The ad budget is 5000 euros for the quarter.",
            "Content production costs 2,500 euros.",
            "Tooling costs 800 euros per year.",
            "Sarah prepares the spending report."]},
        {"heading": "Growth targets", "bullets": [
            "Reach 30% more signups than last quarter.",
            "Cut churn by 12% this year.",
            "Get 45% of users onto the mobile app."]},
        {"heading": "Team rhythm", "bullets": [
            "Meet every Monday morning.",
            "Keep the meeting short and focused on blockers."]},
    ],
    "key_points": ["The ad budget is 5000 euros for the quarter.", "Reach 30% more signups than last quarter."],
    "actions": ["Call the venue about the launch date", "Send the final copy to marketing", "Book the review meeting"],
}


def transcript_for(notes: dict) -> str:
    """A transcript that says everything the notes say, as a real model would have read it."""
    sentences = [notes["title"] + ".", notes["summary"]]
    for sec in notes["sections"]:
        sentences += sec["bullets"]
    sentences += notes.get("key_points", [])
    sentences += [f"We need to {a[:1].lower()}{a[1:]}." for a in notes.get("actions", [])]
    return " ".join(sentences)


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
        elif self.path.endswith("/openai/v1/models"):
            ids = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "whisper-large-v3-turbo"]
            self._send(200, {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]})
        else:
            self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path.endswith("/chat/completions"):
            if self.headers.get("Authorization", "") != "Bearer " + self.server.expected_key:
                return self._send(401, {"error": {"message": "bad key"}})
            self.server.chat_requests.append(json.loads(raw or b"{}"))
            if self.server.chat_fail:
                return self._send(500, {"error": {"message": "temporary problem"}})
            time.sleep(self.server.delay)
            notes = dict(self.server.notes)
            notes["action_items"] = notes.pop("actions", [])
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": json.dumps(notes)}}]})
        if self.path.endswith("/audio/transcriptions"):
            found = re.search(rb'name="language"\r\n\r\n([^\r]*)', raw)
            self.server.languages.append(found.group(1).decode() if found else None)
            if self.headers.get("Authorization", "") != "Bearer " + self.server.expected_key:
                return self._send(401, {"error": "bad key"})
            with self.server.lock:
                failing = self.server.fail_next > 0
                if failing:
                    self.server.fail_next -= 1
            if failing:  # a one-off failure, used to test "Try again"
                return self._send(400, {"error": "temporary problem"})
            time.sleep(self.server.delay)
            with self.server.lock:
                i = self.server.count
                self.server.count += 1
            return self._send(200, {"text": self.server.parts[i % len(self.server.parts)]})
        if self.path == "/api/chat":
            time.sleep(self.server.delay)
            return self._send(200, {"message": {"content": json.dumps(self.server.notes)}})
        self._send(404, {})


def start_fake(delay: float = 0.0, model: str = "qwen2.5:1.5b", key: str = "test-key-not-real", notes: dict | None = None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.notes = notes or NOTES
    server.parts = [transcript_for(notes)] if notes else PARTS
    server.delay = delay
    server.model = model
    server.expected_key = key
    server.count = 0
    server.fail_next = 0
    server.chat_requests = []  # bodies of the Groq chat requests
    server.chat_fail = False  # make the Groq chat endpoint answer with an error
    server.languages = []  # the language field of each transcription request, None when it was left out
    server.lock = threading.Lock()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
