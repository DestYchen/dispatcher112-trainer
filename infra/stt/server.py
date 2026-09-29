"""Offline Russian speech recognition for the boss call (Vosk, CPU).

POST /transcribe with raw 16-bit little-endian mono PCM at 16 kHz -> {"text": "..."}.
The model is mounted read-only from data/stt (scripts/prepare_stt.py downloads it once).
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)
MODEL = Model(os.environ.get("STT_MODEL", "/models/vosk-model-small-ru-0.22"))
RATE = 16000
MAX_BYTES = 60 * RATE * 2  # one minute of audio


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._json(200 if self.path == "/healthz" else 404, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/transcribe":
            return self._json(404, {"error": "not found"})
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= MAX_BYTES:
            return self._json(413, {"error": "audio must be 0-60 s"})
        recognizer = KaldiRecognizer(MODEL, RATE)
        recognizer.AcceptWaveform(self.rfile.read(size))
        self._json(200, {"text": json.loads(recognizer.FinalResult()).get("text", "")})

    def log_message(self, *args: object) -> None:
        pass


ThreadingHTTPServer(("0.0.0.0", 2700), Handler).serve_forever()
