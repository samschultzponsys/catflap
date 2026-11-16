"""AdGuard for Linux support: adguard-cli control, pause, HTTPS filtering checks."""

from __future__ import annotations

import datetime as dt
import os
import socket
import ssl
import stat
import sys
import threading
from pathlib import Path

import pytest

from catflap import adguard, conflicts
from catflap.http import TlsPinError, fetch_fingerprint

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="adguard-cli is Linux only")


# ============================================================ a fake adguard-cli
FAKE_CLI = r"""#!/bin/sh
state="$AG_DIR/state"
[ -f "$state" ] || echo off > "$state"
echo "$@" >> "$AG_DIR/calls"
case "$1" in
  status)
    if [ "$(cat "$state")" = on ]; then
      printf '%s\n' "The AdGuard proxy server is running" "HTTP proxy is listening on 127.0.0.1:3129" \
        "Manual DNS proxy is disabled" "System-wide automatic filtering is enabled" \
        "System-wide DNS filtering is disabled"
    else echo "The AdGuard proxy server is not running"; fi
    if [ -f "$AG_DIR/update" ]; then
      printf '\n%s\n%s\n' "> AdGuard CLI v$(cat "$AG_DIR/update") is now available" \
        "You can update AdGuard CLI by running \`adguard-cli update\`"
    fi ;;
  --version) echo "AdGuard CLI v$(cat "$AG_DIR/version" 2>/dev/null || echo 1.5.0)" ;;
  start)
    if [ -f "$AG_DIR/needs-sudo" ]; then echo "sudo: a terminal is required to read the password" >&2; exit 1; fi
    echo on > "$state"; echo "Started" ;;
  stop) echo off > "$state"; echo "Stopped" ;;
  restart) echo on > "$state"; echo "Restarted" ;;
  config)
    case "$2" in
      show) echo "Config location: $AG_DIR/proxy.yaml" ;;
      get) case "$3" in proxy_mode) echo "auto" ;; listen_ports.http_proxy) echo "3129" ;; esac ;;
    esac ;;
  filters) echo "1 AdGuard Base filter (enabled)" ;;
  *) echo "unknown command $1" >&2; exit 2 ;;
esac
"""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    d = tmp_path / "ag"
    d.mkdir()
    exe = d / "adguard-cli"
    exe.write_text(FAKE_CLI)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("AG_DIR", str(d))
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(adguard, "find_askpass", lambda host: None)
    return d


REAL_STATUS = """The AdGuard proxy server is running
HTTP proxy is listening on 127.0.0.1:3129
SOCKS5 proxy is listening on 127.0.0.1:1081
Manual DNS proxy is disabled
System-wide automatic filtering is enabled
System-wide DNS filtering is disabled
You can stop the proxy server by running `adguard-cli stop`

> AdGuard CLI v1.5.1 is now available
You can update AdGuard CLI by running `adguard-cli update`
"""


def test_parse_status_adguard_cli_1_5():
    # "... is disabled" lines are about single features, not the proxy itself.
    assert adguard.parse_status(0, REAL_STATUS) == adguard.ON
    text, update = adguard.split_notice(REAL_STATUS)
    assert update == "1.5.1" and "now available" not in text and "update AdGuard" not in text
    assert text.endswith("by running `adguard-cli stop`")
    assert adguard.parse_details(text) == {"http_port": 3129, "auto": True}
    assert adguard.parse_status(0, "The AdGuard proxy server is not running") == adguard.OFF
    assert adguard.parse_status(1, "Protection: stopped") == adguard.OFF
    assert adguard.parse_status(0, "hello") == adguard.UNKNOWN
    assert adguard.split_notice("plain") == ("plain", None)


def test_status_on_off_pause_resume(fake_cli):
    ag = adguard.AdGuard(adguard.Host(flatpak=False))
    assert ag.path() == str(fake_cli / "adguard-cli")
    assert ag.status().state == adguard.OFF
    settings: dict = {}
    adguard.resume(ag, settings)
    assert ag.status().state == adguard.ON
    adguard.pause(ag, settings, 300, now=1000)
    assert settings[adguard.PAUSE_KEY] == 1300
    raw = ag.status().state
    assert raw == adguard.OFF and adguard.effective_state(raw, settings) == adguard.PAUSED
    assert adguard.describe(adguard.PAUSED, settings, now=1000) == "Paused, 5 min left"
    assert not adguard.pause_due(settings, now=1299) and adguard.pause_due(settings, now=1300)
    adguard.resume(ag, settings)
    assert adguard.PAUSE_KEY not in settings and ag.status().state == adguard.ON
    adguard.pause(ag, settings, None)
    assert settings[adguard.PAUSE_KEY] == adguard.FOREVER and not adguard.pause_due(settings)
    assert adguard.describe(adguard.PAUSED, settings) == "Paused until you resume"
    adguard.turn_off(ag, settings)
    assert adguard.PAUSE_KEY not in settings
    assert adguard.effective_state(adguard.OFF, settings) == adguard.OFF


def test_started_elsewhere_ends_the_pause():
    settings = {adguard.PAUSE_KEY: 99999999999}
    assert adguard.effective_state(adguard.ON, settings) == adguard.ON
    assert adguard.PAUSE_KEY not in settings


def test_update_notice_comes_and_goes(fake_cli, monkeypatch):
    (fake_cli / "update").write_text("1.5.1")
    (fake_cli / "state").write_text("on\n")
    ag = adguard.AdGuard(adguard.Host(flatpak=False))
    st = ag.status()
    assert st.update == "1.5.1" and "available" not in st.text and st.details["http_port"] == 3129
    assert ag.proxy_port() == 3129
    (fake_cli / "update").unlink()
    assert ag.status().update == "1.5.1"            # still due: 1.5.0 is installed
    (fake_cli / "version").write_text("1.5.1")
    ag._version_checked = 0.0
    assert ag.version() == "1.5.1" and ag.status().update is None


def test_password_prompt_means_terminal(fake_cli):
    (fake_cli / "needs-sudo").write_text("")
    ag = adguard.AdGuard(adguard.Host(flatpak=False))
    with pytest.raises(adguard.NeedsTerminal) as e:
        ag.start()
    assert e.value.argv[-1] == "start"


def test_missing_and_flatpak_without_permission(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(adguard, "CLI_PATHS", ())
    assert adguard.AdGuard(adguard.Host(flatpak=False)).status().state == adguard.MISSING

    def denied(argv, **kw):
        assert argv[:2] == ["flatpak-spawn", "--host"]
        raise FileNotFoundError
    host = adguard.Host(flatpak=True, runner=denied)
    assert adguard.AdGuard(host).status().state == adguard.NO_ACCESS


def test_flatpak_runs_on_host_with_env():
    seen = []

    class P:
        returncode, stdout, stderr = 0, b"/usr/local/bin/adguard-cli\n", b""

    def runner(argv, **kw):
        seen.append(argv)
        return P()
    host = adguard.Host(flatpak=True, runner=runner)
    assert host.which("adguard-cli") == "/usr/local/bin/adguard-cli"
    host.run(["adguard-cli", "status"], env={"SUDO_ASKPASS": "/usr/bin/ksshaskpass"})
    assert seen[-1] == ["flatpak-spawn", "--host", "--env=SUDO_ASKPASS=/usr/bin/ksshaskpass",
                        "adguard-cli", "status"]


def test_terminal_command(monkeypatch):
    host = adguard.Host(flatpak=False)
    monkeypatch.setattr(host, "which", lambda n: "/usr/bin/konsole" if n == "konsole" else None)
    argv = adguard.terminal_argv(host, ["/usr/local/bin/adguard-cli", "start"])
    assert argv[:4] == ["konsole", "-e", "bash", "-c"] and "adguard-cli start" in argv[4]


# ============================================================ proxy.yaml app exclusions
YAML = """\
# AdGuard settings
listen_ports:
  http_proxy: 3129
apps:
  - include-list: browsers.yaml
  - name: '*vpn*'
    action: 'bypass'
    skip_outbound_proxy: true
filters:
  - 'flm://'
"""


def test_add_app_exclusion_goes_first_and_keeps_the_rest():
    out = adguard.add_app_exclusion(YAML, "discord")
    assert "apps:\n  - name: 'discord'\n    action: 'bypass_https'\n  - include-list" in out
    assert out.replace("  - name: 'discord'\n    action: 'bypass_https'\n", "") == YAML
    assert adguard.add_app_exclusion(out, "discord") == out            # once only
    full = adguard.add_app_exclusion(YAML, "python3*", "bypass")
    assert "- name: 'python3*'\n    action: 'bypass'" in full


def test_add_app_exclusion_other_layouts():
    assert adguard.add_app_exclusion("apps: []\nfoo: 1\n", "x") == \
        "apps:\n  - name: 'x'\n    action: 'bypass_https'\nfoo: 1\n"
    assert adguard.add_app_exclusion("foo: 1", "x") == \
        "foo: 1\napps:\n  - name: 'x'\n    action: 'bypass_https'\n"
    indented = "apps:\n    - name: 'a'\n      action: 'bypass'\n"
    assert adguard.add_app_exclusion(indented, "b").startswith(
        "apps:\n    - name: 'b'\n      action: 'bypass_https'\n    - name: 'a'")
    for bad in ("x'; rm", "a b", ""):
        with pytest.raises(ValueError):
            adguard.add_app_exclusion(YAML, bad)


def test_exclude_app_keeps_backup_and_restarts(fake_cli):
    (fake_cli / "proxy.yaml").write_text(YAML)
    (fake_cli / "state").write_text("on\n")
    ag = adguard.AdGuard(adguard.Host(flatpak=False))
    msg = adguard.exclude_app(ag, "discord")
    assert "Added 'discord'" in msg
    assert (fake_cli / "proxy.yaml.catflap-backup").read_text() == YAML
    assert "name: 'discord'" in (fake_cli / "proxy.yaml").read_text()
    assert "restart" in (fake_cli / "calls").read_text()
    assert "already" in adguard.exclude_app(ag, "discord")


# ============================================================ certificates
def make_ca(cn="AdGuard Personal CA 1234", org="AdGuard"):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, org)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert, key


def make_leaf(ca, ca_key, host="127.0.0.1"):
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    return cert, key


def der(cert):
    from cryptography.hazmat.primitives.serialization import Encoding
    return cert.public_bytes(Encoding.DER)


def test_recognises_adguard_certificates():
    ca, key = make_ca()
    leaf, _ = make_leaf(ca, key)
    other, okey = make_ca("Example Root", "Example")
    assert adguard.intercepted_by_adguard(der(leaf))
    assert not adguard.intercepted_by_adguard(der(make_leaf(other, okey)[0]))
    assert not adguard.intercepted_by_adguard(b"junk") and not adguard.intercepted_by_adguard(None)
    bundle = adguard.cert_pem(other) + adguard.cert_pem(ca)
    assert [c.subject for c in adguard.adguard_cas(adguard.parse_certs(bundle))] == [ca.subject]
    assert adguard.parse_certs(der(ca))[0].subject == ca.subject


def test_fetch_ca_follows_the_download_link():
    ca, _ = make_ca()
    pages = {adguard.CA_URL: b'<html><a href="/cert/download">Download</a></html>',
             "http://local.adguard.org/cert/download": adguard.cert_pem(ca)}

    class R:
        def __init__(self, data):
            self.data = data

        def read(self, n=-1):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(url, timeout=None):
        if url not in pages:
            raise OSError("404")
        return R(pages[url])
    assert adguard.fetch_ca(opener=opener).subject == ca.subject
    assert adguard.fetch_ca(opener=lambda u, timeout=None: R(b"<html>nothing</html>")) is None


class FakeHost(adguard.Host):
    """Real commands, but a pretend home folder and system bundle."""

    def __init__(self, home: Path, bundle: bytes, certutil=False):
        super().__init__(flatpak=False)
        self._home, self._bundle, self._certutil = home, bundle, certutil
        self.ran: list[list[str]] = []

    def home(self):
        return self._home

    def read(self, path):
        if path in adguard.SYSTEM_BUNDLES:
            return self._bundle if path == adguard.SYSTEM_BUNDLES[0] else None
        return super().read(path)

    def which(self, name):
        if name == "certutil":
            return "/usr/bin/certutil" if self._certutil else None
        return super().which(name)

    def exists(self, path):
        return False if path.startswith("/etc/pki") else super().exists(path)

    def run(self, cmd, **kw):
        if cmd[0] in ("/usr/bin/certutil", "pkexec"):
            self.ran.append(list(cmd))
            return adguard.Result(0, "")
        return super().run(cmd, **kw)


def test_https_report_finds_what_to_fix(fake_cli, tmp_path):
    (fake_cli / "state").write_text("on\n")
    ca, key = make_ca()
    leaf, _ = make_leaf(ca, key)
    home = tmp_path / "home"
    trusted = home / ".mozilla/firefox/abc.default-release"
    flat = home / ".var/app/org.mozilla.firefox/.mozilla/firefox/xyz.default"
    chrome = home / ".pki/nssdb"
    for d, data in ((trusted, b"SQLite..." + der(ca)), (flat, b"SQLite..."), (chrome, b"SQLite")):
        d.mkdir(parents=True)
        (d / "cert9.db").write_bytes(data)
    host = FakeHost(home, bundle=b"", certutil=True)
    ag = adguard.AdGuard(host)

    def probe(name, port=443):
        return adguard.Probe(name, reachable=True, intercepted=True, issuer=ca.subject.rfc4514_string(),
                             verified=False, error="unable to get local issuer certificate",
                             der=der(leaf))
    rep = adguard.https_report(ag, {"home": {"url": "https://192.168.1.1"}, "cloud": {"url": ""}},
                               probe_fn=probe, fetch_fn=lambda port: ca)
    by = {c.title: c for c in rep.checks}
    assert rep.filtering and rep.ca is not None and Path(rep.ca_file).read_bytes().startswith(b"-----BEGIN")
    assert by["System certificates (curl, dnf, Python, CatFlap)"].fix == "system"
    assert by["Firefox · profile default-release"].result == adguard.OK
    ff = by["Firefox (Flatpak) · profile default"]
    assert ff.result == adguard.BAD and ff.fix == "nss" and ff.target == str(flat)
    assert by["Chrome, Chromium, Brave, Edge, Vivaldi"].result == adguard.BAD
    assert by["Gateway home"].fix == "exclude" and by["Gateway home"].target == "python3*"
    assert by["CatFlap update checks"].result == adguard.BAD

    adguard.apply_fix(ag, ff, rep)
    assert host.ran[-1][:5] == ["/usr/bin/certutil", "-A", "-n", adguard.CA_NICK, "-t"]
    assert host.ran[-1][-2:] == ["-d", f"sql:{flat}"]
    adguard.apply_fix(ag, by["System certificates (curl, dnf, Python, CatFlap)"], rep)
    assert host.ran[-1][0] == "pkexec" and host.ran[-1][-1] == rep.ca_file
    assert "update-ca-certificates" in host.ran[-1][3]          # Debian layout (no /etc/pki)

    # Once the system trusts it, the system check passes.
    host._bundle = adguard.cert_pem(ca)
    rep = adguard.https_report(ag, {}, probe_fn=probe, fetch_fn=lambda port: ca)
    assert {c.title: c for c in rep.checks}["System certificates (curl, dnf, Python, CatFlap)"].result == adguard.OK


def test_https_report_without_adguard(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(adguard, "CLI_PATHS", ())
    rep = adguard.https_report(adguard.AdGuard(adguard.Host(flatpak=False)))
    assert [c.result for c in rep.checks] == [adguard.BAD]


# ============================================================ gateway connections
@pytest.fixture
def adguard_tls_server(tmp_path):
    """A local HTTPS server presenting an AdGuard-issued certificate."""
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    ca, ca_key = make_ca()
    leaf, key = make_leaf(ca, ca_key)
    cert_file, key_file = tmp_path / "leaf.pem", tmp_path / "leaf.key"
    cert_file.write_bytes(leaf.public_bytes(Encoding.PEM))
    key_file.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                continue
            try:
                with ctx.wrap_socket(c, server_side=True) as tc:
                    tc.recv(1024)
            except (OSError, ssl.SSLError):
                pass
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join(2)
    srv.close()


def test_tofu_refuses_to_pin_adguards_certificate(adguard_tls_server):
    with pytest.raises(TlsPinError, match="AdGuard's HTTPS filtering"):
        fetch_fingerprint(f"https://127.0.0.1:{adguard_tls_server}")


def test_pinned_gateway_explains_adguard(adguard_tls_server):
    from catflap.http import JsonClient
    c = JsonClient(f"https://127.0.0.1:{adguard_tls_server}", fingerprint="AA" * 32, timeout=5)
    with pytest.raises(TlsPinError, match="intercepting the connection to 127.0.0.1"):
        c.get("/api")
    c = JsonClient(f"https://127.0.0.1:{adguard_tls_server}", timeout=5)  # system CAs
    with pytest.raises(TlsPinError, match="AdGuard"):
        c.get("/api")


# ============================================================ other-software list
def test_adguard_windows_is_listed_as_compatible():
    progs = [{"name": "AdGuard", "version": "7.20"}, {"name": "AdGuard VPN", "version": "2.4"},
             {"name": "AdGuard Home"}]
    found = conflicts.match_programs(progs)
    assert [f.key for f in found] == ["adguard", "adguard-vpn"]
    assert found[0].conflict.compatible and not found[1].conflict.compatible
    assert [f.key for f in conflicts.to_warn(found, {})] == ["adguard-vpn"]     # no popup for AdGuard


def test_adguard_cli_listed_on_linux(fake_cli):
    class B:
        def local_network(self):
            raise RuntimeError("no NM")
    found = conflicts.detect(B(), "linux")
    assert [f.key for f in found] == ["adguard-cli"] and found[0].conflict.compatible
    assert conflicts.to_warn(found, {}) == []
