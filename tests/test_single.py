"""The one-CatFlap-at-a-time channel (standard library only)."""

import subprocess
import sys
import time

import pytest

from catflap import single


def _name(tmp_path):
    """A channel name: a socket file here, a named pipe on Windows."""
    if single.WINDOWS:
        import uuid
        return rf"\\.\pipe\catflap-test-{uuid.uuid4().hex}"
    return str(tmp_path / "s.sock")


def test_no_qtnetwork_anywhere():
    """The Flatpak's PySide6 has no QtNetwork: importing it made 2.2-2.6 crash at start."""
    import pathlib
    import catflap
    for f in pathlib.Path(catflap.__file__).parent.rglob("*.py"):
        code = "\n".join(line for line in f.read_text().splitlines() if not line.lstrip().startswith("#"))
        assert "import QtNetwork" not in code and "QtNetwork." not in code, f


def test_tray_starts_without_qtnetwork(tmp_path):
    """Run the start-up check with QtNetwork made unimportable, like in the Flatpak."""
    code = (
        "import sys, os; os.environ['QT_QPA_PLATFORM']='offscreen'\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'PySide6.QtNetwork': raise ImportError('no QtNetwork here')\n"
        "sys.meta_path.insert(0, Block())\n"
        "from catflap import tray\n"
        f"print(tray.wait_for_other_copy({_name(tmp_path)!r}, b'show', 0))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "None"


def test_ask_and_answer(tmp_path):
    name = _name(tmp_path)
    assert single.ask(name, b"ping") is None                        # nothing listening
    got = []
    srv = single.Server(name, lambda m: got.append(m) or b"ok 9.9")
    assert srv.listen()
    assert single.ask(name, b"show") == b"ok 9.9" and got == [b"show"]
    assert not single.Server(name, lambda m: b"x").listen()          # name taken
    srv.close()
    # A closed Windows pipe can linger until its waiting thread ends, but never answers.
    assert single.ask(name, b"ping", timeout=0.3) in ((None, b"") if single.WINDOWS else (None,))


@pytest.mark.skipif(single.WINDOWS, reason="named pipes leave nothing behind")
def test_stale_socket_file_is_replaced(tmp_path):
    import socket
    name = str(tmp_path / "s.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(name)
    s.close()                                                        # a crashed copy's leftover
    assert single.ask(name, b"ping") is None
    srv = single.Server(name, lambda m: b"ok")
    assert not srv.listen()
    srv.remove_stale()
    assert srv.listen() and single.ask(name, b"ping") == b"ok"
    srv.close()


def test_silent_copy_reads_as_empty_answer(tmp_path):
    name = _name(tmp_path)
    srv = single.Server(name, lambda m: time.sleep(1) or b"late")
    assert srv.listen()
    assert single.ask(name, b"ping", timeout=0.2) == b""
    srv.close()
