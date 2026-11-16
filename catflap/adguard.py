"""AdGuard for Linux (``adguard-cli``): status, on/off/pause, and HTTPS-filtering checks.

CatFlap doesn't block ads itself. It drives the ``adguard-cli`` you installed
(and licensed) from AdGuard, the way the Windows app's tray icon drives its
service. Only documented commands are used: ``start``, ``stop``, ``restart``,
``status``, ``config show/get``, ``filters list``, ``check-update``,
``install-browser-integration`` and ``configure``.

HTTPS filtering works by re-signing every HTTPS connection with AdGuard's own
certificate authority (CA). Anything that doesn't trust that CA breaks. On Linux
there are several separate places that decide trust:

* the system store (``/etc/pki/ca-trust`` on Fedora, ``/usr/local/share/ca-certificates``
  on Debian): curl, dnf, Python, and on Fedora also Firefox and Chrome (their NSS
  is wired to the system store through p11-kit);
* each Firefox-family profile's own ``cert9.db`` (always for Firefox from Flathub or
  mozilla.org, which bring their own NSS);
* Chrome-family browsers' ``~/.pki/nssdb`` (and the same path inside each Flatpak
  browser's ``~/.var/app/<id>``).

:func:`https_report` checks each of them and says what to fix; the ``fix_*``
functions do the fixes. Apps that pin their certificates can only be excluded
from HTTPS filtering, which :func:`add_app_exclusion` does in AdGuard's
``proxy.yaml`` (``apps`` section, ``action: bypass_https``).

Inside CatFlap's Flatpak, ``adguard-cli`` and the browser profiles are on the
host, so everything runs through ``flatpak-spawn --host``. That needs the
``org.freedesktop.Flatpak`` permission, which CatFlap's Flatpak does not have
by default (see :data:`FLATPAK_OVERRIDE`).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

CLI = "adguard-cli"
CLI_PATHS = ("/usr/local/bin/adguard-cli", "/opt/adguard-cli/adguard-cli",
             "/opt/adguard/adguard-cli", "~/.local/bin/adguard-cli", "~/adguard-cli/adguard-cli")
FLATPAK_APP_ID = "io.github.samschultzponsys.CatFlap"
FLATPAK_OVERRIDE = f"flatpak override --user --talk-name=org.freedesktop.Flatpak {FLATPAK_APP_ID}"
CA_URL = "http://local.adguard.org/cert"
CA_NICK = "AdGuard Personal CA"
ASKPASS = ("/usr/bin/ksshaskpass", "/usr/libexec/openssh/ksshaskpass",
           "/usr/libexec/openssh/gnome-ssh-askpass", "/usr/libexec/seahorse/ssh-askpass",
           "/usr/bin/ssh-askpass", "/usr/lib/ssh/ssh-askpass", "/usr/bin/lxqt-openssh-askpass")
# Terminals, in order of preference, with the flag that runs a command.
TERMINALS = (("konsole", ["-e"]), ("ptyxis", ["--"]), ("gnome-terminal", ["--"]),
             ("kgx", ["-e"]), ("xfce4-terminal", ["-x"]), ("tilix", ["-e"]),
             ("alacritty", ["-e"]), ("kitty", []), ("foot", []), ("xterm", ["-e"]))

# States shown on the tray icon
ON, PAUSED, OFF, UNKNOWN, MISSING, NO_ACCESS = "on", "paused", "off", "unknown", "missing", "no-access"
LABELS = {ON: "Protecting", PAUSED: "Paused", OFF: "Turned off", UNKNOWN: "Status unknown",
          MISSING: "Not installed", NO_ACCESS: "Needs permission (Flatpak)"}

# Settings keys
SHOW_KEY = "adguard_show"                  # show the shield on the tray icon (default on)
PAUSE_KEY = "adguard_paused_until"         # epoch seconds; -1 = until resumed; absent = not paused
FOREVER = -1


class AdGuardError(RuntimeError):
    pass


class NeedsTerminal(AdGuardError):
    """The command wants a password typed into a terminal (sudo without askpass)."""

    def __init__(self, argv: list[str], output: str):
        super().__init__("AdGuard needs your password for this. Run it in a terminal.")
        self.argv = argv
        self.output = output


# ============================================================ running things on the host
@dataclass
class Result:
    rc: int
    out: str
    err: str = ""

    @property
    def text(self) -> str:
        return (self.out + ("\n" if self.out and self.err else "") + self.err).strip()


def in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


class Host:
    """Runs commands and reads files on the real system (through ``flatpak-spawn
    --host`` inside the Flatpak)."""

    def __init__(self, flatpak: bool | None = None, runner: Callable[..., Any] = subprocess.run):
        self.flatpak = in_flatpak() if flatpak is None else flatpak
        self._run = runner
        self._access: bool | None = None

    def argv(self, cmd: Sequence[str], env: dict[str, str] | None = None) -> list[str]:
        if not self.flatpak:
            return list(cmd)
        return ["flatpak-spawn", "--host", *(f"--env={k}={v}" for k, v in (env or {}).items()), *cmd]

    def run(self, cmd: Sequence[str], timeout: float = 30, env: dict[str, str] | None = None,
            input_bytes: bytes | None = None) -> Result:
        full_env = None if self.flatpak or not env else {**os.environ, **env}
        try:
            p = self._run(self.argv(cmd, env), capture_output=True, timeout=timeout,
                          input=input_bytes if input_bytes is not None else b"",
                          env=full_env)
        except FileNotFoundError:
            return Result(127, "", f"{cmd[0]}: command not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"{cmd[0]}: timed out")
        dec = (lambda b: b.decode(errors="replace") if isinstance(b, bytes) else (b or ""))
        return Result(p.returncode, dec(p.stdout), dec(p.stderr))

    def run_bytes(self, cmd: Sequence[str], timeout: float = 30) -> bytes | None:
        try:
            p = self._run(self.argv(cmd), capture_output=True, timeout=timeout, input=b"")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        return p.stdout if p.returncode == 0 else None

    def has_access(self) -> bool:
        """Outside the Flatpak always; inside it only with the host-command permission."""
        if not self.flatpak:
            return True
        if self._access is None:
            self._access = self.run(["true"], timeout=10).rc == 0
        return self._access

    # -- files
    def home(self) -> Path:
        return Path.home()

    def exists(self, path: str) -> bool:
        if not self.flatpak:
            return os.path.exists(path)
        return self.run(["test", "-e", path], timeout=10).rc == 0

    def glob(self, patterns: Sequence[str]) -> list[str]:
        """Absolute paths matching shell globs (``~`` = home)."""
        pats = [str(self.home()) + p[1:] if p.startswith("~") else p for p in patterns]
        if not self.flatpak:
            import glob as g
            out: list[str] = []
            for p in pats:
                out += sorted(g.glob(p))
            return out
        script = 'for p in "$@"; do for f in $p; do [ -e "$f" ] && printf "%s\\n" "$f"; done; done'
        r = self.run(["sh", "-c", script, "sh", *pats], timeout=15)
        return [line for line in r.out.splitlines() if line]

    def read(self, path: str) -> bytes | None:
        if not self.flatpak:
            try:
                return Path(path).read_bytes()
            except OSError:
                return None
        return self.run_bytes(["cat", path])

    def write(self, path: str, data: bytes) -> None:
        if not self.flatpak:
            Path(path).write_bytes(data)
            return
        r = self.run(["sh", "-c", 'cat > "$1"', "sh", path], input_bytes=data)
        if r.rc != 0:
            raise AdGuardError(f"couldn't write {path}: {r.text}")

    def which(self, name: str) -> str | None:
        if not self.flatpak:
            return shutil.which(name)
        r = self.run(["sh", "-c", 'command -v "$1"', "sh", name], timeout=10)
        return r.out.strip() or None if r.rc == 0 else None


def scratch_dir() -> Path:
    """A folder that has the same path inside the Flatpak and on the host."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    d = Path(base) / "catflap"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================ adguard-cli
_UPDATE_RE = re.compile(r"^\s*>?\s*AdGuard CLI v?(\d[\w.\-]*) is now available.*$", re.I | re.M)
_UPDATE_HINT_RE = re.compile(r"^.*You can update AdGuard CLI by running.*$\n?", re.I | re.M)


def split_notice(text: str) -> tuple[str, str | None]:
    """(output without adguard-cli's "update available" notice, the new version or None).

    adguard-cli 1.5 appends to every command's output:
    ``> AdGuard CLI v1.5.1 is now available`` / ``You can update AdGuard CLI by running ...``"""
    m = _UPDATE_RE.search(text)
    clean = _UPDATE_HINT_RE.sub("", _UPDATE_RE.sub("", text))
    return clean.strip(), (m.group(1) if m else None)


def parse_status(rc: int, text: str) -> str:
    """ON / OFF / UNKNOWN from ``adguard-cli status``.

    adguard-cli 1.5 says "The AdGuard proxy server is running" followed by lines
    about each feature ("Manual DNS proxy is disabled" ...), so the proxy line
    decides; the other lines must not be mistaken for the overall state."""
    t = split_notice(text)[0].lower()
    m = re.search(r"proxy server (?:is|was)\s+(not\s+running|running|stopped|not started)", t)
    if m:
        return ON if m.group(1) == "running" else OFF
    first = next((line for line in t.splitlines() if line.strip()), "")
    if re.search(r"\b(not running|isn't running|stopped|inactive|not active|not started|"
                 r"is off|turned off)\b", first):
        return OFF
    if re.search(r"\b(running|active|started|is on)\b", first):
        return ON
    return UNKNOWN


def parse_details(text: str) -> dict[str, Any]:
    """Extra facts from ``adguard-cli status`` (1.5): proxy port, automatic mode."""
    t = text.lower()
    out: dict[str, Any] = {}
    m = re.search(r"http proxy is listening on [\w.:\[\]]*?:(\d{2,5})", t)
    if m:
        out["http_port"] = int(m.group(1))
    m = re.search(r"system-wide automatic filtering is (enabled|disabled)", t)
    if m:
        out["auto"] = m.group(1) == "enabled"
    return out


def _needs_terminal(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ("a terminal is required", "no tty present", "askpass",
                                "password is required", "a password is required",
                                "sudo: no password", "unable to read password"))


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def find_askpass(host: Host) -> str | None:
    for p in ASKPASS:
        if host.exists(p):
            return p
    return None


@dataclass
class CliStatus:
    state: str
    text: str = ""
    update: str | None = None          # newer adguard-cli version, if it says so
    details: dict = field(default_factory=dict)


class AdGuard:
    """Thin wrapper around ``adguard-cli``."""

    def __init__(self, host: Host | None = None):
        self.host = host or Host()
        self._path: str | None = None
        self._looked = 0.0
        self._askpass: str | None = None
        self.update_available: str | None = None
        self._port: int | None = None
        self._version_checked = 0.0

    # -- finding it
    def path(self, refresh: bool = False) -> str | None:
        if sys.platform == "win32":
            return None
        # Look again at most every minute (it may be installed while CatFlap runs).
        if refresh or (self._path is None and (not self._looked or time.monotonic() - self._looked > 60)):
            self._looked = time.monotonic()
            self._path = None
            if self.host.has_access():
                found = self.host.which(CLI)
                if not found:
                    for p in CLI_PATHS:
                        p = os.path.expanduser(p)
                        if self.host.exists(p):
                            found = p
                            break
                self._path = found
        return self._path

    def installed(self) -> bool:
        return bool(self.path())

    # -- commands
    def run(self, *args: str, timeout: float = 30) -> Result:
        exe = self.path()
        if not exe:
            raise AdGuardError("adguard-cli isn't installed (see adguard.com/adguard-linux)")
        env: dict[str, str] = {}
        if self._askpass is None:
            self._askpass = find_askpass(self.host) or ""
        askpass = self._askpass
        if askpass:
            env["SUDO_ASKPASS"] = askpass       # used if adguard-cli asks sudo for a password
        return self.host.run([exe, *args], timeout=timeout, env=env)

    def status(self) -> CliStatus:
        if sys.platform == "win32":
            return CliStatus(MISSING)
        if self.host.flatpak and not self.host.has_access():
            return CliStatus(NO_ACCESS)
        if not self.path():
            return CliStatus(MISSING)
        r = self.run("status", timeout=20)
        text, update = split_notice(r.text)
        state = parse_status(r.rc, text)
        if state == UNKNOWN and r.rc != 0 and not text:
            state = OFF
        if update:
            self.update_available = update
        elif self.update_available and (not self._version_checked
                                        or time.monotonic() - self._version_checked > 600):
            self._version_checked = time.monotonic()
            have = self.version()
            if have and version_key(have) >= version_key(self.update_available):
                self.update_available = None        # updated meanwhile
        st = CliStatus(state, text, update or self.update_available, parse_details(text))
        if "http_port" in st.details:
            self._port = st.details["http_port"]
        return st

    def _act(self, *args: str, timeout: float = 90) -> str:
        r = self.run(*args, timeout=timeout)
        text, update = split_notice(r.text)
        if update:
            self.update_available = update
        if r.rc != 0:
            if _needs_terminal(text):
                raise NeedsTerminal([self.path() or CLI, *args], text)
            raise AdGuardError(text or f"adguard-cli {' '.join(args)} failed (exit {r.rc})")
        return text

    def version(self) -> str | None:
        """Installed version, e.g. "1.5.0" (from ``adguard-cli --version``)."""
        m = re.search(r"AdGuard CLI v?(\d[\w.\-]*)", split_notice(self.run("--version", timeout=20).text)[0])
        return m.group(1) if m else None

    def license(self) -> str:
        return self._act("license", timeout=30)

    def argv(self, *args: str) -> list[str]:
        """Full command line, for running in a terminal."""
        return [self.path() or CLI, *args]

    def start(self) -> str:
        return self._act("start")

    def stop(self) -> str:
        return self._act("stop")

    def restart(self) -> str:
        return self._act("restart")

    def check_update(self) -> str:
        return self._act("check-update", timeout=300)

    def filters_list(self) -> str:
        return self._act("filters", "list", timeout=30)

    def install_browser_integration(self) -> str:
        return self._act("install-browser-integration", timeout=60)

    def config_get(self, key: str) -> str:
        r = self.run("config", "get", key, timeout=20)
        return split_notice(r.out)[0] if r.rc == 0 else ""

    def config_location(self) -> str | None:
        r = self.run("config", "show", timeout=20)
        m = re.search(r"config location:\s*(\S+)", r.text, re.I)
        if m:
            return m.group(1)
        for p in ("~/.local/share/adguard-cli/proxy.yaml", "~/.config/adguard-cli/proxy.yaml"):
            p = os.path.expanduser(p)
            if self.host.exists(p):
                return p
        return None

    def proxy_port(self) -> int | None:
        if self._port:
            return self._port
        m = re.findall(r"\d{2,5}", self.config_get("listen_ports.http_proxy"))
        return int(m[-1]) if m else None

    def proxy_mode(self) -> str:
        v = self.config_get("proxy_mode").lower()
        return "manual" if "manual" in v else "auto" if "auto" in v else ""


def terminal_argv(host: Host, argv: list[str], title: str = "AdGuard") -> list[str] | None:
    """A command that opens a terminal running ``argv`` and waits for Enter."""
    import shlex
    script = (f"printf '%s\\n\\n' {shlex.quote(title)}; " + " ".join(shlex.quote(a) for a in argv)
              + "; echo; read -r -p 'Done. Press Enter to close this window.' _")
    for name, flag in TERMINALS:
        if host.which(name):
            return [name, *flag, "bash", "-c", script]
    return None


def open_terminal(host: Host, argv: list[str], title: str = "AdGuard") -> bool:
    cmd = terminal_argv(host, argv, title)
    if not cmd:
        return False
    subprocess.Popen(host.argv(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # noqa: S603
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return True


# ============================================================ pause / resume
def effective_state(raw: str, settings: dict, now: float | None = None) -> str:
    """Turn ``adguard-cli status`` into what the tray shows, given CatFlap's pause.

    Paused = stopped by CatFlap's Pause and not yet resumed. If AdGuard was started
    some other way meanwhile, the pause is forgotten."""
    until = settings.get(PAUSE_KEY)
    if until is None:
        return raw
    if raw == ON:
        settings.pop(PAUSE_KEY, None)
        return ON
    if raw == OFF:
        return PAUSED
    return raw


def pause_due(settings: dict, now: float | None = None) -> bool:
    until = settings.get(PAUSE_KEY)
    if until is None or until == FOREVER:
        return False
    return (time.time() if now is None else now) >= float(until)


def pause_left(settings: dict, now: float | None = None) -> float | None:
    """Seconds of pause left, None if not paused, -1 if paused until resumed."""
    until = settings.get(PAUSE_KEY)
    if until is None:
        return None
    if until == FOREVER:
        return FOREVER
    return max(0.0, float(until) - (time.time() if now is None else now))


def pause(ag: AdGuard, settings: dict, seconds: float | None, now: float | None = None) -> None:
    """Stop protection; the tray starts it again after ``seconds`` (None = until resumed)."""
    ag.stop()
    now = time.time() if now is None else now
    settings[PAUSE_KEY] = FOREVER if seconds is None else now + seconds


def resume(ag: AdGuard, settings: dict) -> None:
    ag.start()
    settings.pop(PAUSE_KEY, None)


def turn_off(ag: AdGuard, settings: dict) -> None:
    ag.stop()
    settings.pop(PAUSE_KEY, None)


def describe(state: str, settings: dict, now: float | None = None) -> str:
    if state == PAUSED:
        left = pause_left(settings, now)
        if left is None or left == FOREVER:
            return "Paused until you resume"
        mins = int(left // 60) + (1 if left % 60 else 0)
        return f"Paused, {mins} min left" if mins < 120 else f"Paused, {round(left / 3600)} h left"
    return LABELS.get(state, state)


# ============================================================ HTTPS filtering: certificates
def _x509():
    from cryptography import x509
    return x509


def is_adguard_name(name: str) -> bool:
    return "adguard" in name.lower()


def issuer_of(der: bytes) -> str:
    try:
        return _x509().load_der_x509_certificate(der).issuer.rfc4514_string()
    except Exception:  # noqa: BLE001 - not a certificate we can read
        return ""


def intercepted_by_adguard(der: bytes | None) -> bool:
    """True if this server certificate was issued by AdGuard (HTTPS filtering)."""
    return bool(der) and is_adguard_name(issuer_of(der))


def parse_certs(data: bytes) -> list:
    """Certificates in PEM (one or many) or DER."""
    x509 = _x509()
    if b"-----BEGIN CERTIFICATE-----" in data:
        try:
            return list(x509.load_pem_x509_certificates(data))
        except ValueError:
            out = []
            for block in re.findall(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----",
                                    data, re.S):
                try:
                    out.append(x509.load_pem_x509_certificate(block))
                except ValueError:
                    pass
            return out
    try:
        return [x509.load_der_x509_certificate(data)]
    except ValueError:
        return []


def adguard_cas(certs: list) -> list:
    return [c for c in certs if is_adguard_name(c.subject.rfc4514_string())]


def cert_pem(cert) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding
    return cert.public_bytes(Encoding.PEM)


def _links(html: bytes) -> list[str]:
    return [h.decode(errors="replace") for h in re.findall(rb'href=["\']([^"\']+)["\']', html, re.I)]


def fetch_ca(port: int | None = None, opener: Callable[..., Any] | None = None,
             timeout: float = 8):
    """AdGuard's CA from http://local.adguard.org/cert, a page that AdGuard itself
    answers while it filters this connection (auto mode) or through its proxy
    (manual mode, ``port``). The page may be the certificate or a page linking to it."""
    from urllib.parse import urljoin
    openers = []
    if opener is not None:
        openers.append(opener)
    else:
        openers.append(urllib.request.build_opener(urllib.request.ProxyHandler({})).open)
        if port:
            px = f"http://127.0.0.1:{port}"
            openers.append(urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": px, "https": px})).open)
    for op in openers:
        todo, seen = [CA_URL], set()
        while todo and len(seen) < 6:
            url = todo.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                with op(url, timeout=timeout) as r:
                    data = r.read(2_000_000)
            except Exception:  # noqa: BLE001 - not reachable this way
                break
            cas = adguard_cas(parse_certs(data))
            if cas:
                return cas[0]
            for link in _links(data):
                full = urljoin(url, link)
                if "adguard.org" in full and ("cert" in link.lower() or link.lower().endswith(
                        (".pem", ".crt", ".cer", ".der"))):
                    todo.append(full)
    return None


SYSTEM_BUNDLES = ("/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/certs/ca-certificates.crt",
                  "/etc/ssl/cert.pem", "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem")


def system_cas(host: Host) -> tuple[str | None, list]:
    """(bundle path, AdGuard CAs in it)."""
    for b in SYSTEM_BUNDLES:
        data = host.read(b)
        if data:
            return b, adguard_cas(parse_certs(data))
    return None, []


def same_cert(a, b) -> bool:
    from cryptography.hazmat.primitives.serialization import Encoding
    return a.public_bytes(Encoding.DER) == b.public_bytes(Encoding.DER)


@dataclass
class Probe:
    host: str
    reachable: bool = False
    intercepted: bool = False
    issuer: str = ""
    verified: bool = False         # this process (system trust) accepts it
    error: str = ""
    der: bytes | None = None


def probe(hostname: str, port: int = 443, timeout: float = 8,
          connect: Callable[..., Any] = socket.create_connection) -> Probe:
    """Who signed the certificate we get for ``hostname``, and do we trust it?"""
    pr = Probe(hostname)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with connect((hostname, port), timeout=timeout) as s, \
                ctx.wrap_socket(s, server_hostname=hostname) as ts:
            pr.der = ts.getpeercert(binary_form=True)
    except OSError as e:
        pr.error = str(e)
        return pr
    pr.reachable = True
    pr.issuer = issuer_of(pr.der or b"")
    pr.intercepted = is_adguard_name(pr.issuer)
    try:
        with connect((hostname, port), timeout=timeout) as s, \
                ssl.create_default_context().wrap_socket(s, server_hostname=hostname):
            pr.verified = True
    except ssl.SSLCertVerificationError as e:
        pr.error = e.verify_message or str(e)
    except OSError as e:
        pr.error = str(e)
    return pr


# ============================================================ HTTPS filtering: browser stores
FLATPAK_NAMES = {
    "org.mozilla.firefox": "Firefox (Flatpak)", "io.gitlab.librewolf-community": "LibreWolf (Flatpak)",
    "one.ablaze.floorp": "Floorp (Flatpak)", "app.zen_browser.zen": "Zen (Flatpak)",
    "net.waterfox.waterfox": "Waterfox (Flatpak)", "org.mozilla.Thunderbird": "Thunderbird (Flatpak)",
    "com.google.Chrome": "Google Chrome (Flatpak)", "org.chromium.Chromium": "Chromium (Flatpak)",
    "com.brave.Browser": "Brave (Flatpak)", "com.microsoft.Edge": "Microsoft Edge (Flatpak)",
    "com.vivaldi.Vivaldi": "Vivaldi (Flatpak)", "com.opera.Opera": "Opera (Flatpak)",
    "io.github.ungoogled_software.ungoogled_chromium": "Ungoogled Chromium (Flatpak)",
}
NSS_PATTERNS = (
    ("~/.mozilla/firefox/*/cert9.db", "Firefox"),
    ("~/.config/mozilla/firefox/*/cert9.db", "Firefox"),
    ("~/.librewolf/*/cert9.db", "LibreWolf"),
    ("~/.floorp/*/cert9.db", "Floorp"),
    ("~/.zen/*/cert9.db", "Zen"),
    ("~/.waterfox/*/cert9.db", "Waterfox"),
    ("~/.thunderbird/*/cert9.db", "Thunderbird"),
    ("~/.pki/nssdb/cert9.db", "Chrome, Chromium, Brave, Edge, Vivaldi"),
    ("~/.var/app/*/.mozilla/firefox/*/cert9.db", None),
    ("~/.var/app/*/config/mozilla/firefox/*/cert9.db", None),
    ("~/.var/app/*/.librewolf/*/cert9.db", None),
    ("~/.var/app/*/.floorp/*/cert9.db", None),
    ("~/.var/app/*/.zen/*/cert9.db", None),
    ("~/.var/app/*/.waterfox/*/cert9.db", None),
    ("~/.var/app/*/.thunderbird/*/cert9.db", None),
    ("~/.var/app/*/.pki/nssdb/cert9.db", None),
)


@dataclass
class NssStore:
    path: str            # directory holding cert9.db
    name: str            # "Firefox (Flatpak) - default-release"
    flatpak: bool
    chromium: bool


def nss_stores(host: Host) -> list[NssStore]:
    out, seen = [], set()
    for pat, name in NSS_PATTERNS:
        for db in host.glob([pat]):
            d = os.path.dirname(db)
            if d in seen:
                continue
            seen.add(d)
            chromium = d.endswith("/.pki/nssdb")
            fp = "/.var/app/" in d
            if fp:
                app = d.split("/.var/app/", 1)[1].split("/", 1)[0]
                label = FLATPAK_NAMES.get(app, f"{app} (Flatpak)")
            else:
                label = name or "Browser"
            if not chromium:
                prof = os.path.basename(d)
                label += f" · profile {prof.split('.', 1)[-1] if '.' in prof else prof}"
            out.append(NssStore(d, label, fp, chromium))
    return out


def p11kit_wired(host: Host) -> bool:
    """Fedora points NSS's built-in roots at the system store (p11-kit), so the
    distribution's Firefox and Chrome trust whatever the system trusts."""
    for lib in ("/usr/lib64/libnssckbi.so", "/usr/lib/x86_64-linux-gnu/libnssckbi.so",
                "/usr/lib/aarch64-linux-gnu/libnssckbi.so", "/usr/lib/libnssckbi.so"):
        if host.flatpak:
            r = host.run(["readlink", "-f", lib], timeout=10)
            target = r.out.strip() if r.rc == 0 else ""
        else:
            target = os.path.realpath(lib) if os.path.exists(lib) else ""
        if target and "p11-kit" in target:
            return True
    return False


def store_has(host: Host, store: NssStore, ca) -> bool | None:
    """Is ``ca`` in this NSS database? None if we can't tell."""
    certutil = host.which("certutil")
    if certutil:
        r = host.run([certutil, "-L", "-d", f"sql:{store.path}"], timeout=15)
        if r.rc == 0:
            if any(is_adguard_name(line) for line in r.out.splitlines()):
                return True
    data = host.read(os.path.join(store.path, "cert9.db"))
    if data is None:
        return None
    # cert9.db is SQLite; the certificate's DER encoding is stored as-is.
    from cryptography.hazmat.primitives.serialization import Encoding
    return ca.public_bytes(Encoding.DER) in data


# ============================================================ the report
OK, BAD, INFO = "ok", "bad", "info"


@dataclass
class Check:
    key: str
    title: str
    result: str                    # OK / BAD / INFO
    detail: str = ""
    fix: str = ""                  # fix id, see fix()
    fix_label: str = ""
    target: str = ""               # what the fix applies to (store path, app name...)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    ca: Any = None
    ca_file: str = ""
    filtering: bool = False        # we saw AdGuard re-signing a connection

    def add(self, *a, **kw) -> Check:
        c = Check(*a, **kw)
        self.checks.append(c)
        return c

    @property
    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.result == BAD]


def save_ca(ca) -> str:
    p = scratch_dir() / "adguard-personal-ca.pem"
    p.write_bytes(cert_pem(ca))
    return str(p)


def https_report(ag: AdGuard, gateways: dict[str, dict] | None = None,
                 probe_fn: Callable[..., Probe] = probe,
                 fetch_fn: Callable[..., Any] = fetch_ca) -> Report:
    """Everything that decides whether AdGuard's HTTPS filtering works on this PC."""
    rep = Report()
    host = ag.host
    st = ag.status()
    if st.state in (MISSING, NO_ACCESS):
        rep.add("adguard", "AdGuard for Linux", BAD, LABELS[st.state])
        return rep
    rep.add("adguard", "AdGuard protection", OK if st.state == ON else INFO,
            LABELS.get(st.state, st.state) + ("" if st.state == ON else
                                              ": turn it on to check HTTPS filtering properly."))

    # 1. Is HTTPS filtering actually happening on this computer?
    pr = probe_fn("example.com")
    rep.filtering = pr.intercepted
    mode = ag.proxy_mode()
    if pr.intercepted:
        rep.add("filtering", "HTTPS filtering", OK, "Active: AdGuard is filtering HTTPS connections.")
    elif not pr.reachable:
        rep.add("filtering", "HTTPS filtering", INFO, f"Couldn't reach the internet to check ({pr.error}).")
    else:
        why = ("AdGuard is in manual proxy mode, so only apps set to use its proxy are filtered"
               if mode == "manual" else "HTTPS filtering is off, or this app is excluded from it")
        rep.add("filtering", "HTTPS filtering", INFO,
                f"CatFlap's own test connection wasn't filtered: {why}. The checks below still "
                "look at where AdGuard's certificate is trusted.")

    # 2. AdGuard's certificate
    bundle, sys_cas = system_cas(host)
    ca = fetch_fn(ag.proxy_port()) if st.state == ON else None
    if ca is None and sys_cas:
        ca = sys_cas[0]
    if ca is None:
        for p in host.glob(["/etc/pki/ca-trust/source/anchors/*", "/usr/local/share/ca-certificates/*",
                            "~/.local/share/adguard-cli/*.pem", "~/.local/share/adguard-cli/*.crt"]):
            cas = adguard_cas(parse_certs(host.read(p) or b""))
            if cas:
                ca = cas[0]
                break
    if ca is None:
        rep.add("ca", "AdGuard's certificate", BAD if pr.intercepted else INFO,
                "Couldn't get AdGuard's certificate. AdGuard can create and install it "
                "('adguard-cli cert'); or, with protection and HTTPS filtering on, open "
                "http://local.adguard.org/cert in a browser to download it.",
                fix="cert", fix_label="Run AdGuard's certificate setup")
        return rep
    rep.ca = ca
    rep.ca_file = save_ca(ca)
    rep.add("ca", "AdGuard's certificate", OK,
            f"{ca.subject.rfc4514_string()} (saved to {rep.ca_file})")

    # 3. System store
    sys_ok = any(same_cert(c, ca) for c in sys_cas)
    rep.add("system", "System certificates (curl, dnf, Python, CatFlap)", OK if sys_ok else BAD,
            "AdGuard's certificate is trusted." if sys_ok else
            "Not trusted: command-line tools, updates and many apps fail while HTTPS filtering "
            "is on." + ("" if bundle else " (No system certificate bundle found.)"),
            fix="" if sys_ok else "system", fix_label="Trust it (asks for your password)")

    # 4. Browsers
    wired = p11kit_wired(host)
    stores = nss_stores(host)
    certutil = host.which("certutil")
    for s in stores:
        has = store_has(host, s, ca)
        uses_system = wired and not s.flatpak
        if has:
            rep.add("nss", s.name, OK, "AdGuard's certificate is trusted.", target=s.path)
        elif uses_system and sys_ok:
            rep.add("nss", s.name, OK, "Uses the system certificates, which trust AdGuard. If this "
                    "browser still shows certificate errors (Firefox from mozilla.org brings its own "
                    "list), use Fix.", fix="nss" if certutil else "", fix_label="Add to this browser",
                    target=s.path)
        else:
            why = ("Flatpak browsers don't see the system certificates." if s.flatpak else
                   "Fix the system certificates above first." if uses_system else
                   "This browser keeps its own certificate list.")
            rep.add("nss", s.name, BAD, f"Not trusted: sites show certificate errors. {why}"
                    + ("" if certutil else " Adding it needs 'certutil': install nss-tools "
                       "(Fedora) or libnss3-tools (Debian/Ubuntu)."),
                    fix="nss" if certutil else "", fix_label="Add to this browser", target=s.path)
    if not stores:
        rep.add("nss", "Browsers", INFO, "No Firefox or Chrome profiles found in your home folder.")

    # 5. CatFlap's own connections
    if pr.intercepted:
        gh = probe_fn("api.github.com")
        rep.add("catflap", "CatFlap update checks", OK if gh.verified or not gh.intercepted else BAD,
                "Work through HTTPS filtering." if gh.verified or not gh.intercepted else
                "Fail while HTTPS filtering is on (" + (gh.error or "certificate not trusted") + "). "
                "Trusting the certificate in the system fixes the RPM version; the Flatpak "
                "version has its own certificate list, so exclude it from HTTPS filtering.",
                fix="" if gh.verified or not gh.intercepted else "exclude",
                fix_label="Exclude CatFlap from HTTPS filtering", target="python3*")
    for name, gw in (gateways or {}).items():
        url = gw.get("url") or ""
        m = re.match(r"https://([^/:]+)(?::(\d+))?", url)
        if not m:
            continue
        gp = probe_fn(m.group(1), int(m.group(2) or 443))
        if gp.intercepted:
            rep.add("gateway", f"Gateway {name}", BAD,
                    "AdGuard is re-signing the connection to this gateway, so CatFlap can't check "
                    "it's really your gateway and refuses to connect.", fix="exclude",
                    fix_label="Exclude CatFlap from HTTPS filtering", target="python3*")
        elif gp.reachable:
            rep.add("gateway", f"Gateway {name}", OK, "Not affected by HTTPS filtering.")
    return rep


# ============================================================ fixes
def system_fix_argv(host: Host, pem_file: str) -> list[str]:
    """pkexec command that adds the CA to the system store (Fedora or Debian layout)."""
    if host.exists("/etc/pki/ca-trust/source/anchors"):
        script = ('install -m 0644 "$1" /etc/pki/ca-trust/source/anchors/adguard-personal-ca.pem '
                  '&& update-ca-trust extract')
    else:
        script = ('install -m 0644 "$1" /usr/local/share/ca-certificates/adguard-personal-ca.crt '
                  '&& update-ca-certificates')
    return ["pkexec", "sh", "-c", script, "sh", pem_file]


def fix_system(host: Host, pem_file: str) -> None:
    r = host.run(system_fix_argv(host, pem_file), timeout=300)
    if r.rc == 126:
        raise AdGuardError("Cancelled.")
    if r.rc != 0:
        raise AdGuardError(r.text or f"couldn't add the certificate (exit {r.rc})")


def fix_nss(host: Host, store_path: str, pem_file: str) -> None:
    certutil = host.which("certutil")
    if not certutil:
        raise AdGuardError("'certutil' isn't installed: install nss-tools (Fedora) or "
                           "libnss3-tools (Debian/Ubuntu) and try again.")
    r = host.run([certutil, "-A", "-n", CA_NICK, "-t", "C,,", "-i", pem_file,
                  "-d", f"sql:{store_path}"], timeout=30)
    if r.rc != 0:
        raise AdGuardError(r.text or f"certutil failed (exit {r.rc})")


_APPS_RE = re.compile(r"^apps:[ \t]*(\[\s*\])?[ \t]*$", re.M)


def has_app_rule(text: str, app: str) -> bool:
    return bool(re.search(r"^\s*-?\s*name:\s*['\"]?" + re.escape(app) + r"['\"]?\s*$", text, re.M))


def add_app_exclusion(text: str, app: str, action: str = "bypass_https") -> str:
    """``proxy.yaml`` with an ``apps`` rule for ``app`` added first in the list (first match wins).

    Edits the text in place (no YAML library, so comments and order are kept)."""
    if not re.fullmatch(r"[A-Za-z0-9_.*+\-]+", app):
        raise ValueError(f"not an app name: {app!r}")
    if action not in ("bypass_https", "bypass", "default"):
        raise ValueError(f"unknown action {action!r}")
    if has_app_rule(text, app):
        return text
    m = _APPS_RE.search(text)
    if not m:
        sep = "" if text.endswith("\n") or not text else "\n"
        return f"{text}{sep}apps:\n  - name: '{app}'\n    action: '{action}'\n"
    indent = "  "
    rest = text[m.end():]
    nxt = re.search(r"^([ \t]*)- ", rest, re.M)
    if nxt and not m.group(1):
        indent = nxt.group(1)
    entry = f"{indent}- name: '{app}'\n{indent}  action: '{action}'\n"
    head = text[:m.start()] + "apps:\n"
    tail = rest[1:] if rest.startswith("\n") else rest
    return head + entry + tail


def exclude_app(ag: AdGuard, app: str, action: str = "bypass_https") -> str:
    """Add an app exclusion to AdGuard's proxy.yaml (keeping a backup) and restart it."""
    path = ag.config_location()
    if not path:
        raise AdGuardError("Couldn't find AdGuard's proxy.yaml ('adguard-cli config show' "
                           "doesn't say where it is).")
    old = ag.host.read(path)
    if old is None:
        raise AdGuardError(f"Couldn't read {path}.")
    text = old.decode()
    new = add_app_exclusion(text, app, action)
    if new == text:
        return f"'{app}' already has its own rule in {path}."
    ag.host.write(path + ".catflap-backup", old)
    ag.host.write(path, new.encode())
    if ag.status().state == ON:
        ag.restart()
    return f"Added '{app}' to AdGuard's exclusions ({action}). Backup: {path}.catflap-backup"


# Apps people commonly exclude from HTTPS filtering (own certificate lists or pinning).
EXCLUDE_SUGGESTIONS = ("python3*", "node", "code", "codium", "discord", "slack", "steam",
                       "signal-desktop", "teams-for-linux", "git-remote-https", "flatpak",
                       "dropbox", "1password", "bitwarden", "spotify")


def apply_fix(ag: AdGuard, check: Check, report: Report) -> str:
    """Do what a check's Fix button says. Returns a message for the user."""
    if check.fix == "system":
        fix_system(ag.host, report.ca_file)
        return ("AdGuard's certificate is now trusted by the system. Restart apps that were "
                "already open (browsers included).")
    if check.fix == "nss":
        fix_nss(ag.host, check.target, report.ca_file)
        return f"Added to {check.title}. Restart that browser."
    if check.fix == "exclude":
        return exclude_app(ag, check.target)
    if check.fix == "cert":
        if not open_terminal(ag.host, ag.argv("cert"), "AdGuard: certificate for HTTPS filtering"):
            raise AdGuardError("No terminal app found. Run this in a terminal: adguard-cli cert")
        return ("A terminal opened with AdGuard's certificate setup (adguard-cli cert). When it "
                "has finished, click Check again.")
    raise AdGuardError(f"nothing to fix for {check.title}")
