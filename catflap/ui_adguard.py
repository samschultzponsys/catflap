"""AdGuard windows: the HTTPS filtering check, app exclusions, and command output."""

from __future__ import annotations

from . import adguard
from .ui_common import QtWidgets, friendly_error, run_blocking

_Dialog = QtWidgets.QDialog if QtWidgets else object

MARKS = {adguard.OK: ("✔", "#16A34A"), adguard.BAD: ("✖", "#D92D20"), adguard.INFO: ("ℹ", "#3B82F6")}


class TextDialog(_Dialog):
    """Read-only output of an adguard-cli command."""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 420)
        lay = QtWidgets.QVBoxLayout(self)
        box = QtWidgets.QPlainTextEdit(text or "(no output)")
        box.setReadOnly(True)
        box.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        lay.addWidget(box)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class ExcludeDialog(_Dialog):
    """Pick an app to exclude from HTTPS filtering."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Exclude an app from HTTPS filtering")
        lay = QtWidgets.QVBoxLayout(self)
        lab = QtWidgets.QLabel(
            "Apps that check certificates themselves (banking apps, some games and chat apps, "
            "developer tools) break while AdGuard re-signs their connections. Excluding one keeps "
            "its ads filtered where possible but leaves its HTTPS alone.<br><br>"
            "Enter the program's <b>executable name</b> (for example <code>discord</code>); "
            "<code>*</code> matches anything. CatFlap adds it to AdGuard's <code>proxy.yaml</code> "
            "(a backup is kept) and restarts AdGuard.")
        lab.setWordWrap(True)
        lay.addWidget(lab)
        self.app = QtWidgets.QComboBox()
        self.app.setEditable(True)
        self.app.addItems(adguard.EXCLUDE_SUGGESTIONS)
        self.app.setCurrentText("")
        lay.addWidget(self.app)
        self.full = QtWidgets.QCheckBox("Don't filter this app at all (not just its HTTPS)")
        lay.addWidget(self.full)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.Ok).setText("Exclude")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    @property
    def action(self) -> str:
        return "bypass" if self.full.isChecked() else "bypass_https"


class HttpsCheckDialog(_Dialog):
    """One row per place that has to trust AdGuard's certificate, each with a Fix button."""

    def __init__(self, ag: "adguard.AdGuard", report: "adguard.Report", parent=None):
        super().__init__(parent)
        self.ag, self.report = ag, report
        self.rerun = False
        self.messages: list[str] = []
        self.setWindowTitle("AdGuard HTTPS filtering")
        self.resize(720, 560)
        lay = QtWidgets.QVBoxLayout(self)
        n = len(report.problems)
        intro = QtWidgets.QLabel(
            "<h3 style='margin:0'>" + (f"{n} thing{'s' if n != 1 else ''} to fix" if n else
                                       "Nothing to fix") + "</h3>"
            "HTTPS filtering lets AdGuard block ads inside encrypted connections by re-signing them "
            "with its own certificate. Every place below must trust that certificate, or sites and "
            "apps show certificate errors. Apps that refuse any stand-in certificate must be "
            "excluded instead.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        inner = QtWidgets.QWidget()
        rows = QtWidgets.QVBoxLayout(inner)
        self.buttons: dict[int, QtWidgets.QPushButton] = {}
        for i, c in enumerate(report.checks):
            box = QtWidgets.QFrame()
            box.setFrameShape(QtWidgets.QFrame.StyledPanel)
            bl = QtWidgets.QHBoxLayout(box)
            mark, color = MARKS[c.result]
            lab = QtWidgets.QLabel(f"<span style='color:{color};font-size:16px'>{mark}</span>&nbsp; "
                                   f"<b>{c.title}</b><br><small>{c.detail}</small>")
            lab.setWordWrap(True)
            bl.addWidget(lab, 1)
            if c.fix:
                b = QtWidgets.QPushButton(c.fix_label or "Fix")
                b.clicked.connect(lambda _=False, c=c: self.fix(c))
                bl.addWidget(b)
                self.buttons[i] = b
            rows.addWidget(box)
        rows.addStretch(1)
        area.setWidget(inner)
        lay.addWidget(area, 1)
        bb = QtWidgets.QHBoxLayout()
        ex = QtWidgets.QPushButton("Exclude an app…")
        ex.clicked.connect(self.exclude)
        bb.addWidget(ex)
        if report.ca_file:
            op = QtWidgets.QPushButton("Show certificate file")
            op.clicked.connect(self.show_file)
            bb.addWidget(op)
        bb.addStretch(1)
        again = QtWidgets.QPushButton("Check again")
        again.clicked.connect(self.again)
        bb.addWidget(again)
        close = QtWidgets.QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        bb.addWidget(close)
        lay.addLayout(bb)

    def again(self) -> None:
        self.rerun = True
        self.accept()

    def fix(self, c: "adguard.Check") -> None:
        try:
            msg = run_blocking(self, f"Fixing {c.title}…",
                               lambda: adguard.apply_fix(self.ag, c, self.report))
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "AdGuard", friendly_error(e))
            return
        QtWidgets.QMessageBox.information(self, "AdGuard", msg)
        self.again()

    def exclude(self) -> None:
        dlg = ExcludeDialog(self)
        if not dlg.exec():
            return
        app = dlg.app.currentText().strip()
        if not app:
            return
        try:
            msg = run_blocking(self, f"Excluding {app}…",
                               lambda: adguard.exclude_app(self.ag, app, dlg.action))
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "AdGuard", friendly_error(e))
            return
        QtWidgets.QMessageBox.information(self, "AdGuard", msg)

    def show_file(self) -> None:
        QtWidgets.QMessageBox.information(
            self, "AdGuard's certificate",
            f"Saved to:\n{self.report.ca_file}\n\nImport it in apps that keep their own "
            "certificate list (in Firefox: Settings → Privacy & Security → View Certificates → "
            "Authorities → Import, then tick \"Trust this CA to identify websites\").")


__all__ = ["HttpsCheckDialog", "ExcludeDialog", "TextDialog"]
