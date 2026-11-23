"""One CatFlap at a time: a tiny local message channel, standard library only.

The first CatFlap listens; a later one asks it something ("show", "ping") and
gets one line back. On Linux it's a Unix socket, on Windows a named pipe. No Qt
here on purpose: the Flatpak's PySide6 has no QtNetwork, and this must never
stop CatFlap from starting.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Callable

WINDOWS = sys.platform == "win32"
Handler = Callable[[bytes], bytes]


def default_name() -> str:
    if WINDOWS:
        import getpass
        return rf"\\.\pipe\catflap-tray-{getpass.getuser()}"
    run = os.environ.get("XDG_RUNTIME_DIR", "")
    fid = os.environ.get("FLATPAK_ID", "")
    app_dir = os.path.join(run, "app", fid)
    path = ""
    if run and fid and os.path.isdir(app_dir):
        path = os.path.join(app_dir, "tray.sock")   # shared by every copy of the Flatpak
    elif run and os.path.isdir(run):
        path = os.path.join(run, "catflap-tray.sock")
    if path and len(path.encode()) < 100:           # Unix socket paths: up to ~107 bytes
        return path
    return f"\0catflap-tray-{os.getuid()}"          # abstract socket: no file at all


def ask(name: str, message: bytes, timeout: float = 1.5) -> bytes | None:
    """Send one line; return the answer line (b"" if none came), or None when nothing
    is listening."""
    if WINDOWS:
        return _ask_pipe(name, message, timeout)
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(name)
        except (FileNotFoundError, ConnectionRefusedError):
            return None
        except OSError:
            return None
        try:
            s.sendall(message + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = s.recv(256)
                if not chunk:
                    break
                data += chunk
            return data.split(b"\n")[0].strip()
        except OSError:            # timed out or reset: it's there but not answering
            return b""
    finally:
        s.close()


def _ask_pipe(name: str, message: bytes, timeout: float) -> bytes | None:
    from multiprocessing.connection import Client
    try:
        conn = Client(name, family="AF_PIPE")
    except OSError:
        return None
    try:
        conn.send_bytes(message)
        if not conn.poll(timeout):
            return b""
        return conn.recv_bytes(256).strip()
    except (OSError, EOFError):
        return b""
    finally:
        conn.close()


class Server:
    """Answers each message with ``handler(message)`` on a background thread."""

    def __init__(self, name: str, handler: Handler):
        self.name = name
        self.handler = handler
        self._sock = None
        self._listener = None

    def listen(self) -> bool:
        """Start listening. False if the name is taken (another CatFlap holds it)."""
        if WINDOWS:
            from multiprocessing.connection import Listener
            try:
                self._listener = Listener(self.name, family="AF_PIPE")
            except OSError:
                return False
            loop = self._pipe_loop
        else:
            import socket
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.bind(self.name)
            except OSError:
                s.close()
                return False
            if not self.name.startswith("\0"):
                os.chmod(self.name, 0o600)
            s.listen(8)
            self._sock = s
            loop = self._socket_loop
        threading.Thread(target=loop, name="catflap-single", daemon=True).start()
        return True

    def remove_stale(self) -> None:
        """Delete a socket file left behind by a CatFlap that crashed."""
        if not WINDOWS and not self.name.startswith("\0"):
            try:
                os.unlink(self.name)
            except FileNotFoundError:
                pass

    def _answer(self, message: bytes) -> bytes:
        try:
            return self.handler(message.strip())
        except Exception:  # noqa: BLE001 - never let a message kill the listener
            return b"error"

    def _socket_loop(self) -> None:
        while self._sock is not None:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2)
                    data = b""
                    while b"\n" not in data:
                        chunk = conn.recv(256)
                        if not chunk:
                            break
                        data += chunk
                    conn.sendall(self._answer(data.split(b"\n")[0]) + b"\n")
                except OSError:
                    pass

    def _pipe_loop(self) -> None:
        while self._listener is not None:
            try:
                conn = self._listener.accept()
            except OSError:
                return
            try:
                if conn.poll(2):
                    conn.send_bytes(self._answer(conn.recv_bytes(256)))
            except (OSError, EOFError):
                pass
            finally:
                conn.close()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            import socket
            try:
                sock.shutdown(socket.SHUT_RDWR)      # wakes the accept() in the thread
            except OSError:
                pass
            try:
                sock.close()
            finally:
                self.remove_stale()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
