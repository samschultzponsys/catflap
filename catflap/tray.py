"""CatFlap window and tray app (Qt / PySide6).

KDE Plasma on Wayland shows QSystemTrayIcon through StatusNotifierItem;
Windows uses the notification area.

* First start: a wizard asks for Basic or Advanced (with live previews) and
  offers to add the first site with a step-by-step guide.
* Basic view: one big card per site with a Connect button.
* Advanced view: sites and gateways tables, details, checks and every action.
* The window minimizes to the tray; closing asks "keep running in the tray or
  quit" (with "remember my choice").
* Tray icon: the cat is a grey outline when nothing is connected, and turns
  the site's color when connected (striped for several sites); red badge after
  a failure.
* Notifications, start-at-login, update check at start-up and every 6 hours, with install-aware
  update instructions.
* Linux with AdGuard for Linux (adguard-cli) installed: an AdGuard section in
  the tray menu (on/off, pause, HTTPS filtering check) and a shield in the
  icon's top-left corner (green on, amber paused, white off).

All real work happens in :class:`catflap.core.Manager` on worker threads.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from . import adguard, autostart, checks, update
from .core import Manager
from .drivers import DRIVERS
from .model import Profile
from .ui_common import (GREY, INFO, ColorDialog, app_icon, offer_wireguard_install, short_status, QtCore, QtGui, QtWidgets, dot_icon, friendly_error, make_icon, require_qt, run_bg, run_blocking, shield_icon)
from .version import __version__

__all__ = ["MainWindow", "TrayApp", "make_icon", "main"]


# ============================================================ small dialogs
class EditSiteDialog(QtWidgets.QDialog if QtWidgets else object):
    def __init__(self, parent, p: Profile):
        super().__init__(parent)
        self.setWindowTitle(f"Edit {p.display}")
        f = QtWidgets.QFormLayout(self)
        self.label = QtWidgets.QLineEdit(p.display)
        self.endpoint = QtWidgets.QLineEdit(p.endpoint_host)
        self.routes = QtWidgets.QLineEdit(", ".join(p.routes))
        self.dns_domain = QtWidgets.QLineEdit(p.dns_domain or "")
        self.dns_domain.setPlaceholderText("e.g. office.lan (optional)")
        self.dns_server = QtWidgets.QLineEdit(", ".join(p.dns_servers))
        self.dns_server.setPlaceholderText("DNS server inside the site, e.g. 192.168.1.1")
        f.addRow("Name", self.label)
        f.addRow("Site's internet address / DDNS name", self.endpoint)
        f.addRow("Networks to reach (comma separated)", self.routes)
        f.addRow("Internal domain (split DNS)", self.dns_domain)
        f.addRow("DNS server for that domain", self.dns_server)
        f.addRow(QtWidgets.QLabel("<small>Only names ending in the internal domain are looked "
                                  "up through the VPN; everything else uses your normal DNS."
                                  "</small>"))
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


# ============================================================ main window
class MainWindow(QtWidgets.QMainWindow if QtWidgets else object):
    def __init__(self, app: "TrayApp"):
        super().__init__()
        from .ui_views import AdvancedView, BasicView
        self.app = app
        self.setWindowTitle("CatFlap")
        self.setWindowIcon(app_icon())
        self.resize(820, 560)
        self.stack = QtWidgets.QStackedWidget()
        self.basic = BasicView()
        self.advanced = AdvancedView()
        self.stack.addWidget(self.basic)
        self.stack.addWidget(self.advanced)
        self.setCentralWidget(self.stack)
        for v in (self.basic, self.advanced):
            v.toggleRequested.connect(self.app.toggle)
            v.addSiteRequested.connect(self.app.add_site)
        self.basic.switchRequested.connect(lambda: self.set_mode("advanced"))
        self.advanced.switchRequested.connect(lambda: self.set_mode("basic"))
        self.advanced.addGatewayRequested.connect(self.app.add_gateway)
        self.advanced.action.connect(self.on_action)

        mb = self.menuBar()
        fm = mb.addMenu("&File")
        fm.addAction("Add a site…", self.app.add_site)
        fm.addAction("Add a gateway…", self.app.add_gateway)
        fm.addSeparator()
        fm.addAction("Export everything (to move to another computer)…", self.export)
        fm.addAction("Import from another computer…", self.import_bundle)
        fm.addSeparator()
        fm.addAction("Hide to tray", self.hide)
        fm.addAction("Quit", self.app.quit)
        vm = mb.addMenu("&View")
        grp = QtGui.QActionGroup(self)
        self.a_basic = vm.addAction("Basic")
        self.a_adv = vm.addAction("Advanced")
        for a, mode in ((self.a_basic, "basic"), (self.a_adv, "advanced")):
            a.setCheckable(True)
            grp.addAction(a)
            a.triggered.connect(lambda _=False, mo=mode: self.set_mode(mo))
        sm = mb.addMenu("&Settings")
        self.a_autostart = sm.addAction("Start CatFlap when I log in")
        self.a_autostart.setCheckable(True)
        self.a_autostart.setChecked(autostart.is_enabled())
        self.a_autostart.toggled.connect(self.set_autostart)
        self.a_updates = sm.addAction("Check for updates automatically")
        self.a_updates.setCheckable(True)
        self.a_updates.setChecked(self.app.settings.get("update_check", True))
        self.a_updates.toggled.connect(lambda v: self.app.set_setting("update_check", v))
        try:
            lists_sites = getattr(self.app.m.backend, "os_vpn_list", False)
        except Exception:  # noqa: BLE001 - no backend here: nothing to offer
            lists_sites = False
        if lists_sites:
            self.a_keep_entries = sm.addAction("Keep sites in the system's VPN list")
            self.a_keep_entries.setCheckable(True)
            self.a_keep_entries.setChecked(self.app.m.keeps_os_entries())
            self.a_keep_entries.setToolTip(
                "On: every site is listed in your system's network/VPN menu all the time.\n"
                "Off: a site is listed there only while it's connected.")
            self.a_keep_entries.toggled.connect(self.app.set_keep_os_entries)
        sm.addAction("Ask again what the close button does",
                     lambda: self.app.set_setting("close_behavior", "ask"))
        sm.addAction("Check for other VPN software…",
                     lambda: self.app.check_conflicting_apps(manual=True))
        if sys.platform != "win32":
            am = sm.addMenu("AdGuard Ad Blocking")
            self.a_adguard_show = am.addAction("Show AdGuard's status on the tray icon")
            self.a_adguard_show.setCheckable(True)
            self.a_adguard_show.setChecked(self.app.settings.get(adguard.SHOW_KEY, True))
            self.a_adguard_show.toggled.connect(self.app.set_adguard_show)
            am.addAction("Check HTTPS filtering…", self.app.adguard_https_check)
            am.addAction("Exclude an app from HTTPS filtering…", self.app.adguard_exclude)
            if adguard.in_flatpak():
                am.addAction("Let CatFlap control AdGuard (Flatpak)…", self.app.adguard_flatpak_help)
        tm = sm.addMenu("Tray icon when connected")
        tgrp = QtGui.QActionGroup(self)
        current = self.app.settings.get("tray_style", "site")
        for style, label in (("site", "Site colors (blend when several are connected)"),
                             ("cycle", "Cycle through site colors"),
                             ("blue", "Blue")):
            a = tm.addAction(label)
            a.setCheckable(True)
            a.setChecked(style == current)
            tgrp.addAction(a)
            a.triggered.connect(lambda _=False, st=style: self.app.set_tray_style(st))
        hm = mb.addMenu("&Help")
        hm.addAction("Setup guide…", self.app.first_run)
        hm.addAction("Check for updates…", lambda: self.app.check_updates(interactive=True))
        hm.addAction("About CatFlap", self.about)
        self.set_mode(self.app.settings.get("ui_mode", "basic"), save=False)

    # -- view
    def set_mode(self, mode: str, save: bool = True) -> None:
        mode = "advanced" if mode == "advanced" else "basic"
        self.stack.setCurrentWidget(self.advanced if mode == "advanced" else self.basic)
        (self.a_adv if mode == "advanced" else self.a_basic).setChecked(True)
        if save:
            self.app.set_setting("ui_mode", mode)
        self.refresh()

    def refresh(self) -> None:
        a = self.app
        self.stack.currentWidget().update_data(a.profiles, a.status, a.errors, a.busy,
                                               gateways=a.m.store.gateways(), checks=a.checks)

    # -- advanced actions
    def on_action(self, act: str, name: str) -> None:
        a, m = self.app, self.app.m
        try:
            if act == "check":
                a.checks[name] = run_blocking(self, "Running checks…", lambda: m.preflight(name))
                self.refresh()
            elif act == "color":
                p = m.store.profile(name)
                dlg = ColorDialog(self, f"Color for {p.display}", p.color or "")
                if a.run_dialog(dlg):
                    m.update_profile(name, color=dlg.picker.color())
                    a.poll()
            elif act == "edit":
                self.edit_site(name)
            elif act == "apply":
                run_blocking(self, "Re-applying safe settings…", lambda: m.apply(name))
                QtWidgets.QMessageBox.information(self, "CatFlap", "Settings re-applied.")
            elif act == "remove":
                self.remove_site(name)
            elif act == "gw-key":
                key, ok = QtWidgets.QInputDialog.getText(self, "Change API key",
                                                         f"New API key for '{name}':",
                                                         QtWidgets.QLineEdit.Password)
                if ok and key.strip():
                    m.set_gateway_secret(name, "api_key", key.strip())
                    QtWidgets.QMessageBox.information(self, "CatFlap", "API key updated.")
            elif act == "gw-remove":
                if QtWidgets.QMessageBox.question(
                        self, "Remove gateway", f"Remove gateway '{name}' and its saved API key "
                        "from this computer?") == QtWidgets.QMessageBox.Yes:
                    m.remove_gateway(name)
                    a.poll()
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "CatFlap", friendly_error(e))

    def edit_site(self, name: str) -> None:
        m = self.app.m
        p = m.store.profile(name)
        dlg = EditSiteDialog(self, p)
        if not self.app.run_dialog(dlg):
            return
        routes = [r.strip() for r in dlg.routes.text().split(",") if r.strip()]
        dom = dlg.dns_domain.text().strip()
        servers = [s.strip() for s in dlg.dns_server.text().split(",") if s.strip()]
        endpoint = dlg.endpoint.text().strip()
        kw: dict[str, Any] = {"label": dlg.label.text().strip() or p.display}
        if endpoint and endpoint != p.endpoint_host:
            kw["endpoint"] = endpoint
        if routes != p.routes:
            kw["routes"] = routes
        if dom != (p.dns_domain or "") or servers != p.dns_servers:
            if dom:
                kw.update(dns_domain=dom, dns_servers=servers or None)
            else:
                kw["clear_dns"] = True
        try:
            run_blocking(self, "Saving…", lambda: m.update_profile(name, **kw))
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "CatFlap", friendly_error(e))
        self.app.poll()

    def remove_site(self, name: str) -> None:
        m = self.app.m
        p = m.store.profile(name)
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Remove site")
        box.setText(f"Remove <b>{p.display}</b> from this computer?")
        revoke = None
        if p.gateway and p.client_id:
            box.setInformativeText("You can also delete this computer's access on the router, so "
                                   "the key on this computer stops working.")
            revoke = box.addButton("Remove and delete on router", QtWidgets.QMessageBox.DestructiveRole)
        local = box.addButton("Remove from this computer", QtWidgets.QMessageBox.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() not in (revoke, local):
            return
        do_revoke = box.clickedButton() is revoke
        try:
            run_blocking(self, "Removing…", lambda: m.remove(name, revoke=do_revoke))
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "CatFlap", friendly_error(e))
        self.app.poll()

    # -- settings / files
    def set_autostart(self, v: bool) -> None:
        try:
            autostart.set_enabled(v)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "CatFlap", str(e))

    def _ask_pass(self, title: str, confirm: bool) -> str | None:
        pw, ok = QtWidgets.QInputDialog.getText(
            self, title, "Passphrase (at least 8 characters). You'll need it on the other "
                         "computer:", QtWidgets.QLineEdit.Password)
        if not ok:
            return None
        if confirm:
            again, ok = QtWidgets.QInputDialog.getText(self, title, "Type it again:",
                                                       QtWidgets.QLineEdit.Password)
            if not ok or again != pw:
                QtWidgets.QMessageBox.warning(self, "CatFlap", "The passphrases don't match.")
                return None
        return pw

    def export(self) -> None:
        QtWidgets.QMessageBox.information(
            self, "Export everything",
            "This saves your routers, API keys, sites and their keys into one file, locked "
            "with a passphrase, so you can set up another computer quickly.\n\n"
            "Don't use the same site on both computers at the same time; for a second "
            "computer it's best to add the site again there.")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export CatFlap", os.path.expanduser("~/Downloads/catflap-export.json"))
        if not path:
            return
        pw = self._ask_pass("Lock the export", confirm=True)
        if pw is None:
            return
        try:
            text = self.app.m.export(pw)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            QtWidgets.QMessageBox.information(self, "CatFlap", f"Saved to {path}.")
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "CatFlap", friendly_error(e))

    def import_bundle(self) -> None:
        from .bundle import is_encrypted
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import CatFlap export", os.path.expanduser("~/Downloads"),
            "CatFlap export (*.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            pw = self._ask_pass("Unlock the export", confirm=False) if is_encrypted(text) else None
            plan = run_blocking(self, "Reading the export…", lambda: self.app.m.preview_bundle(text, pw))
            choice = "keep"
            clashes = plan["conflict_sites"] + plan["conflict_gateways"]
            if clashes:
                box = QtWidgets.QMessageBox(self)
                box.setWindowTitle("Merge with what's here?")
                box.setText("These already exist on this computer with different settings:\n\n  "
                            + "\n  ".join(clashes))
                box.setInformativeText(
                    f"{len(plan['new_sites'])} new site(s) and {len(plan['new_gateways'])} new "
                    "gateway(s) will be added either way; identical ones are left alone.")
                keep = box.addButton("Keep mine", QtWidgets.QMessageBox.AcceptRole)
                replace = box.addButton("Replace with imported", QtWidgets.QMessageBox.DestructiveRole)
                both = box.addButton("Keep both", QtWidgets.QMessageBox.ActionRole)
                box.addButton(QtWidgets.QMessageBox.Cancel)
                box.setDefaultButton(keep)
                self.app.run_dialog(box)
                clicked = box.clickedButton()
                if clicked not in (keep, replace, both):
                    return
                choice = "replace" if clicked is replace else "both" if clicked is both else "keep"
            msgs = run_blocking(self, "Importing…",
                                lambda: self.app.m.import_bundle(text, pw, on_conflict=choice))
            QtWidgets.QMessageBox.information(self, "CatFlap", "\n".join(msgs))
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "CatFlap", friendly_error(e))
        self.app.poll()

    def about(self) -> None:
        info = update.detect_install()
        QtWidgets.QMessageBox.about(
            self, "About CatFlap",
            f"<b>CatFlap {__version__}</b><br>Safe WireGuard remote access to your sites.<br><br>"
            f"Installed via: <b>{info.method}</b> {info.detail}<br>"
            f"Licensed under the Apache License 2.0; see NOTICE.<br>"
            f"<a href='https://github.com/{update.REPO}'>github.com/{update.REPO}</a>")

    # -- window behaviour
    def closeEvent(self, ev) -> None:  # noqa: N802 (Qt API)
        if self.app.quitting:
            ev.accept()
            return
        behavior = self.app.settings.get("close_behavior", "ask")
        if behavior == "ask":
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("Close CatFlap?")
            box.setText("Keep CatFlap running in the system tray, or quit completely?")
            box.setInformativeText("Connected sites stay connected either way.")
            tray_btn = box.addButton("Keep running in tray", QtWidgets.QMessageBox.AcceptRole)
            quit_btn = box.addButton("Quit CatFlap", QtWidgets.QMessageBox.DestructiveRole)
            box.addButton(QtWidgets.QMessageBox.Cancel)
            remember = QtWidgets.QCheckBox("Remember my choice")
            box.setCheckBox(remember)
            box.setDefaultButton(tray_btn)
            box.exec()
            if box.clickedButton() is tray_btn:
                behavior = "tray"
            elif box.clickedButton() is quit_btn:
                behavior = "quit"
            else:
                ev.ignore()
                return
            if remember.isChecked():
                self.app.set_setting("close_behavior", behavior)
        if behavior == "quit":
            ev.accept()
            self.app.quit()
        else:
            ev.ignore()
            self.hide()

    def changeEvent(self, ev) -> None:  # noqa: N802 (Qt API)
        # Minimizing sends the window to the tray instead of the task bar.
        if ev.type() == QtCore.QEvent.WindowStateChange and self.isMinimized():
            QtCore.QTimer.singleShot(0, self.hide)
        super().changeEvent(ev)


# ============================================================ tray app
class TrayApp(QtCore.QObject if QtCore else object):
    POLL_MS = 5000
    UPDATE_CHECK_MS = 6 * 3600 * 1000   # while running, look for a new release every 6 hours
    INSTALL_CHECK_MS = 60_000      # look for a newer installed CatFlap every minute
    SANDBOX_DETECT_S = 300         # Flatpak without permission: look for AdGuard every 5 min
    CYCLE_MS = 100          # tray color cycling: 100 ms steps,
    CYCLE_STEP = 0.04       # 2.5 s per site color

    def __init__(self, qapp: "QtWidgets.QApplication", manager: Manager | None = None,
                 adguard_cli: "adguard.AdGuard | None" = None):
        super().__init__()
        self.qapp = qapp
        self.m = manager or Manager()
        self.ag = adguard_cli or adguard.AdGuard()
        self.ag_state = adguard.MISSING
        self.ag_text = ""
        self.ag_busy = False
        self._ag_polling = False
        self.ag_update: str | None = None
        self.ag_in_sandbox_seen = False     # Flatpak: AdGuard runs, but we may not control it
        self._ag_detect_at = 0.0
        self._ag_asked = False
        self.settings = self.m.store.settings()
        self.quitting = False
        self.instance_server = None         # see claim_single_instance()
        self.paused = 0
        self.profiles: dict[str, Profile] = self.m.store.profiles()
        self.status: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        self.busy: set[str] = set()
        self.checks: dict[str, list] = {}
        self.update_status: update.UpdateStatus | None = None

        self.tray = QtWidgets.QSystemTrayIcon(make_icon([]), self)
        self.tray.setToolTip("CatFlap: not connected")
        self.menu = QtWidgets.QMenu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self.tray.messageClicked.connect(self._message_clicked)
        self.window = MainWindow(self)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(self.POLL_MS)
        self._phase = 0.0
        self._cycle = QtCore.QTimer(self)        # tray color cycling (style "cycle")
        self._cycle.timeout.connect(self._cycle_tick)
        self._render()
        self.poll()
        if not self.m.keeps_os_entries():
            # Sites disconnected while CatFlap wasn't running (or from the system's menu).
            run_bg(self.m.tidy_os_entries, lambda r, e: None)
        if self.settings.get("update_check", True):
            # Every start asks GitHub afresh (not yesterday's answer), then every few hours.
            QtCore.QTimer.singleShot(3000, lambda: self.check_updates(interactive=False))
        self._update_timer = QtCore.QTimer(self)
        self._update_timer.timeout.connect(self._periodic_update_check)
        self._update_timer.start(self.UPDATE_CHECK_MS)
        # Restart by ourselves when a newer CatFlap gets installed while we run
        # (from the update window, a terminal, Discover or dnf).
        self._restarting = False
        self._install_checking = False
        self._install_timer = QtCore.QTimer(self)
        self._install_timer.timeout.connect(self.check_installed_version)
        self._install_timer.start(self.INSTALL_CHECK_MS)

    # -- settings
    def set_keep_os_entries(self, keep: bool) -> None:
        def done(problems, err):
            self.profiles = self.m.store.profiles()
            if err or problems:
                QtWidgets.QMessageBox.warning(
                    self.window, "CatFlap", "Couldn't update every site in the system's VPN list:\n\n"
                    + (friendly_error(err) if err else "\n".join(problems)))
            self.poll()
        run_bg(lambda: self.m.set_keep_os_entries(keep), done)

    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self.m.store.save()

    # -- dialogs pause polling so the store isn't reloaded under them
    def run_dialog(self, dlg) -> bool:
        self.paused += 1
        try:
            return bool(dlg.exec())
        finally:
            self.paused -= 1
            self.poll()

    # -- polling
    def poll(self) -> None:
        if self.paused:
            return
        self.poll_adguard()
        m = self.m

        def work():
            m.store.reload()   # the CLI may have changed things
            profiles = m.store.profiles()
            st = {}
            for n, p in profiles.items():
                try:
                    st[n] = m.backend.status(p)
                except Exception:  # noqa: BLE001
                    st[n] = None
            if not m.keeps_os_entries() and any(
                    p.backend_id and s is not None and not s.active
                    for p, s in ((profiles[n], st[n]) for n in profiles)):
                m.tidy_os_entries()          # e.g. disconnected from the system's own menu
                profiles = m.store.profiles()
            return profiles, st
        run_bg(work, self._polled)

    def _polled(self, res, err) -> None:
        if err or res is None:
            return
        self.profiles, self.status = res
        self._render()

    def _tray_icon(self) -> "QtGui.QIcon":
        active = [n for n, s in self.status.items() if s and s.active and n in self.profiles]
        colors = [self.profiles[n].color or "#22C55E" for n in sorted(active)]
        failed = any(not e.startswith(INFO) for e in self.errors.values())
        style = self.settings.get("tray_style", "site")
        shield = self.ag_state if self.settings.get(adguard.SHOW_KEY, True) else None
        if self.adguard_needs_permission and not self.settings.get("adguard_flatpak_declined"):
            shield = "attention"
        return make_icon(colors, error=failed and not active, style=style, phase=self._phase,
                         adguard=shield)

    def _cycle_tick(self) -> None:
        """Fade the tray icon through the connected sites' colors (style "cycle")."""
        n = sum(1 for s in self.status.values() if s and s.active)
        if self.settings.get("tray_style", "site") != "cycle" or n < 2:
            self._cycle.stop()
            return
        self._phase = (self._phase + self.CYCLE_STEP) % n
        self.tray.setIcon(self._tray_icon())

    def _update_cycle_timer(self) -> None:
        n = sum(1 for s in self.status.values() if s and s.active)
        want = self.settings.get("tray_style", "site") == "cycle" and n >= 2
        if want and not self._cycle.isActive():
            self._cycle.start(self.CYCLE_MS)
        elif not want and self._cycle.isActive():
            self._cycle.stop()

    def set_tray_style(self, style: str) -> None:
        self.set_setting("tray_style", style)
        self._phase = 0.0
        self._render()

    def _render(self) -> None:
        active = [n for n, s in self.status.items() if s and s.active and n in self.profiles]
        self.tray.setIcon(self._tray_icon())
        self._update_cycle_timer()
        names = [self.profiles[n].display for n in sorted(active)]
        tip = "CatFlap: " + (", ".join(names) + " connected" if names else "not connected")
        if self.adguard_shown:
            tip += "\nAdGuard: " + adguard.describe(self.ag_state, self.settings)
        elif self.adguard_needs_permission:
            tip += "\nAdGuard: CatFlap needs permission to control it"
        self.tray.setToolTip(tip)
        self._build_menu()
        if self.window.isVisible():
            self.window.refresh()

    def _build_menu(self) -> None:
        m = self.menu
        m.clear()
        groups: dict[str, list[Profile]] = {}
        for p in self.profiles.values():
            vendor = DRIVERS[p.vendor].display_name if p.vendor in DRIVERS else p.vendor
            groups.setdefault(f"{p.gateway or 'Imported'} · {vendor}", []).append(p)
        for g in sorted(groups):
            if len(groups) > 1:
                m.addSection(g)
            for p in sorted(groups[g], key=lambda x: x.display.lower()):
                st = self.status.get(p.name)
                active = bool(st and st.active)
                text = short_status(st, self.errors.get(p.name), p.name in self.busy)
                verb = "Disconnect" if active else "Connect"
                a = m.addAction(dot_icon(p.color or GREY, active), f"{p.display}: {text}  ({verb})")
                a.setEnabled(p.name not in self.busy)
                a.triggered.connect(lambda _=False, n=p.name: self.toggle(n))
        if groups:
            m.addSeparator()
        if self.adguard_shown:
            self._build_adguard_menu(m)
            m.addSeparator()
        elif self.adguard_needs_permission:
            m.addAction(shield_icon("attention"), "⚠ Let CatFlap control AdGuard…",
                        self.adguard_flatpak_help)
            m.addSeparator()
        m.addAction("Add a site…", self.add_site)
        m.addAction("Open CatFlap", self.show_window)
        if self.update_status and self.update_status.available:
            m.addAction(f"Update to {self.update_status.latest.version}…",
                        lambda: self.check_updates(interactive=True))
        else:
            m.addAction("Check for updates…", lambda: self.check_updates(interactive=True))
        m.addSeparator()
        m.addAction("Quit", self.quit)

    # -- AdGuard for Linux
    @property
    def adguard_shown(self) -> bool:
        """AdGuard is installed and CatFlap can reach it."""
        return self.ag_state not in (adguard.MISSING, adguard.NO_ACCESS)

    @property
    def adguard_needs_permission(self) -> bool:
        """Flatpak: AdGuard is running here but the sandbox can't control it yet."""
        return self.ag_state == adguard.NO_ACCESS and self.ag_in_sandbox_seen

    def _detect_in_sandbox(self) -> None:
        import time
        if time.monotonic() < self._ag_detect_at:
            return
        self._ag_detect_at = time.monotonic() + self.SANDBOX_DETECT_S
        run_bg(adguard.detect_from_sandbox, self._detected_in_sandbox)

    def _detected_in_sandbox(self, seen, err) -> None:
        if err or not seen or self.ag_in_sandbox_seen:
            return
        self.ag_in_sandbox_seen = True
        self._render()
        if not self._ag_asked and not self.settings.get("adguard_flatpak_declined"):
            self._ag_asked = True               # once per start, until it's allowed
            QtCore.QTimer.singleShot(0, self.adguard_flatpak_help)

    def poll_adguard(self) -> None:
        if self._ag_polling or self.ag_busy or sys.platform == "win32":
            return
        self._ag_polling = True
        run_bg(self.ag.status, self._adguard_polled)

    def _adguard_polled(self, st, err) -> None:
        self._ag_polling = False
        if err or st is None:
            return
        self.ag_text = st.text
        if st.state == adguard.NO_ACCESS:
            self._detect_in_sandbox()
        if st.update != self.ag_update:
            self.ag_update = st.update
            self._build_menu()
        if st.update and self.settings.get("adguard_update_notified") != st.update:
            self.set_setting("adguard_update_notified", st.update)
            self.tray.showMessage("AdGuard update", f"AdGuard {st.update} is available. Tray menu → "
                                  f"Update AdGuard to {st.update}…",
                                  QtWidgets.QSystemTrayIcon.Information, 8000)
        state = adguard.effective_state(st.state, self.settings)
        if state == adguard.PAUSED and adguard.pause_due(self.settings):
            self.adguard_do("resume", notify="AdGuard protection is back on.")
        if state != self.ag_state:
            self.ag_state = state
            self._render()

    def set_adguard_show(self, on: bool) -> None:
        self.set_setting(adguard.SHOW_KEY, bool(on))
        self._render()

    def _build_adguard_menu(self, m) -> None:
        st = self.ag_state
        label = "working…" if self.ag_busy else adguard.describe(st, self.settings)
        a = m.addAction(shield_icon(st), f"AdGuard Ad Blocking: {label}")
        a.setCheckable(True)
        a.setChecked(st == adguard.ON)
        a.setEnabled(not self.ag_busy)
        if self.ag_text:
            a.setToolTip(self.ag_text)
        a.triggered.connect(lambda on: self.adguard_do("on" if on else "off"))
        if st == adguard.ON:
            pm = m.addMenu("Pause AdGuard")
            pm.setEnabled(not self.ag_busy)
            for mins, text in ((5, "For 5 minutes"), (30, "For 30 minutes"), (60, "For 1 hour"),
                               (None, "Until I resume")):
                pm.addAction(text, lambda mi=mins: self.adguard_do("pause", minutes=mi))
        elif st == adguard.PAUSED:
            r = m.addAction("Resume AdGuard now", lambda: self.adguard_do("resume"))
            r.setEnabled(not self.ag_busy)
        if self.ag_update:
            m.addAction(f"Update AdGuard to {self.ag_update}…", self.adguard_update)
        more = m.addMenu("AdGuard")
        more.addAction("Check HTTPS filtering…", self.adguard_https_check)
        more.addAction("Exclude an app from HTTPS filtering…", self.adguard_exclude)
        more.addSeparator()
        more.addAction("Show filter lists", lambda: self.adguard_output("Filter lists", self.ag.filters_list))
        more.addAction("Check for AdGuard and filter updates",
                       lambda: self.adguard_output("AdGuard updates", self.ag.check_update))
        more.addAction("Connect the AdGuard browser extension",
                       lambda: self.adguard_output("Browser extension", self.ag.install_browser_integration))
        more.addAction("License information",
                       lambda: self.adguard_output("License", self.ag.license))
        more.addSeparator()
        more.addAction("Set up the HTTPS filtering certificate…",
                       lambda: self._adguard_terminal(self.ag.argv("cert")))
        more.addAction("Start AdGuard when the computer starts…",
                       lambda: self._adguard_terminal(self.ag.argv("install-service")))
        more.addAction("Update AdGuard…", self.adguard_update)
        more.addAction("Run AdGuard setup in a terminal…",
                       lambda: self._adguard_terminal(self.ag.argv("configure")))
        more.addSeparator()
        show = more.addAction("Show status on the tray icon")
        show.setCheckable(True)
        show.setChecked(self.settings.get(adguard.SHOW_KEY, True))
        show.toggled.connect(self.set_adguard_show)

    def adguard_do(self, what: str, minutes: int | None = None, notify: str = "") -> None:
        """Turn AdGuard on/off, pause or resume it, in the background."""
        if self.ag_busy:
            return
        ag = self.ag
        # Worker threads get a copy; the pause is recorded on the UI thread when it worked.
        settings = {k: v for k, v in self.settings.items() if k == adguard.PAUSE_KEY}

        def work():
            if what == "on":
                adguard.resume(ag, settings)
            elif what == "off":
                adguard.turn_off(ag, settings)
            elif what == "pause":
                adguard.pause(ag, settings, None if minutes is None else minutes * 60)
            elif what == "resume":
                adguard.resume(ag, settings)
            return ag.status()

        def done(st, err):
            self.ag_busy = False
            if err and notify:
                # The automatic end of a pause failed: don't retry (and ask) every 5 seconds.
                self.settings.pop(adguard.PAUSE_KEY, None)
                self.m.store.save()
                self.tray.showMessage("AdGuard", "Couldn't turn AdGuard back on after the pause: "
                                      + friendly_error(err) + " Use the tray menu to turn it on.",
                                      QtWidgets.QSystemTrayIcon.Warning)
            elif isinstance(err, adguard.NeedsTerminal):
                self._adguard_needs_terminal(err)
            elif err:
                self.tray.showMessage("AdGuard", friendly_error(err), QtWidgets.QSystemTrayIcon.Warning)
            else:
                if adguard.PAUSE_KEY in settings:
                    self.settings[adguard.PAUSE_KEY] = settings[adguard.PAUSE_KEY]
                else:
                    self.settings.pop(adguard.PAUSE_KEY, None)
                msg = notify or {
                    "on": "AdGuard protection is on.", "off": "AdGuard protection is off.",
                    "resume": "AdGuard protection is back on.",
                    "pause": ("AdGuard is paused until you resume it." if minutes is None else
                              f"AdGuard is paused for {minutes} minutes."),
                }[what]
                self.tray.showMessage("AdGuard", msg, QtWidgets.QSystemTrayIcon.Information, 3000)
            self.m.store.save()
            if st is not None:
                self.ag_text = st.text
                self.ag_state = adguard.effective_state(st.state, self.settings)
            self._render()
            if err:
                self.poll_adguard()             # show what state it was left in

        self.ag_busy = True
        self._render()
        run_bg(work, done)

    def _adguard_needs_terminal(self, err: "adguard.NeedsTerminal") -> None:
        box = QtWidgets.QMessageBox(self.window)
        box.setWindowTitle("AdGuard")
        box.setText("AdGuard needs your password for this, and asks for it in a terminal.")
        box.setInformativeText("Open a terminal to run it? Tip: installing ksshaskpass (KDE) "
                               "lets it ask in a window instead.")
        go = box.addButton("Open a terminal", QtWidgets.QMessageBox.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        self.run_dialog(box)
        if box.clickedButton() is go:
            self._adguard_terminal(err.argv)

    def _adguard_terminal(self, argv: list[str]) -> None:
        if not adguard.open_terminal(self.ag.host, argv):
            QtWidgets.QMessageBox.information(self.window, "AdGuard", "No terminal app found. Run "
                                              "this in a terminal:\n\n" + " ".join(argv))

    def adguard_update(self) -> None:
        """``adguard-cli update`` in a terminal (it may ask for a password and shows
        progress). It refuses while the proxy runs, so stop it first and start it after."""
        running = self.ag_state == adguard.ON
        if not adguard.open_terminal(self.ag.host, adguard.update_script(self.ag, running),
                                     "AdGuard: update"):
            QtWidgets.QMessageBox.information(self.window, "AdGuard", "No terminal app found. Run "
                                              "this in a terminal:\n\n"
                                              + adguard.update_script(self.ag, running))
        self.ag.update_available = None             # re-learned from the next output if still due
        self.ag_update = None
        self._build_menu()

    def adguard_output(self, title: str, fn) -> None:
        from .ui_adguard import TextDialog
        try:
            text = run_blocking(self.window, f"{title}…", fn)
        except adguard.NeedsTerminal as e:
            self._adguard_needs_terminal(e)
            return
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self.window, "AdGuard", friendly_error(e))
            return
        self.run_dialog(TextDialog(f"AdGuard: {title}", text, self.window))

    def _adguard_ready(self) -> bool:
        if self.ag.host.flatpak and not self.ag.host.has_access():
            self.adguard_flatpak_help()
            return False
        if not self.ag.path(refresh=True):
            QtWidgets.QMessageBox.information(
                self.window, "AdGuard", "AdGuard for Linux (adguard-cli) isn't installed. Get it "
                "from adguard.com, then CatFlap shows it in the tray menu.")
            return False
        return True

    def adguard_https_check(self) -> None:
        from .ui_adguard import HttpsCheckDialog
        if not self._adguard_ready():
            return
        gateways = self.m.store.gateways()
        while True:
            try:
                rep = run_blocking(self.window, "Checking HTTPS filtering…",
                                   lambda: adguard.https_report(self.ag, gateways))
            except Exception as e:  # noqa: BLE001
                QtWidgets.QMessageBox.warning(self.window, "AdGuard", friendly_error(e))
                return
            dlg = HttpsCheckDialog(self.ag, rep, self.window)
            self.run_dialog(dlg)
            if not dlg.rerun:
                return

    def adguard_exclude(self) -> None:
        from .ui_adguard import ExcludeDialog
        if not self._adguard_ready():
            return
        dlg = ExcludeDialog(self.window)
        if not self.run_dialog(dlg):
            return
        app = dlg.app.currentText().strip()
        if not app:
            return
        try:
            msg = run_blocking(self.window, f"Excluding {app}…",
                               lambda: adguard.exclude_app(self.ag, app, dlg.action))
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self.window, "AdGuard", friendly_error(e))
            return
        QtWidgets.QMessageBox.information(self.window, "AdGuard", msg)

    def adguard_flatpak_help(self) -> None:
        from .ui_adguard import FlatpakPermissionDialog
        if getattr(self, "_perm_dialog_open", False):
            return
        self._perm_dialog_open = True
        try:
            dlg = FlatpakPermissionDialog(self.ag_in_sandbox_seen,
                                          bool(self.settings.get("adguard_flatpak_declined")),
                                          self.window)
            self.run_dialog(dlg)
        finally:
            self._perm_dialog_open = False
        self.set_setting("adguard_flatpak_declined", dlg.declined)
        self._render()
        if dlg.restart_now:
            self.restart_app()

    # -- restarting into a newly installed version
    def check_installed_version(self) -> None:
        if self._install_checking or self._restarting or sys.platform == "win32":
            return
        self._install_checking = True
        run_bg(update.installed_version, self._installed_version_checked)

    def _installed_version_checked(self, version, err) -> None:
        from .version import is_newer
        self._install_checking = False
        if err or not version or self._restarting:
            return
        try:
            newer = is_newer(version, __version__)
        except ValueError:
            return
        if not newer:
            return
        self.settings["pending_update"] = version
        self.m.store.save()
        self.tray.showMessage("CatFlap", f"CatFlap {version} is installed. Restarting into it… "
                              "Connected sites stay connected.",
                              QtWidgets.QSystemTrayIcon.Information, 3000)
        QtCore.QTimer.singleShot(2500, self.restart_app)

    def restart_app(self, info=None) -> None:
        """Start a fresh CatFlap (the latest installed version) and quit this one for sure."""
        if self._restarting:
            return
        self._restarting = True
        try:
            update.restart(info)
        except OSError as e:
            self._restarting = False
            QtWidgets.QMessageBox.warning(self.window, "CatFlap",
                                          f"Couldn't restart automatically ({e}). Start CatFlap "
                                          "again from your application menu.")
            return
        self.quit(force=True)

    # -- actions
    def _activated(self, reason) -> None:
        if reason in (QtWidgets.QSystemTrayIcon.Trigger, QtWidgets.QSystemTrayIcon.DoubleClick):
            if self.window.isVisible():
                self.window.hide()
            else:
                self.show_window()

    def _message_clicked(self) -> None:
        if self.update_status and self.update_status.available:
            self.check_updates(interactive=True)
        else:
            self.show_window()

    def show_window(self) -> None:
        self.window.refresh()
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def first_run(self) -> None:
        from .ui_wizards import FirstRunWizard
        self.show_window()
        wiz = FirstRunWizard(self.window)
        ok = self.run_dialog(wiz)
        self.settings["first_run_done"] = True
        if ok:
            self.settings["ui_mode"] = wiz.mode
        self.m.store.save()
        self.window.set_mode(self.settings.get("ui_mode", "basic"), save=False)
        if ok and wiz.add_now.isChecked():
            self.add_site()

    def add_site(self) -> None:
        from .ui_wizards import SiteWizard
        self.show_window()
        wiz = SiteWizard(self.m, self.window)
        if self.run_dialog(wiz) and wiz.result is not None:
            name = wiz.result.profile.name
            self.profiles = self.m.store.profiles()
            self._render()
            if wiz.connect_after:
                self.toggle(name)

    def add_gateway(self) -> None:
        from .ui_wizards import GatewayWizard
        self.show_window()
        wiz = GatewayWizard(self.m, self.window)
        if self.run_dialog(wiz) and wiz.gateway:
            QtWidgets.QMessageBox.information(
                self.window, "CatFlap", "Gateway added. Use 'Add a site' to choose what to reach.")

    def toggle(self, name: str) -> None:
        if name in self.busy:
            return
        st = self.status.get(name)
        self.busy.add(name)
        self._render()
        if st and st.active:
            run_bg(lambda: self.m.disconnect(name), lambda r, e: self._toggled(name, None, e, False))
        else:
            run_bg(lambda: self.m.connect(name), lambda r, e: self._toggled(name, r, e, True))

    def _toggled(self, name: str, res, err, connecting: bool) -> None:
        self.busy.discard(name)
        p = self.profiles.get(name)
        label = p.display if p else name
        codes = {f.code: f for f in (res.findings if res is not None else [])}
        if err and offer_wireguard_install(self.window, err, self.m.backend):
            self.toggle(name)                       # WireGuard installed: try again
            return
        if err:
            self.errors[name] = friendly_error(err)
            self.tray.showMessage(f"CatFlap: {label}", self.errors[name], QtWidgets.QSystemTrayIcon.Critical)
        elif connecting and res is not None and not res.ok and "on-site" in codes:
            self.errors[name] = INFO + codes["on-site"].message
            self.checks[name] = res.findings
            self.tray.showMessage(f"CatFlap: {label}", codes["on-site"].message,
                                  QtWidgets.QSystemTrayIcon.Information, 6000)
        elif connecting and res is not None and not res.ok and self._offer_switch(name, label, res):
            return
        elif connecting and res is not None and not res.ok:
            bad = [f.message for f in res.findings if f.level == checks.ERROR]
            self.errors[name] = bad[0] if bad else "failed"
            self.checks[name] = res.findings
            self.tray.showMessage(f"CatFlap: couldn't connect to {label}", "\n".join(bad)[:400],
                                  QtWidgets.QSystemTrayIcon.Warning)
        else:
            self.errors.pop(name, None)
            if res is not None:
                self.checks[name] = res.findings
            if connecting and "local-overlap" in codes:
                self.errors[name] = (INFO + "Your current network uses the same addresses as "
                                     f"{label}; CatFlap is routing around it.")
                self.tray.showMessage(f"CatFlap: {label} connected", codes["local-overlap"].message,
                                      QtWidgets.QSystemTrayIcon.Information, 8000)
            else:
                self.tray.showMessage("CatFlap", f"{label} {'connected' if connecting else 'disconnected'}",
                                      QtWidgets.QSystemTrayIcon.Information, 3000)
        self.poll()

    def _offer_switch(self, name: str, label: str, res) -> bool:
        """Two sites with the same addresses can't both be up: offer to switch."""
        errors = [f for f in res.findings if f.level == checks.ERROR]
        if not errors or any(f.code != "tunnel-overlap" for f in errors):
            return False
        others = sorted({f.other for f in errors if f.other})
        names = ", ".join(self.profiles[o].display if o in self.profiles else o for o in others)
        box = QtWidgets.QMessageBox(self.window)
        box.setWindowTitle("Switch sites?")
        box.setText(f"{label} and {names} use the same addresses, so only one of them can be "
                    "connected at a time.")
        box.setInformativeText(f"Disconnect {names} and connect {label}?")
        switch = box.addButton(f"Switch to {label}", QtWidgets.QMessageBox.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(switch)
        self.run_dialog(box)
        if box.clickedButton() is switch:
            self.busy.add(name)
            self._render()
            run_bg(lambda: self.m.connect(name, switch=True),
                   lambda r, e: self._toggled(name, r, e, True))
        else:
            self.poll()
        return True

    def check_updates(self, interactive: bool) -> None:
        # Always a fresh look at GitHub: at start-up, every few hours, and on request.
        run_bg(lambda: update.check(self.settings, force=True),
               lambda r, e: self._update_checked(r, e, interactive))

    def _periodic_update_check(self) -> None:
        if self.settings.get("update_check", True) and not self._restarting:
            self.check_updates(interactive=False)

    def _update_checked(self, st, err, interactive: bool) -> None:
        self.m.store.save()
        self.update_status = st
        self._build_menu()
        if err or st is None:
            if interactive:
                QtWidgets.QMessageBox.warning(
                    self.window, "CatFlap",
                    "Couldn't check for updates: " + (friendly_error(err) if err else "no answer")
                    + "\n\nCheck your internet connection and try again.")
            return
        if not st.available:
            if interactive:
                QtWidgets.QMessageBox.information(self.window, "CatFlap",
                                                  st.error or f"CatFlap {st.current} is up to date.")
            return
        if not interactive:
            # The first time a version is seen, open the update window so it can't be
            # missed; after that, a notification and the tray menu item are enough.
            if self.settings.get("update_offered") != st.latest.version and not self.paused:
                self.set_setting("update_offered", st.latest.version)
            else:
                self.tray.showMessage("CatFlap update", f"Version {st.latest.version} is available. "
                                      "Click here, or use the tray menu, to update.",
                                      QtWidgets.QSystemTrayIcon.Information)
                return
        from .ui_update import UpdateDialog
        dlg = UpdateDialog(st, self.window, self.settings)
        self.run_dialog(dlg)
        self.m.store.save()
        if dlg.restart_requested and getattr(dlg, "quit_only", False):
            self.quit()
            return
        if dlg.restart_requested:
            self.restart_app(st.install)

    def check_conflicting_apps(self, manual: bool = False, found=None) -> None:
        """Look for other VPN software and list it with uninstall / don't-warn checkboxes.

        Automatic (first start and after every CatFlap update): skips software the user
        silenced at the same version. Manual (Settings menu): lists everything found."""
        from . import conflicts
        if not manual and found is None and self.settings.get("conflicts_scanned_version") == __version__:
            return
        if found is None:
            found = (run_blocking(self.window, "Looking for other VPN software…",
                                  lambda: conflicts.detect(self.m.backend, sys.platform))
                     if manual else conflicts.detect(self.m.backend, sys.platform))
        self.settings["conflicts_scanned_version"] = __version__
        show = found if manual else conflicts.to_warn(found, self.settings)
        self.m.store.save()
        if not show:
            if manual:
                QtWidgets.QMessageBox.information(
                    self.window, "Other VPN software",
                    "No other VPN software found." + ("" if sys.platform == "win32" else
                    "\n\nOn Linux CatFlap sees other VPNs only while they're running."))
            return
        from .ui_conflicts import ConflictsDialog
        dlg = ConflictsDialog(show, self.settings, manual, windows=sys.platform == "win32" or
                              any(f.info for f in show), parent=self.window)
        self.run_dialog(dlg)
        self.apply_conflict_choices(dlg)

    def apply_conflict_choices(self, dlg) -> None:
        from . import conflicts
        conflicts.unsilence(self.settings, dlg.unsilenced())
        conflicts.silence(self.settings, dlg.silenced())
        self.m.store.save()
        done, errors = [], []
        from .platform.windows import uninstall_program
        for f in dlg.uninstall():
            try:
                if f.needs_engine:                 # keep sites working without the app
                    run_blocking(self.window, "Downloading the WireGuard engine…",
                                 self.m.backend.install_wireguard)
                run_blocking(self.window, f"Uninstalling {f.label}…",
                             lambda f=f: uninstall_program(f.info, self.m.backend.run))
                done.append(f"{f.name} was uninstalled")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{f.name}: {friendly_error(e)}")
        for f in dlg.hide_tray():
            try:
                run_blocking(self.window, "Hiding WireGuard's tray icon…",
                             self.m.backend.hide_wireguard_app_tray)
                done.append("WireGuard's tray icon is hidden")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{f.name}: {friendly_error(e)}")
        if errors:
            QtWidgets.QMessageBox.warning(self.window, "CatFlap", "\n\n".join(errors))
        if done:
            self.tray.showMessage("CatFlap", ". ".join(done) + ".",
                                  QtWidgets.QSystemTrayIcon.Information, 5000)

    def show_after_restart(self) -> None:
        from .ui_update import after_restart_message
        msg = after_restart_message(self.settings)
        self.m.store.save()
        if not msg:
            return
        title, text = msg
        if title.startswith("Update not"):
            QtWidgets.QMessageBox.information(self.window, title, text)
        else:
            self.tray.showMessage(title, text, QtWidgets.QSystemTrayIcon.Information, 6000)

    def quit(self, force: bool = True) -> None:
        """Quit. ``force``: also end the process within a few seconds even if something
        (a stuck background job) would keep it alive, so a restart never leaves two."""
        # The single-instance socket stays open until this process has really ended
        # (it answers "quitting" meanwhile), so a CatFlap started now waits for that.
        self.quitting = True
        self.tray.hide()
        if force and not os.environ.get("CATFLAP_NO_FORCE_EXIT"):
            import threading
            t = threading.Timer(4.0, lambda: os._exit(0))
            t.daemon = True
            t.start()
        self.qapp.quit()


# ============================================================ one CatFlap at a time
# Starting CatFlap while it's already running (from the app menu, at login, or a
# restart racing the old copy) used to give two tray icons. Now the first copy
# listens on a local socket; a later one asks it to show its window and exits.
RESTART_FLAG = update.AFTER_RESTART  # started by a restart: wait for the old copy to go


def instance_socket_name() -> str:
    if sys.platform == "win32":
        import getpass
        return f"catflap-tray-{getpass.getuser()}"
    run = os.environ.get("XDG_RUNTIME_DIR", "")
    app_dir = os.path.join(run, "app", os.environ.get("FLATPAK_ID", ""))
    path = ""
    if run and os.environ.get("FLATPAK_ID") and os.path.isdir(app_dir):
        path = os.path.join(app_dir, "tray.sock")   # shared by every copy of the Flatpak
    elif run and os.path.isdir(run):
        path = os.path.join(run, "catflap-tray.sock")
    if path and len(path.encode()) < 100:           # Unix sockets: paths up to ~107 bytes
        return path
    return f"catflap-tray-{os.getuid()}"


NO_INSTANCE = None        # ask_running_instance: nothing is listening
QUITTING = b"quitting"    # ...: a copy is closing down (or hung): wait for it to go


def ask_running_instance(name: str, message: bytes, timeout_ms: int = 1500) -> bytes | None:
    """Send ``message`` to the CatFlap already running. Returns its answer: b"ok" when
    it's running normally, QUITTING when it's closing down or doesn't answer, or
    NO_INSTANCE (None) when no CatFlap is running."""
    from PySide6 import QtNetwork
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        return NO_INSTANCE
    sock.write(message + b"\n")
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    reply = b""
    if sock.waitForReadyRead(timeout_ms):
        reply = bytes(sock.readAll()).strip()
    sock.disconnectFromServer()
    # No answer: its event loop has stopped (it's exiting) or it's stuck.
    return reply if is_running_reply(reply) else QUITTING


def is_running_reply(reply: bytes | None) -> bool:
    """b"ok <version>" (2.6+) or a bare b"ok" (2.2-2.5): a copy running normally."""
    return reply is not None and (reply == b"ok" or reply.startswith(b"ok "))


def reply_version(reply: bytes) -> str | None:
    """The running copy's version from its answer, or None if it's too old to say."""
    return reply[3:].decode(errors="replace").strip() or None if reply.startswith(b"ok ") else None


def wait_for_other_copy(name: str, message: bytes, wait_s: float,
                        sleep=time.sleep, clock=time.monotonic) -> bytes | None:
    """Before starting: None if this copy should start, or the answer of the CatFlap
    that's already running (it was sent ``message``, e.g. to show its window). A copy that is still
    quitting (an update restart, or Quit then a quick start again) is waited for until
    its process has really gone: in the Flatpak every copy is process 2, so two at once
    would both claim the same tray-icon name and the new icon would break."""
    deadline = clock() + wait_s
    while True:
        reply = ask_running_instance(name, message)
        if reply is NO_INSTANCE:
            return None
        if is_running_reply(reply) and message != b"ping-restart":
            return reply
        if clock() >= deadline:
            # Still there after all this time: if it never answers it's hung, so start
            # anyway (claim_single_instance takes its socket over).
            return reply if is_running_reply(reply) else None
        sleep(0.25)


def claim_single_instance(app: "TrayApp", name: str) -> bool:
    """Become the CatFlap that later starts talk to. False if another one holds the name."""
    from PySide6 import QtNetwork
    if is_running_reply(ask_running_instance(name, b"ping")):
        return False
    # No socket options: with them Qt binds elsewhere and renames over the path, which
    # would silently replace a running copy's socket. The runtime dir is private anyway.
    server = QtNetwork.QLocalServer(app)
    if not server.listen(name):
        QtNetwork.QLocalServer.removeServer(name)    # left behind by a copy that crashed
        if not server.listen(name):
            print(f"catflap: can't watch for a second copy ({server.errorString()})", file=sys.stderr)
            return True

    def on_connection():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()

            def read(c=conn, done=[]):  # noqa: B006 - per-connection flag
                if done or not c.canReadLine():
                    return
                done.append(1)
                data = bytes(c.readLine()).strip()
                c.write(QUITTING + b"\n" if app.quitting else b"ok " + __version__.encode() + b"\n")
                c.flush()
                c.disconnectFromServer()
                if data == b"show" and not app.quitting:
                    app.show_window()
            conn.readyRead.connect(read)
            read()      # the message may have arrived before readyRead was connected
    server.newConnection.connect(on_connection)
    app.instance_server = server
    return True


def other_copy_text(reply: bytes) -> str | None:
    """Why this CatFlap isn't starting, when the one in the way is older (or too old to
    say its version); None when it's the same or a newer version (it shows its window)."""
    from .version import is_newer
    theirs = reply_version(reply)
    try:
        if theirs and not is_newer(__version__, theirs):
            return None
    except ValueError:
        pass
    which = f"CatFlap {theirs}" if theirs else "an older CatFlap"
    if os.environ.get("FLATPAK_ID"):
        cmd = f"flatpak kill {os.environ['FLATPAK_ID']}"
    elif sys.platform == "win32":
        cmd = "taskkill /im CatFlap.exe /f"
    else:
        cmd = "pkill -f catflap-tray"
    return (f"{which[0].upper() + which[1:]} is still running, so CatFlap {__version__} can't "
            "start. It may have no working tray icon.\n\n"
            "If you can see its tray icon, right-click it and choose Quit. Otherwise close it "
            f"with this command in a terminal:\n\n    {cmd}\n\n"
            "Then start CatFlap again. (Connected sites stay connected.)")


def explain_other_copy(reply: bytes, quiet: bool = False) -> None:
    text = other_copy_text(reply)
    v = reply_version(reply) or "older version"
    print(f"catflap: CatFlap is already running ({v}); "
          + ("showing its window" if text is None else "it has to be closed first"), file=sys.stderr)
    if text is not None:
        print(text, file=sys.stderr)
        if not quiet:
            box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Warning, "CatFlap", text)
            box.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            box.exec()


def main(argv: list[str] | None = None) -> int:
    require_qt()
    argv = sys.argv[1:] if argv is None else argv
    QtWidgets.QApplication.setApplicationName("catflap")
    QtWidgets.QApplication.setApplicationDisplayName("CatFlap")
    QtWidgets.QApplication.setDesktopFileName(os.environ.get("FLATPAK_ID", "catflap"))
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([sys.argv[0]])
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(app_icon())
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        print("catflap: no system tray available; showing the window only", file=sys.stderr)
    name = instance_socket_name()
    if RESTART_FLAG in argv:
        other = wait_for_other_copy(name, b"ping-restart", 15)
    else:
        # Already running? Bring its window up (unless this is the quiet start at login).
        other = wait_for_other_copy(name, b"ping" if "--minimized" in argv else b"show", 10)
    if other is not None:
        explain_other_copy(other, quiet="--minimized" in argv)
        return 0
    app = TrayApp(qapp)
    claim_single_instance(app, name)
    # Quit cleanly on logout/SIGTERM/Ctrl+C: Python only handles signals while
    # it runs, so wake the interpreter regularly.
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    wake = QtCore.QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(500)
    app.tray.show()
    QtCore.QTimer.singleShot(500, app.show_after_restart)
    QtCore.QTimer.singleShot(2500, app.check_conflicting_apps)
    if not app.settings.get("first_run_done"):
        QtCore.QTimer.singleShot(0, app.first_run)
    elif "--minimized" not in argv:
        app.show_window()
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
