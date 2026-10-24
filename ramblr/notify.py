"""Tiny IPC: writers (capture, diarize) send one unix datagram after each commit; the web process
listens and pushes to SSE clients. No DB polling anywhere. Fire and forget: web down never blocks a writer."""
import json
import socket

from . import config

_sock = None


def send(obj):
    global _sock
    try:
        if _sock is None:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            _sock.setblocking(False)
        _sock.sendto(json.dumps(obj, separators=(",", ":")).encode(), str(config.NOTIFY_SOCK))
    except OSError:
        pass
