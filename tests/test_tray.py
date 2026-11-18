"""Headless tests of the window, tray and wizards (skipped without PySide6)."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from catflap import tray, ui_wizards  # noqa: E402
from catflap.ui_common import make_icon  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def no_popups(monkeypatch):
    shown = []
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, kind,
                            lambda *a, k=kind, **kw: shown.append((k, a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(ui_wizards, "fetch_fingerprint", lambda url: "AA:BB")
    return shown


def wait_for(qapp, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        qapp.processEvents()
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_icon_states(qapp):
    off = make_icon([]).pixmap(64, 64).toImage()
    on = make_icon(["#ff0000"]).pixmap(64, 64).toImage()          # default style: site color
    assert off != on
    tile = on.pixelColor(4, 32)
    assert tile.red() > 150 and tile.blue() < 100
    dot = on.pixelColor(51, 51)                                     # green status dot
    assert dot.green() > 150 and dot.red() < 100
    assert off.pixelColor(4, 32).alpha() == 0                      # disconnected: outline only
    err = make_icon([], error=True).pixmap(64, 64).toImage().pixelColor(51, 51)
    assert err.red() > 180 and err.green() < 100


def test_icon_styles_for_several_sites(qapp):
    red, blue = "#ff0000", "#0000ff"
    grad = make_icon([red, blue]).pixmap(64, 64).toImage()          # blend across both
    top_left, bottom_right = grad.pixelColor(6, 6), grad.pixelColor(58, 40)
    assert top_left.red() > top_left.blue() and bottom_right.blue() > bottom_right.red()
    c0 = make_icon([red, blue], style="cycle", phase=0.0).pixmap(64, 64).toImage().pixelColor(4, 32)
    c1 = make_icon([red, blue], style="cycle", phase=1.0).pixmap(64, 64).toImage().pixelColor(4, 32)
    assert c0.red() > c0.blue() and c1.blue() > c1.red()           # cycles from red to blue
    b = make_icon([red, blue], style="blue").pixmap(64, 64).toImage().pixelColor(4, 32)
    assert b.blue() > 150 and b.red() < 100                        # always the blue app icon


def test_tray_menu_and_basic_window(qapp, manager, backend):
    manager.provision("home-gw", "home", ["LAN"], device="d", color="#ff0000", label="Home Sweet Home")
    app = tray.TrayApp(qapp, manager)
    app.timer.stop()
    app.profiles = manager.store.profiles()
    app.status = manager.status()
    app._render()
    labels = [a.text() for a in app.menu.actions()]
    assert "Home Sweet Home: Not connected  (Connect)" in labels
    assert "Add a site…" in labels and "Open CatFlap" in labels and "Quit" in labels
    assert "Check for updates…" in labels
    backend.active.add("home")
    app.status = manager.status()
    app._render()
    assert "Home Sweet Home connected" in app.tray.toolTip()
    app.show_window()
    basic = app.window.basic
    assert len(basic.cards) == 1 and basic.cards[0].button.text() == "Disconnect"
    app.window.set_mode("advanced")
    assert app.settings["ui_mode"] == "advanced"
    assert app.window.advanced.sites.rowCount() == 1
    assert app.window.advanced.gws.rowCount() == 1
    app.settings["close_behavior"] = "tray"
    app.window.close()
    assert not app.window.isVisible() and not app.quitting   # closed to the tray


def test_basic_view_empty_state(qapp, manager):
    app = tray.TrayApp(qapp, manager)
    app.timer.stop()
    app.show_window()
    assert app.window.basic.stack.currentIndex() == 0          # "Add your first site"


def test_first_run_wizard_previews_and_choice(qapp):
    w = ui_wizards.FirstRunWizard()
    assert w.mode == "basic"
    w.group.button(1).setChecked(True)
    assert w.mode == "advanced"
    assert w.add_now.isChecked()


def pick(w, vendor):
    vp = w.page(ui_wizards.P_VENDOR)
    next(b for b in vp.group.buttons() if b.property("vendor") == vendor).setChecked(True)
    w.next()


CONSOLES = [{"id": "AA:11", "name": "Home UDM", "model": "UDM Pro", "online": True},
            {"id": "BB:22", "name": "Office UCG", "model": "", "online": True}]


def test_site_wizard_unifi_cloud(qapp, manager, no_popups, monkeypatch):
    from catflap.drivers import unifi
    monkeypatch.setattr(unifi, "list_cloud_consoles", lambda key: CONSOLES)
    w = ui_wizards.SiteWizard(manager)
    w.show()
    pick(w, "unifi")
    assert w.currentId() == ui_wizards.P_LOGIN
    access = w.currentPage()
    assert access.cloud.isChecked() and access.address.isHidden()   # no router address asked
    access.key.setText("CLOUDKEY")
    w.next()
    assert w.currentId() == ui_wizards.P_CONSOLE, no_popups
    cp = w.currentPage()
    assert cp.list.count() == 2
    cp.list.setCurrentRow(1)
    w.next()
    assert w.currentId() == ui_wizards.P_SITE, no_popups
    gw = manager.store.gateway(w.gateway)
    assert gw["url"] == "https://api.ui.com/v1/connector/consoles/BB:22"
    assert gw["options"]["mode"] == "cloud" and gw["options"]["console_name"] == "Office UCG"
    assert manager.secrets.get(f"gateway/{w.gateway}/api_key") == "CLOUDKEY"
    w.next()
    server = w.currentPage()
    wait_for(qapp, lambda: server.ok)
    w.next()
    assert w.currentPage().chosen() == ["192.168.1.0/24"]
    w.next()
    assert w.currentPage().name.text() == "Home"
    w.next()
    assert w.currentId() == ui_wizards.P_DONE, no_popups
    assert w.result.profile.gateway == w.gateway


def test_site_wizard_unifi_direct(qapp, manager, no_popups):
    w = ui_wizards.SiteWizard(manager)
    w.show()
    pick(w, "unifi")
    access = w.currentPage()
    access.direct.setChecked(True)
    assert not access.address.isHidden()
    access.address.setText("192.168.7.1")
    access.key.setText("LOCALKEY")
    w.next()
    assert w.currentId() == ui_wizards.P_SITE, no_popups
    assert manager.store.gateway(w.gateway)["url"] == "https://192.168.7.1"


def test_site_wizard_omada_typed_in(qapp, manager, no_popups):
    from catflap import keys
    _, srv_pub = keys.generate_keypair()
    w = ui_wizards.SiteWizard(manager)
    w.show()
    pick(w, "omada")
    assert w.currentId() == ui_wizards.P_METHOD
    assert w.currentPage().manual.isChecked()
    w.next()
    mp = w.currentPage()
    assert "Settings → VPN → WireGuard" in mp.help.toPlainText().replace("->", "→")
    mp.server_key.setText(srv_pub)
    mp.endpoint.setText("cabin.tplinkdns.com")
    mp.address.setText("10.250.3.2")
    w.next()
    assert w.currentId() == ui_wizards.P_SITE_NETS, no_popups
    nets = w.currentPage()
    nets.new.setText("192.168.50.0")
    nets._add()
    assert nets.chosen() == ["192.168.50.0/24"]
    w.next()
    w.currentPage().name.setText("Cabin")
    w.next()
    assert w.currentId() == ui_wizards.P_DONE, no_popups
    p = w.result.profile
    assert p.vendor == "omada" and p.routes == ["192.168.50.0/24"] and p.addresses == ["10.250.3.2/32"]
    assert p.endpoint_host == "cabin.tplinkdns.com"


def test_site_wizard_alta_file(qapp, manager, no_popups, unifi_conf):
    w = ui_wizards.SiteWizard(manager)
    w.show()
    pick(w, "alta")
    assert w.currentId() == ui_wizards.P_FILE
    fp = w.currentPage()
    assert "manage.alta.inc" in fp.help.toPlainText()
    fp.path.setText(str(unifi_conf))
    w.next()
    nets = w.currentPage()
    assert nets.chosen() == []               # the file only had a full tunnel; nothing pre-ticked
    assert not w.button(QtWidgets.QWizard.NextButton).isEnabled()
    nets.new.setText("10.20.0.0/24")
    nets._add()
    w.next()
    w.currentPage().name.setText("Lake house")
    w.next()
    assert w.currentId() == ui_wizards.P_DONE, no_popups
    assert w.result.profile.routes == ["10.20.0.0/24"]


def test_site_wizard_rejects_bad_file(qapp, manager, no_popups, tmp_path):
    bad = tmp_path / "x.conf"
    bad.write_text("not a wireguard file")
    w = ui_wizards.SiteWizard(manager)
    w.show()
    pick(w, "alta")
    w.currentPage().path.setText(str(bad))
    w.next()
    assert w.currentId() == ui_wizards.P_FILE
    assert any("doesn't look like a WireGuard" in m for _, m in no_popups)


def test_gateway_wizard_cloud_single_console(qapp, manager, no_popups, monkeypatch):
    from catflap.drivers import unifi
    monkeypatch.setattr(unifi, "list_cloud_consoles", lambda key: CONSOLES[:1])
    w = ui_wizards.GatewayWizard(manager)
    w.show()
    w.next()
    w.access_page.key.setText("K2")
    assert w.access_page.validatePage()
    assert manager.store.gateway(w.gateway)["options"]["console_id"] == "AA:11"
    # adding the same console again reuses it
    assert manager.add_unifi_cloud_gateway("K3", "AA:11", "Home UDM") == w.gateway


def test_update_dialog_flatpak_manual_then_restart(qapp, monkeypatch, tmp_path):
    from catflap import update, ui_update
    rel = update.ReleaseInfo(version="9.9", url="https://x/releases/v9.9", notes="* new stuff",
                             assets={"catflap-9.9.flatpak": "https://dl/catflap-9.9.flatpak"})
    st = update.check({}, fetch=lambda: rel, install=update.InstallInfo("flatpak", "io.x.CatFlap", True))
    settings = {}
    d = ui_update.UpdateDialog(st, settings=settings)
    assert "curl -L -o ~/Downloads/catflap-9.9.flatpak https://dl/catflap-9.9.flatpak" in d.cmds.toPlainText()
    assert d.go.text() == "Download and install"
    bundle = tmp_path / "catflap-9.9.flatpak"
    monkeypatch.setattr(update, "apply_update", lambda s: update.UpdateResult(
        False, "Downloaded", restart=True, downloaded=bundle,
        commands=[f"flatpak install --user -y {bundle}"]))
    d.do_update()
    assert d.cmds.toPlainText() == f"flatpak install --user -y {bundle}"
    assert not d.restart_btn.isHidden()
    d.do_restart()
    assert d.restart_requested and settings["pending_update"] == "9.9"


def test_after_restart_messages(monkeypatch):
    from catflap import ui_update
    monkeypatch.setattr(ui_update, "__version__", "1.3")
    s = {"last_run_version": "1.2"}
    assert ui_update.after_restart_message(s)[0] == "CatFlap updated"
    assert s["last_run_version"] == "1.3"
    assert ui_update.after_restart_message(s) is None
    s["pending_update"] = "1.4"
    assert ui_update.after_restart_message(s)[0] == "Update not installed yet"


def test_update_dialog_restarts_once_installed(qapp, monkeypatch, tmp_path):
    from catflap import update, ui_update
    rel = update.ReleaseInfo(version="9.9", url="u", assets={"catflap-9.9.flatpak": "https://dl/x.flatpak"})
    st = update.check({}, fetch=lambda: rel, install=update.InstallInfo("flatpak", "io.x.CatFlap", True))
    d = ui_update.UpdateDialog(st, settings={})
    d.COUNTDOWN = 0
    d.WATCH_MS = 10
    monkeypatch.setattr(update, "apply_update", lambda s: update.UpdateResult(
        False, "x", restart=True, downloaded=tmp_path / "x.flatpak", commands=["flatpak install …"]))
    versions = iter(["0.6", "9.9"])          # first check: not yet installed; then installed
    monkeypatch.setattr(update, "installed_version", lambda info: next(versions, "9.9"))
    d.do_update()
    wait_for(qapp, lambda: d.restart_requested, timeout=5)
    assert d.restart_requested


def test_update_dialog_auto_restart_countdown_can_be_cancelled(qapp, monkeypatch):
    from catflap import update, ui_update
    rel = update.ReleaseInfo(version="9.9", url="u", assets={})
    st = update.check({}, fetch=lambda: rel, install=update.InstallInfo("rpm", automatic=True))
    d = ui_update.UpdateDialog(st, settings={})
    monkeypatch.setattr(update, "apply_update", lambda s: update.UpdateResult(True, "installed", restart=True))
    d.do_update()
    assert "Restarting CatFlap in" in d.status.text()
    d.reject()                                 # "Don't restart now"
    qapp.processEvents()
    assert not d.restart_requested and not d._tick_timer.isActive()


def test_site_wizard_unifi_cloud_refused_falls_back_to_file(qapp, manager, no_popups, monkeypatch, unifi_conf):
    from catflap.drivers import unifi
    from catflap.drivers.unifi import ConnectorRefused
    from conftest import FakeDriver
    monkeypatch.setattr(unifi, "list_cloud_consoles", lambda key: CONSOLES[:1])
    def refuse(self, site, name=None):
        raise ConnectorRefused("only the official API (Ubiquiti said: HTTP 403: insufficient permissions)")
    monkeypatch.setattr(FakeDriver, "get_wireguard_server", refuse)
    monkeypatch.setattr(FakeDriver, "wireguard_servers_official",
                        lambda self, site: [{"name": "WG", "enabled": True}], raising=False)
    w = ui_wizards.SiteWizard(manager)
    w.show()
    pick(w, "unifi")
    w.currentPage().key.setText("K")
    w.next()                      # console page
    w.next()                      # site page
    w.next()                      # server page
    server = w.currentPage()
    wait_for(qapp, lambda: not server.file_btn.isHidden())
    assert "official API" in server.text.toPlainText()
    server.file_btn.click()
    assert w.currentId() == ui_wizards.P_FILE
    w.currentPage().path.setText(str(unifi_conf))
    w.next()
    assert w.currentId() == ui_wizards.P_NETWORKS          # networks still come from the API
    assert w.currentPage().chosen() == ["192.168.1.0/24"]
    w.next()
    w.currentPage().name.setText("HQ")
    w.next()
    assert w.currentId() == ui_wizards.P_DONE, no_popups
    p = w.result.profile
    assert p.vendor == "unifi" and p.routes == ["192.168.1.0/24"] and p.peer_public_key


def test_color_picker(qapp, monkeypatch):
    from catflap.model import PALETTE
    from catflap.ui_common import ColorPicker
    from PySide6 import QtGui
    cp = ColorPicker()
    assert len(cp.group.buttons()) == len(PALETTE) == 24
    cp.first_unused({PALETTE[0], PALETTE[1]})
    assert cp.color() == PALETTE[2]
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor", lambda *a, **k: QtGui.QColor("#123456"))
    cp._pick_custom()
    assert cp.color() == "#123456"
    cp.set_color(PALETTE[5])
    assert cp.color() == PALETTE[5]


def test_tray_menu_status_is_short():
    from catflap.platform.base import TunnelStatus
    from catflap.ui_common import INFO, short_status
    up = TunnelStatus(active=True)
    assert short_status(up, INFO + "Your current network uses the same addresses as X; …") == "Connected (workaround)"
    assert short_status(up, None) == "Connected"
    assert short_status(None, INFO + "You're at X right now …") == "You're here"
    assert short_status(None, "Site network … a very long error message …") == "Couldn't connect"
    assert short_status(None, None) == "Not connected"


def test_offer_wireguard_install(qapp, monkeypatch, no_popups):
    from catflap.platform.windows import WireGuardMissing
    from catflap.ui_common import offer_wireguard_install
    installed = []

    class B:
        def install_wireguard(self):
            installed.append(True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: QtWidgets.QMessageBox.Yes)
    assert offer_wireguard_install(None, WireGuardMissing("missing"), B())
    assert installed == [True]
    assert not offer_wireguard_install(None, RuntimeError("other"), B())     # other errors: no offer


def _pia(version="3.6"):
    from catflap import conflicts
    return conflicts.match_programs([{"name": "Private Internet Access", "version": version,
                                      "uninstall": "pia-uninstall.exe", "per_user": False}])


def _capture(app, monkeypatch, choose=None):
    seen = []

    def fake_exec(dlg):
        seen.append([f.key for f, *_ in dlg.rows])
        if choose:
            choose(dlg)
        return 1
    monkeypatch.setattr(app, "run_dialog", fake_exec)
    return seen


def test_scans_after_each_update_and_respects_dont_warn_per_version(qapp, manager, monkeypatch):
    app = tray.TrayApp(qapp, manager)
    app.timer.stop()
    seen = _capture(app, monkeypatch, lambda d: d.set_choice("pia", quiet=True))
    app.check_conflicting_apps(found=_pia())
    assert seen == [["pia"]] and app.settings["conflicts_ignored"] == {"pia": "3.6"}
    app.check_conflicting_apps()                          # same CatFlap version: no new scan
    assert len(seen) == 1
    app.settings["conflicts_scanned_version"] = "1.0"     # CatFlap was updated
    monkeypatch.setattr("catflap.conflicts.detect", lambda b, p: _pia())
    app.check_conflicting_apps()
    assert len(seen) == 1                                 # silenced at this version
    app.settings["conflicts_scanned_version"] = "1.0"
    monkeypatch.setattr("catflap.conflicts.detect", lambda b, p: _pia("3.7"))
    app.check_conflicting_apps()
    assert seen[-1] == ["pia"]                            # newer PIA: warned again


def test_manual_check_lists_everything_even_silenced(qapp, manager, monkeypatch):
    app = tray.TrayApp(qapp, manager)
    app.timer.stop()
    app.settings["conflicts_ignored"] = {"pia": "3.6"}
    seen = _capture(app, monkeypatch)
    monkeypatch.setattr(tray, "run_blocking", lambda parent, text, fn: fn())
    monkeypatch.setattr("catflap.conflicts.detect", lambda b, p: _pia())
    app.check_conflicting_apps(manual=True)
    assert seen == [["pia"]]
    assert app.settings["conflicts_ignored"] == {"pia": "3.6"}   # box was pre-ticked, kept


def test_uninstall_checkbox_runs_the_uninstaller(qapp, manager, monkeypatch):
    app = tray.TrayApp(qapp, manager)
    app.timer.stop()
    ran = []
    monkeypatch.setattr(tray, "run_blocking", lambda parent, text, fn: fn())
    monkeypatch.setattr("catflap.platform.windows.uninstall_program", lambda info, run: ran.append(info["name"]))
    _capture(app, monkeypatch, lambda d: d.set_choice("pia", uninstall=True))
    app.check_conflicting_apps(found=_pia())
    assert ran == ["Private Internet Access"]


def test_uninstalling_wireguard_app_downloads_engine_first(qapp, manager, monkeypatch):
    from catflap import conflicts
    app = tray.TrayApp(qapp, manager)
    app.timer.stop()
    order = []
    monkeypatch.setattr(tray, "run_blocking", lambda parent, text, fn: fn())
    monkeypatch.setattr("catflap.platform.windows.uninstall_program", lambda info, run: order.append("uninstall"))
    app.m.backend.install_wireguard = lambda: order.append("download")
    found = conflicts.match_programs([{"name": "WireGuard", "version": "1.1.1", "uninstall": "u"}])
    found[0].needs_engine = True
    _capture(app, monkeypatch, lambda d: d.set_choice("wireguard-app", uninstall=True))
    app.check_conflicting_apps(found=found)
    assert order == ["download", "uninstall"]


def test_old_dont_ask_settings_are_kept(qapp, manager, monkeypatch):
    from catflap import conflicts
    app = tray.TrayApp(qapp, manager)
    app.timer.stop()
    app.settings["wifiman_prompt"] = "never"
    app.settings["conflicts_ignored"] = ["pia"]
    seen = _capture(app, monkeypatch)
    wifiman = conflicts.match_programs([{"name": "WiFiman Desktop", "version": "1.2.8", "uninstall": "u"}])
    app.check_conflicting_apps(found=wifiman + _pia())
    assert not seen                                       # both silenced before versions were kept
    assert app.settings["conflicts_ignored"] == {"pia": "3.6", "wifiman": "1.2.8"}


def test_wireguard_app_row_offers_hiding_the_tray_icon(qapp):
    from catflap import conflicts
    from catflap.ui_conflicts import ConflictsDialog
    found = conflicts.match_programs([{"name": "WireGuard", "version": "1.1.1", "uninstall": "MsiExec /X{X}"}])
    assert found[0].conflict is conflicts.WIREGUARD_APP
    d = ConflictsDialog(found, {}, manual=True, windows=True)
    d.set_choice("wireguard-app", hide=True)
    assert [f.key for f in d.hide_tray()] == ["wireguard-app"] and not d.uninstall()
    d.set_choice("wireguard-app", uninstall=True)        # uninstall unticks hide
    assert not d.hide_tray() and [f.key for f in d.uninstall()] == ["wireguard-app"]
    assert d.ok.text() == "Uninstall 1 and close"


# ============================================================ AdGuard for Linux
class FakeAdGuard:
    """Stands in for catflap.adguard.AdGuard."""

    def __init__(self, state="on"):
        from catflap import adguard
        self.state = state
        self.calls = []
        self.host = adguard.Host(flatpak=False)
        self.fail_start = None

    update = None

    def status(self):
        from catflap.adguard import CliStatus
        return CliStatus(self.state, f"status: {self.state}", self.update)

    def argv(self, *args):
        return ["adguard-cli", *args]

    def path(self, refresh=False):
        return "/usr/local/bin/adguard-cli"

    def start(self):
        self.calls.append("start")
        if self.fail_start:
            raise self.fail_start
        self.state = "on"

    def stop(self):
        self.calls.append("stop")
        self.state = "off"


def test_icon_adguard_shield(qapp):
    plain = make_icon([]).pixmap(64, 64).toImage()
    on = make_icon([], adguard="on").pixmap(64, 64).toImage().pixelColor(5, 12)
    paused = make_icon([], adguard="paused").pixmap(64, 64).toImage().pixelColor(5, 12)
    off = make_icon([], adguard="off").pixmap(64, 64).toImage().pixelColor(5, 12)
    assert plain.pixelColor(5, 12).alpha() == 0                     # no shield without AdGuard
    assert on.green() > 120 and on.red() < 100                      # green shield
    assert paused.red() > 200 and paused.green() > 120 and paused.blue() < 80   # amber
    assert off.red() > 200 and off.green() > 200 and off.blue() > 200          # white
    assert make_icon([], adguard="missing").pixmap(64, 64).toImage() == plain
    dot = make_icon(["#ff0000"], adguard="on").pixmap(64, 64).toImage().pixelColor(51, 51)
    assert dot.green() > 150 and dot.red() < 100                    # VPN dot unchanged


def _labels(menu):
    return [a.text() for a in menu.actions()]


linux_only = pytest.mark.skipif(__import__("sys").platform == "win32", reason="adguard-cli is Linux only")


@linux_only
def test_tray_adguard_menu_pause_and_auto_resume(qapp, manager, monkeypatch):
    from catflap import adguard
    ag = FakeAdGuard("on")
    app = tray.TrayApp(qapp, manager, adguard_cli=ag)
    app.timer.stop()
    wait_for(qapp, lambda: app.ag_state == "on")
    labels = _labels(app.menu)
    assert "AdGuard Ad Blocking: Protecting" in labels and "Pause AdGuard" in labels
    assert "AdGuard: Protecting" in app.tray.toolTip()
    pause_menu = next(a.menu() for a in app.menu.actions() if a.text() == "Pause AdGuard")
    next(a for a in pause_menu.actions() if a.text() == "For 5 minutes").trigger()
    wait_for(qapp, lambda: not app.ag_busy)
    assert ag.calls == ["stop"] and app.ag_state == "paused"
    assert adguard.PAUSE_KEY in manager.store.settings()
    assert any(t.startswith("AdGuard Ad Blocking: Paused, 5 min left") for t in _labels(app.menu))
    assert "Resume AdGuard now" in _labels(app.menu)

    # Time's up: the next poll turns it back on.
    app.settings[adguard.PAUSE_KEY] = 1.0
    app.poll_adguard()
    wait_for(qapp, lambda: ag.calls == ["stop", "start"] and not app.ag_busy)
    assert app.ag_state == "on" and adguard.PAUSE_KEY not in app.settings


@linux_only
def test_tray_adguard_failed_auto_resume_gives_up(qapp, manager):
    from catflap import adguard
    ag = FakeAdGuard("off")
    ag.fail_start = adguard.NeedsTerminal(["adguard-cli", "start"], "sudo: a terminal is required")
    app = tray.TrayApp(qapp, manager, adguard_cli=ag)
    app.timer.stop()
    app.settings[adguard.PAUSE_KEY] = 1.0
    wait_for(qapp, lambda: not app._ag_polling)
    app.poll_adguard()
    wait_for(qapp, lambda: "start" in ag.calls and not app.ag_busy)
    wait_for(qapp, lambda: app.ag_state == "off")
    assert adguard.PAUSE_KEY not in app.settings                    # no retry loop
    assert ag.calls == ["start"]


@linux_only
def test_tray_adguard_toggle_and_hide_shield(qapp, manager):
    from catflap import adguard
    ag = FakeAdGuard("off")
    app = tray.TrayApp(qapp, manager, adguard_cli=ag)
    app.timer.stop()
    wait_for(qapp, lambda: app.ag_state == "off")
    toggle = next(a for a in app.menu.actions() if a.text().startswith("AdGuard Ad Blocking"))
    assert toggle.isCheckable() and not toggle.isChecked()
    toggle.trigger()
    wait_for(qapp, lambda: app.ag_state == "on" and not app.ag_busy)
    app.set_adguard_show(False)
    assert manager.store.settings()[adguard.SHOW_KEY] is False
    assert app._tray_icon().pixmap(64, 64).toImage().pixelColor(5, 12).alpha() == 0


@linux_only
def test_tray_without_adguard_has_no_section(qapp, manager):
    ag = FakeAdGuard("missing")
    app = tray.TrayApp(qapp, manager, adguard_cli=ag)
    app.timer.stop()
    wait_for(qapp, lambda: not app._ag_polling)
    assert not any("AdGuard" in t for t in _labels(app.menu))
    assert "AdGuard" not in app.tray.toolTip()


@linux_only
def test_tray_adguard_update_notice(qapp, manager, monkeypatch):
    from catflap import adguard
    ag = FakeAdGuard("on")
    ag.update = "1.5.1"
    opened = []
    monkeypatch.setattr(adguard, "open_terminal", lambda host, cmd, title="": opened.append(cmd) or True)
    shown = []
    app = tray.TrayApp(qapp, manager, adguard_cli=ag)
    app.timer.stop()
    monkeypatch.setattr(app.tray, "showMessage", lambda *a, **k: shown.append(a[1]))
    wait_for(qapp, lambda: not app._ag_polling)
    app.poll_adguard()
    wait_for(qapp, lambda: "Update AdGuard to 1.5.1…" in _labels(app.menu))
    assert app.settings["adguard_update_notified"] == "1.5.1"
    app.poll_adguard()
    wait_for(qapp, lambda: not app._ag_polling)
    assert len([m for m in shown if "1.5.1" in m]) <= 1              # told once per version
    next(a for a in app.menu.actions() if a.text() == "Update AdGuard to 1.5.1…").trigger()
    assert opened == ["adguard-cli stop && adguard-cli update ; adguard-cli start"]


@linux_only
def test_tray_adguard_update_stops_and_restarts_adguard(qapp, manager, monkeypatch):
    from catflap import adguard
    ag = FakeAdGuard("on")
    opened = []
    monkeypatch.setattr(adguard, "open_terminal", lambda host, cmd, title="": opened.append(cmd) or True)
    app = tray.TrayApp(qapp, manager, adguard_cli=ag)
    app.timer.stop()
    wait_for(qapp, lambda: app.ag_state == "on")
    app.adguard_update()
    assert opened == ["adguard-cli stop && adguard-cli update ; adguard-cli start"]


@linux_only
def test_flatpak_without_permission_asks_as_soon_as_adguard_is_seen(qapp, manager, monkeypatch):
    from catflap import adguard, ui_adguard
    ag = FakeAdGuard("no-access")
    monkeypatch.setattr(adguard, "detect_from_sandbox", lambda: True)
    shown = []

    class FakeDialog:
        def __init__(self, seen, declined, parent=None):
            shown.append(seen)
            self.declined, self.restart_now = False, True

        def exec(self):
            return 1
    monkeypatch.setattr(ui_adguard, "FlatpakPermissionDialog", FakeDialog)
    restarted = []
    monkeypatch.setattr(tray.TrayApp, "restart_app", lambda self, info=None: restarted.append(1))
    app = tray.TrayApp(qapp, manager, adguard_cli=ag)
    app.timer.stop()
    wait_for(qapp, lambda: shown and restarted)
    assert shown == [True] and app.adguard_needs_permission
    assert "⚠ Let CatFlap control AdGuard…" in _labels(app.menu)
    assert "needs permission" in app.tray.toolTip()
    red = app._tray_icon().pixmap(64, 64).toImage().pixelColor(13, 12)
    assert red.red() > 180 and red.green() < 100                     # red "!" shield
    app._ag_detect_at = 0
    app.poll_adguard()
    wait_for(qapp, lambda: not app._ag_polling)
    qapp.processEvents()
    assert shown == [True]                                           # once per start


@linux_only
def test_flatpak_permission_declined_stays_quiet(qapp, manager, monkeypatch):
    from catflap import adguard, ui_adguard
    manager.store.settings()["adguard_flatpak_declined"] = True
    monkeypatch.setattr(adguard, "detect_from_sandbox", lambda: True)
    monkeypatch.setattr(ui_adguard, "FlatpakPermissionDialog",
                        lambda *a, **k: pytest.fail("asked although declined"))
    app = tray.TrayApp(qapp, manager, adguard_cli=FakeAdGuard("no-access"))
    app.timer.stop()
    wait_for(qapp, lambda: app.ag_in_sandbox_seen)
    assert "⚠ Let CatFlap control AdGuard…" in _labels(app.menu)    # still reachable
    assert app._tray_icon().pixmap(64, 64).toImage().pixelColor(13, 12).alpha() == 0


def test_restarts_when_a_newer_version_is_installed(qapp, manager, monkeypatch):
    from catflap import update
    monkeypatch.setattr(tray.TrayApp, "INSTALL_CHECK_MS", 10_000_000)
    app = tray.TrayApp(qapp, manager, adguard_cli=FakeAdGuard("missing"))
    app.timer.stop()
    started, quits = [], []
    monkeypatch.setattr(update, "restart", lambda info=None: started.append(info))
    monkeypatch.setattr(app, "quit", lambda force=False: quits.append(force))
    monkeypatch.setattr(update, "installed_version", lambda info=None: tray.__version__)
    app.check_installed_version()
    wait_for(qapp, lambda: not app._install_checking)
    assert not started                                               # same version: nothing
    monkeypatch.setattr(update, "installed_version", lambda info=None: "99.0")
    monkeypatch.setattr(tray.QtCore.QTimer, "singleShot", lambda ms, fn: fn())
    app.check_installed_version()
    wait_for(qapp, lambda: started)
    assert quits == [True] and manager.store.settings()["pending_update"] == "99.0"


def test_update_check_is_fresh_and_opens_the_window_once_per_version(qapp, manager, monkeypatch):
    from catflap import update, ui_update
    from catflap.update import InstallInfo, ReleaseInfo, UpdateStatus
    calls, opened, shown = [], [], []

    def fake_check(settings, force=False, **kw):
        calls.append(force)
        st = UpdateStatus(current="1.9", install=InstallInfo("rpm"))
        st.latest = ReleaseInfo(version="2.0", url="u", notes="", assets={})
        st.available = True
        return st
    monkeypatch.setattr(update, "check", fake_check)

    class FakeDialog:
        restart_requested = False

        def __init__(self, st, parent, settings):
            opened.append(st.latest.version)

        def exec(self):
            return 0
    monkeypatch.setattr(ui_update, "UpdateDialog", FakeDialog)
    app = tray.TrayApp(qapp, manager, adguard_cli=FakeAdGuard("missing"))
    app.timer.stop()
    monkeypatch.setattr(app.tray, "showMessage", lambda *a, **k: shown.append(a[1]))
    app.check_updates(interactive=False)                       # e.g. at start-up
    wait_for(qapp, lambda: opened)
    assert calls == [True] and opened == ["2.0"]               # asked GitHub, not the cache
    assert manager.store.settings()["update_offered"] == "2.0"
    app._periodic_update_check()                               # hours later: same version
    wait_for(qapp, lambda: shown)
    assert opened == ["2.0"] and "2.0" in shown[0] and calls == [True, True]


def test_wizard_ignores_next_while_working(qapp, manager, no_popups):
    """A second click on Next while a step is still working must not run it again."""
    import threading
    from catflap import ui_common
    w = ui_wizards.SiteWizard(manager)
    w.show()
    nxt = w.button(QtWidgets.QWizard.NextButton)
    release = threading.Event()
    calls, seen = [], {}

    def slow():
        calls.append(1)
        release.wait(5)
        return "ok"

    def click_again():
        seen["busy"] = ui_common.is_busy(w)
        seen["next_enabled"] = nxt.isEnabled()
        seen["validated"] = w.validateCurrentPage()        # what Next would do
        nxt.click()                                         # a real second click
        release.set()

    from PySide6 import QtCore
    QtCore.QTimer.singleShot(50, click_again)
    assert ui_common.run_blocking(w.currentPage(), "Working…", slow) == "ok"
    assert calls == [1]
    assert seen == {"busy": True, "next_enabled": False, "validated": False}
    assert nxt.isEnabled() and not ui_common.is_busy(w)    # back to normal afterwards


def test_unifi_server_help_suggests_a_free_subnet(qapp, manager):
    from catflap.model import Network
    html = ui_wizards.unifi_server_help(manager, [Network("LAN", "10.250.1.0/24")])
    assert "Alternate Address for Clients" in html and "Manual" in html
    assert "10.250.2.1" in html                             # 10.250.1.x is the site's LAN


def test_file_page_warns_about_a_private_endpoint(qapp, manager, no_popups, unifi_conf, tmp_path,
                                                  monkeypatch):
    conf = tmp_path / "home.conf"
    conf.write_text(unifi_conf.read_text().replace("203.0.113.10:51820", "10.0.1.30:51821"))
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: asked.append(self.text()) or 0)
    w = ui_wizards.SiteWizard(manager)
    w.show()
    pick(w, "alta")
    fp = w.currentPage()
    fp.path.setText(str(conf))
    w.next()                                                # dialog "closed" without a choice
    assert asked and "10.0.1.30" in asked[0] and "UDP port 51821" in asked[0]
    fp.endpoint.setText("home.example.net")
    w.next()
    assert w.currentId() == ui_wizards.P_SITE_NETS and w.endpoint == "home.example.net"


def test_site_wizard_double_nat_asks_for_the_public_address(qapp, manager, no_popups, monkeypatch):
    """A UniFi gateway behind another router (e.g. a UDM Pro behind a UCG Fiber) reports a
    private WAN address; the site must be set up with the front router's public address."""
    from catflap.model import WanInfo
    from conftest import FakeDriver
    monkeypatch.setattr(FakeDriver, "get_wan_info", lambda self, site: WanInfo(wan_ip="10.0.1.30"))
    w = ui_wizards.SiteWizard(manager)
    w.gateway = "home-gw"
    w.setStartId(ui_wizards.P_SITE)
    w.restart()
    w.show()
    w.next()
    server = w.currentPage()
    wait_for(qapp, lambda: server.ok)
    assert not server.public_row.isHidden()
    assert "10.0.1.30" in server.text.toPlainText()
    server.public.setText("home.example.net")
    w.next()
    w.next()
    w.next()
    assert w.currentId() == ui_wizards.P_DONE, no_popups
    assert w.result.profile.endpoint_host == "home.example.net"


def test_friendly_timeout_blames_the_router_only_when_one_is_involved():
    from catflap.ui_common import friendly_error
    assert "router" not in friendly_error(TimeoutError()).lower()
    assert "router at 10.0.0.1" in friendly_error(TimeoutError(), "10.0.0.1")


def test_second_copy_shows_the_first_one_and_exits(qapp, manager, tmp_path, monkeypatch):
    """Starting CatFlap while it runs must not add a second tray icon."""
    import threading
    name = str(tmp_path / "tray.sock")
    first = tray.TrayApp(qapp, manager)
    shown = []
    monkeypatch.setattr(first, "show_window", lambda: shown.append(1))
    assert tray.claim_single_instance(first, name)

    answered = {}
    t = threading.Thread(target=lambda: answered.setdefault("show", tray.ask_running_instance(name, b"show")))
    t.start()
    wait_for(qapp, lambda: not t.is_alive() and shown)
    assert answered["show"] is True

    # Another copy can't claim it while the first runs...
    second = tray.TrayApp(qapp, manager)
    assert tray.claim_single_instance(second, name) is False
    # ...but can once the first has quit (a restart), and nothing answers then.
    monkeypatch.setattr(first.qapp, "quit", lambda: None)
    first.quit()
    assert tray.ask_running_instance(name, b"ping", timeout_ms=200) is False
    assert tray.claim_single_instance(second, name) is True
    second.instance_server.close()


def test_tray_check_for_updates_says_when_it_fails(qapp, manager, no_popups):
    app = tray.TrayApp(qapp, manager)
    app.timer.stop()
    app._update_checked(None, OSError("network is unreachable"), interactive=True)
    assert no_popups and no_popups[-1][0] == "warning" and "Couldn't check for updates" in no_popups[-1][1]
    shown = len(no_popups)
    app._update_checked(None, OSError("x"), interactive=False)   # background checks stay quiet
    assert len(no_popups) == shown
