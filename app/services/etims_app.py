"""
etims_app.py  –  PyQt5 Desktop UI for KRA eTIMS Receipt Manager
Improved UI with fluid layout and proper object arrangement
"""

import sys
import os
import json
import logging
from pathlib import Path
from datetime import date, datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QDateEdit, QTableWidget,
    QTableWidgetItem, QFileDialog, QTabWidget, QTextEdit,
    QComboBox, QCheckBox, QSplitter, QFrame, QScrollArea,
    QGridLayout, QGroupBox, QHeaderView, QAbstractItemView,
    QSizePolicy, QSpacerItem, QProgressBar, QMessageBox,
    QDialog, QDialogButtonBox, QStackedWidget, QListWidget,
    QListWidgetItem, QToolButton, QStatusBar
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QDate, QSize, QTimer, QPropertyAnimation,
    QEasingCurve
)
from PyQt5.QtGui import (
    QFont, QColor, QPalette, QIcon, QPixmap, QPainter,
    QLinearGradient, QBrush, QCursor
)

# ── Colour palette ─────────────────────────────────────────────────────────────
BG          = "#0d1117"
SURFACE     = "#161b22"
SURFACE2    = "#1c2333"
SURFACE3    = "#21262d"
BORDER      = "#30363d"
ACCENT      = "#00e5a0"
ACCENT2     = "#1f6feb"
ACCENT3     = "#ff6b35"
TEXT        = "#e6edf3"
TEXT2       = "#8b949e"
TEXT3       = "#484f58"
SUCCESS     = "#3fb950"
ERROR       = "#f85149"
WARNING     = "#d29922"

# ── Global stylesheet ──────────────────────────────────────────────────────────
STYLE = f"""
QMainWindow, QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Segoe UI", "SF Pro Display", Arial, sans-serif;
    font-size: 13px;
}}

/* ── Sidebar ── */
#Sidebar {{
    background-color: {SURFACE};
    border-right: 1px solid {BORDER};
    min-width: 220px;
    max-width: 220px;
}}

QPushButton#NavBtn {{
    background-color: transparent;
    color: {TEXT2};
    border: none;
    border-radius: 8px;
    text-align: left;
    padding: 12px 18px;
    margin: 2px 8px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#NavBtn:hover  {{ 
    background-color: {SURFACE2}; 
    color: {TEXT};
}}
QPushButton#NavBtn[active="true"] {{
    background-color: rgba(0,229,160,0.12);
    color: {ACCENT};
    border-left: 3px solid {ACCENT};
}}

/* ── Cards / Group boxes ── */
QGroupBox {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
    margin-top: 20px;
    padding: 20px;
    padding-top: 28px;
    font-size: 13px;
    font-weight: 600;
    color: {TEXT};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 16px;
    top: -10px;
    padding: 0 8px;
    color: {TEXT};
    background: {SURFACE};
    font-weight: 600;
}}

/* ── Inputs ── */
QLineEdit, QDateEdit, QComboBox, QTextEdit {{
    background-color: {SURFACE2};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 10px 14px;
    color: {TEXT};
    font-size: 13px;
    selection-background-color: {ACCENT2};
}}
QLineEdit:focus, QDateEdit:focus, QComboBox:focus, QTextEdit:focus {{
    border: 1px solid {ACCENT};
    background-color: {SURFACE3};
}}
QLineEdit[echoMode="2"] {{ letter-spacing: 3px; }}

QDateEdit::drop-down {{ border: none; width: 24px; }}
QDateEdit::down-arrow {{ color: {TEXT2}; }}
QComboBox::drop-down  {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE2};
    border: 1px solid {BORDER};
    selection-background-color: {SURFACE3};
    color: {TEXT};
}}

/* ── Buttons ── */
QPushButton {{
    background-color: {SURFACE3};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 10px 20px;
    font-size: 13px;
    font-weight: 500;
    min-height: 36px;
}}
QPushButton:hover  {{ 
    background-color: {SURFACE2}; 
    border-color: {TEXT3};
    transform: translateY(-1px);
}}
QPushButton:pressed {{ 
    background-color: {BG};
    transform: translateY(1px);
}}
QPushButton:disabled {{ opacity: 0.4; color: {TEXT3}; }}

QPushButton#PrimaryBtn {{
    background-color: {ACCENT};
    color: #0d1117;
    border: none;
    font-weight: 700;
    min-width: 140px;
}}
QPushButton#PrimaryBtn:hover {{ 
    background-color: #00c98e;
    transform: translateY(-1px);
}}
QPushButton#PrimaryBtn:pressed {{ background-color: #00a876; }}

QPushButton#DangerBtn {{
    background-color: {ERROR};
    color: white;
    border: none;
    font-weight: 600;
}}
QPushButton#DangerBtn:hover {{ background-color: #e03e3e; }}

QPushButton#GhostBtn {{
    background-color: transparent;
    color: {TEXT2};
    border: 1px solid {BORDER};
}}
QPushButton#GhostBtn:hover {{ 
    color: {TEXT}; 
    border-color: {TEXT3};
    background-color: {SURFACE2};
}}

QPushButton#SmallBtn {{
    padding: 6px 12px;
    min-height: 28px;
    font-size: 11px;
}}

/* ── Tables ── */
QTableWidget {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
    gridline-color: {BORDER};
    color: {TEXT};
    font-size: 12px;
    selection-background-color: {SURFACE3};
    alternate-background-color: {SURFACE2};
}}
QTableWidget::item {{ padding: 8px 12px; }}
QTableWidget::item:selected {{
    background-color: {SURFACE3};
    color: {TEXT};
}}
QHeaderView::section {{
    background-color: {SURFACE2};
    color: {TEXT2};
    border: none;
    border-bottom: 1px solid {BORDER};
    border-right: 1px solid {BORDER};
    padding: 10px 12px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}

/* ── Console / Log ── */
QTextEdit#LogConsole {{
    background-color: #060810;
    color: {TEXT2};
    border: 1px solid {BORDER};
    border-radius: 10px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
    padding: 12px;
}}

/* ── Tabs ── */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    background: {SURFACE};
}}
QTabBar::tab {{
    background: {SURFACE2};
    color: {TEXT2};
    border: 1px solid {BORDER};
    padding: 10px 24px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{ 
    background: {SURFACE}; 
    color: {TEXT}; 
    border-bottom: none;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}

/* ── Progress bar ── */
QProgressBar {{
    background: {SURFACE2};
    border: 1px solid {BORDER};
    border-radius: 6px;
    height: 8px;
    text-align: center;
    font-size: 10px;
    color: transparent;
}}
QProgressBar::chunk {{ 
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {ACCENT}, stop:1 {ACCENT2});
    border-radius: 5px;
}}

/* ── Checkboxes ── */
QCheckBox {{ 
    color: {TEXT2}; 
    spacing: 8px;
    font-size: 12px;
}}
QCheckBox::indicator {{
    width: 18px; 
    height: 18px;
    background: {SURFACE2};
    border: 1px solid {BORDER};
    border-radius: 4px;
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox:hover {{ color: {TEXT}; }}

/* ── Scroll bars ── */
QScrollBar:vertical {{
    background: transparent; 
    width: 8px; 
    margin: 0;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; 
    border-radius: 4px; 
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT3};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent; 
    height: 8px; 
    margin: 0;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER}; 
    border-radius: 4px; 
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {TEXT3};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ── Status bar ── */
QStatusBar {{
    background: {SURFACE};
    border-top: 1px solid {BORDER};
    color: {TEXT2};
    font-size: 11px;
    padding: 0 16px;
}}
QStatusBar QLabel {{
    padding: 4px 0;
}}

/* ── List widget ── */
QListWidget {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
    color: {TEXT};
    font-size: 13px;
    padding: 4px;
}}
QListWidget::item {{ 
    padding: 12px 16px; 
    border-bottom: 1px solid {BORDER};
    border-radius: 6px;
    margin: 2px;
}}
QListWidget::item:selected {{ 
    background: {SURFACE2}; 
    color: {TEXT};
}}
QListWidget::item:hover {{ 
    background: {SURFACE2};
}}

/* ── Labels ── */
QLabel#AccentLabel {{ color: {ACCENT}; font-weight: 700; }}
QLabel#DimLabel    {{ color: {TEXT3}; font-size: 11px; }}
QLabel#SectionTitle {{
    color: {TEXT};
    font-size: 28px;
    font-weight: 800;
    letter-spacing: -0.5px;
}}
QLabel#SectionSub {{ 
    color: {TEXT2}; 
    font-size: 13px;
    margin-top: 4px;
}}

/* ── Frames / dividers ── */
QFrame#HLine {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {BG}, stop:0.5 {BORDER}, stop:1 {BG});
    max-height: 1px;
    border: none;
    margin: 8px 0;
}}

/* ── Dialog ── */
QDialog {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 16px;
}}
QDialogButtonBox QPushButton {{
    min-width: 90px;
    padding: 8px 20px;
}}

/* ── Tooltips ── */
QToolTip {{
    background-color: {SURFACE2};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 11px;
}}
"""


# ── Worker threads ──────────────────────────────────────────────────────────────

class DownloadWorker(QThread):
    log_signal     = pyqtSignal(str, str)   # message, level
    progress       = pyqtSignal(int, int)    # current, total
    result         = pyqtSignal(list)        # list of invoice dicts
    finished_dl    = pyqtSignal(int)         # count saved

    def __init__(self, cfg, start_dt, end_dt, invc_no="", out_dir="./downloaded_receipts", mode="list"):
        super().__init__()
        self.cfg      = cfg
        self.start_dt = start_dt
        self.end_dt   = end_dt
        self.invc_no  = invc_no
        self.out_dir  = out_dir
        self.mode     = mode   # "list" or "download"
        self.selected_invoices = []

    def run(self):
        try:
            from fill_kra import EtimsConfig, login, _make_session
            from download_invoice import fetch_invoice_list, download_receipt_pdf

            cfg_obj = EtimsConfig(
                pin=self.cfg["pin"],
                username=self.cfg["username"],
                password=self.cfg["password"],
            )
            session = _make_session()
            self.log_signal.emit("Logging in to eTIMS portal…", "info")
            login(cfg_obj, session)
            self.log_signal.emit("Login successful ✓", "ok")

            if self.mode == "list":
                self.log_signal.emit(f"Fetching invoice list: {self.start_dt} → {self.end_dt}", "info")
                invoices = fetch_invoice_list(session, cfg_obj, self.start_dt, self.end_dt, self.invc_no)
                self.log_signal.emit(f"Found {len(invoices)} invoice(s)", "ok")
                self.result.emit(invoices)

            elif self.mode == "download":
                out = Path(self.out_dir)
                saved = 0
                for i, inv in enumerate(self.selected_invoices):
                    self.progress.emit(i + 1, len(self.selected_invoices))
                    self.log_signal.emit(f"Downloading {inv['invcNo']}…", "info")
                    path = download_receipt_pdf(session, cfg_obj, inv["invcNo"], out)
                    if path:
                        saved += 1
                        self.log_signal.emit(f"✓ Saved: {path.name}", "ok")
                    else:
                        self.log_signal.emit(f"⚠ Failed: {inv['invcNo']}", "warn")
                self.finished_dl.emit(saved)

        except ImportError as e:
            self.log_signal.emit(f"Import error: {e} — ensure fill_kra.py and download_invoice.py are in the same folder", "error")
        except Exception as e:
            self.log_signal.emit(f"Error: {e}", "error")


class ParseWorker(QThread):
    log_signal = pyqtSignal(str, str)
    parsed     = pyqtSignal(dict, str)  # grn dict, filename

    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath

    def run(self):
        try:
            self.log_signal.emit(f"Parsing {Path(self.filepath).name}…", "info")
            from read_salesReceipt import read_Grn
            grn = read_Grn(self.filepath)
            self.log_signal.emit(f"✓ Parsed {Path(self.filepath).name}: {len(grn.get('items', []))} items", "ok")
            self.parsed.emit(grn, self.filepath)
        except ImportError as e:
            self.log_signal.emit(f"Import error: {e} — ensure read_salesReceipt.py is in the same folder", "error")
        except Exception as e:
            self.log_signal.emit(f"Parse error: {e}", "error")


class SubmitWorker(QThread):
    log_signal = pyqtSignal(str, str)
    done       = pyqtSignal(bool, str)  # success, message

    def __init__(self, cfg, grn, invoice_no, store_no):
        super().__init__()
        self.cfg        = cfg
        self.grn        = grn
        self.invoice_no = invoice_no
        self.store_no   = store_no

    def run(self):
        try:
            from fill_kra import EtimsConfig, grn_to_receipt, run_fill

            cfg_obj = EtimsConfig(
                pin=self.cfg["pin"],
                username=self.cfg["username"],
                password=self.cfg["password"],
            )

            self.grn["invoice_no"] = self.invoice_no
            self.grn["store_no"]   = self.store_no

            self.log_signal.emit("Building receipt header…", "info")
            header = grn_to_receipt(self.grn, cfg_obj)

            self.log_signal.emit(f"Submitting {len(header.items)} item(s) to KRA…", "info")
            self.log_signal.emit(f"  Customer: {header.cust_nm}", "info")
            self.log_signal.emit(f"  Supply: {header.tot_sply_amt:.2f}  Tax: {header.tot_tax_amt:.2f}  Grand: {header.sum_tot_amt:.2f}", "info")

            results = run_fill(cfg_obj, header)
            ok  = [r for r in results if r["status"] == "ok"]
            err = [r for r in results if r["status"] == "error"]

            if ok:
                self.log_signal.emit(f"✅ Receipt submitted successfully — resultCd=000", "ok")
                self.done.emit(True, f"Receipt submitted OK. {len(ok)} receipt(s) accepted.")
            else:
                msg = err[0].get("error", "Unknown error") if err else "No response"
                self.log_signal.emit(f"❌ Submission failed: {msg}", "error")
                self.done.emit(False, msg)

        except ImportError as e:
            msg = f"Import error: {e} — ensure fill_kra.py is in the same folder"
            self.log_signal.emit(msg, "error")
            self.done.emit(False, msg)
        except Exception as e:
            self.log_signal.emit(f"Error: {e}", "error")
            self.done.emit(False, str(e))


# ── Reusable widgets ────────────────────────────────────────────────────────────

def make_label(text, style="normal"):
    lbl = QLabel(text)
    if style == "title":
        lbl.setObjectName("SectionTitle")
        lbl.setFont(QFont("Segoe UI", 28, QFont.Bold))
    elif style == "sub":
        lbl.setObjectName("SectionSub")
    elif style == "accent":
        lbl.setObjectName("AccentLabel")
    elif style == "dim":
        lbl.setObjectName("DimLabel")
    elif style == "mono":
        lbl.setFont(QFont("Consolas", 11))
        lbl.setStyleSheet(f"color: {TEXT2};")
    return lbl


def make_button(text, style="default", min_w=None, small=False):
    btn = QPushButton(text)
    if style == "primary":
        btn.setObjectName("PrimaryBtn")
    elif style == "danger":
        btn.setObjectName("DangerBtn")
    elif style == "ghost":
        btn.setObjectName("GhostBtn")
    if small:
        btn.setObjectName("SmallBtn")
    if min_w:
        btn.setMinimumWidth(min_w)
    btn.setCursor(QCursor(Qt.PointingHandCursor))
    return btn


def hline():
    f = QFrame()
    f.setObjectName("HLine")
    f.setFrameShape(QFrame.HLine)
    f.setFixedHeight(1)
    return f


def field_row(label_text, widget, stretch=False):
    lbl = QLabel(label_text)
    lbl.setStyleSheet(f"color: {TEXT2}; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;")
    v = QVBoxLayout()
    v.setSpacing(6)
    v.addWidget(lbl)
    v.addWidget(widget)
    if stretch:
        v.addStretch()
    return v


def card_layout(widget, title=None):
    """Create a card-style container"""
    card = QGroupBox()
    if title:
        card.setTitle(title)
    card.setStyleSheet("""
        QGroupBox {
            background-color: """ + SURFACE + """;
            border: 1px solid """ + BORDER + """;
            border-radius: 12px;
            padding: 16px;
        }
    """)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(12)
    layout.addWidget(widget)
    return card


# ── Log mixin ───────────────────────────────────────────────────────────────────

class LogMixin:
    """Provides self.log_console (QTextEdit) and self.log(msg, level)."""

    def _make_log_console(self):
        t = QTextEdit()
        t.setObjectName("LogConsole")
        t.setReadOnly(True)
        t.setMinimumHeight(180)
        return t

    def log(self, msg, level="info"):
        colors = {"info": ACCENT2, "ok": SUCCESS, "error": ERROR,
                  "warn": WARNING, "sys": TEXT3}
        ts    = datetime.now().strftime("%H:%M:%S")
        color = colors.get(level, TEXT2)
        html  = f'<span style="color:{TEXT3};">[{ts}]</span> <span style="color:{color};">{msg}</span>'
        self.log_console.append(html)
        self.log_console.ensureCursorVisible()
        # Emit to main log too if available
        if hasattr(self, "_global_log"):
            self._global_log(msg, level)


# ── Credentials page ────────────────────────────────────────────────────────────

class CredentialsPage(QWidget, LogMixin):
    credentials_saved = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_saved()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 32, 40, 32)
        outer.setSpacing(24)

        # Header section
        header = QVBoxLayout()
        header.setSpacing(8)
        header.addWidget(make_label("Credentials", "title"))
        header.addWidget(make_label("Configure your KRA eTIMS portal login to get started", "sub"))
        header.addWidget(hline())
        outer.addLayout(header)

        # Main content area with flexible spacing
        content = QVBoxLayout()
        content.setSpacing(24)
        
        # Credentials form
        creds_card = QGroupBox("🔑 Portal Authentication")
        creds_card.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 20px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        creds_layout = QGridLayout(creds_card)
        creds_layout.setSpacing(16)
        creds_layout.setContentsMargins(24, 32, 24, 24)

        self.pin_edit = QLineEdit()
        self.pin_edit.setPlaceholderText("P000000000X")
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("P000000000X")
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.setPlaceholderText("••••••••••••")

        creds_layout.addLayout(field_row("KRA PIN", self.pin_edit), 0, 0)
        creds_layout.addLayout(field_row("Username", self.user_edit), 1, 0)
        creds_layout.addLayout(field_row("Password", self.pass_edit), 2, 0)
        
        content.addWidget(creds_card)

        # Button row
        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(16)
        
        self.save_btn = make_button("💾 Save Credentials", "primary", 180)
        self.test_btn = make_button("🔗 Test Connection", "ghost", 160)
        self.save_btn.clicked.connect(self._save)
        self.test_btn.clicked.connect(self._test)
        
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.test_btn)
        btn_layout.addStretch()
        content.addWidget(btn_container)

        # Status card
        self.status_card = QGroupBox("ℹ️ Status")
        self.status_card.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 16px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        sc_layout = QVBoxLayout(self.status_card)
        sc_layout.setContentsMargins(12, 12, 12, 12)
        self.status_label = QLabel("⚪ No credentials saved")
        self.status_label.setStyleSheet(f"color: {TEXT2}; padding: 8px 0; font-size: 13px;")
        sc_layout.addWidget(self.status_label)
        content.addWidget(self.status_card)

        # Info card
        info_card = QGroupBox("📋 About This Tool")
        info_card.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 16px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        al = QVBoxLayout(info_card)
        info = QLabel(
            "This tool helps you:\n\n"
            "  ⬇️  Download eTIMS sales receipt PDFs by date range\n"
            "  📂  Load GRN files (PDF or image) from a folder\n"
            "  🔍  Review and edit line items before submission\n"
            "  🚀  Submit receipts directly to the KRA eTIMS portal\n\n"
            "Supports: PDF · JPG · PNG · TIFF · BMP · WebP"
        )
        info.setStyleSheet(f"color: {TEXT2}; line-height: 1.6; padding: 8px 0;")
        info.setWordWrap(True)
        al.addWidget(info)
        content.addWidget(info_card)
        
        content.addStretch()
        outer.addLayout(content)

        # Console
        console_card = QGroupBox("📟 Activity Log")
        console_card.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 8px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        console_layout = QVBoxLayout(console_card)
        console_layout.setContentsMargins(8, 16, 8, 8)
        self.log_console = self._make_log_console()
        console_layout.addWidget(self.log_console)
        outer.addWidget(console_card)

    def _load_saved(self):
        cfg_path = Path.home() / ".etims_creds.json"
        if cfg_path.exists():
            try:
                c = json.loads(cfg_path.read_text())
                self.pin_edit.setText(c.get("pin", ""))
                self.user_edit.setText(c.get("username", ""))
                self.pass_edit.setText(c.get("password", ""))
                self._update_status(True, c["pin"])
            except Exception:
                pass

    def _save(self):
        pin  = self.pin_edit.text().strip()
        user = self.user_edit.text().strip()
        pwd  = self.pass_edit.text()
        if not all([pin, user, pwd]):
            QMessageBox.warning(self, "Missing Fields", "Please fill in PIN, Username, and Password.")
            return
        cfg = {"pin": pin, "username": user, "password": pwd}
        cfg_path = Path.home() / ".etims_creds.json"
        cfg_path.write_text(json.dumps(cfg))
        self.credentials_saved.emit(cfg)
        self._update_status(True, pin)
        self.log("Credentials saved to ~/.etims_creds.json", "ok")
        QMessageBox.information(self, "Saved", f"Credentials saved for PIN: {pin}")

    def _test(self):
        self.log("Connection test: checking if fill_kra.py is importable…", "info")
        try:
            from fill_kra import EtimsConfig
            self.log("fill_kra.py found ✓ (real login requires running the submit flow)", "ok")
        except ImportError:
            self.log("fill_kra.py not found — place it in the same folder as this app", "error")

    def _update_status(self, ok, pin=""):
        if ok:
            self.status_label.setText(f"🟢 Credentials loaded — PIN: {pin}")
            self.status_label.setStyleSheet(f"color: {SUCCESS}; padding: 8px 0; font-weight: 600; font-size: 13px;")
        else:
            self.status_label.setText("🔴 No credentials saved")
            self.status_label.setStyleSheet(f"color: {ERROR}; padding: 8px 0;")

    def get_credentials(self):
        pin  = self.pin_edit.text().strip()
        user = self.user_edit.text().strip()
        pwd  = self.pass_edit.text()
        if pin and user and pwd:
            return {"pin": pin, "username": user, "password": pwd}
        return None


# ── Download page ───────────────────────────────────────────────────────────────

class DownloadPage(QWidget, LogMixin):
    def __init__(self, get_creds_fn, parent=None):
        super().__init__(parent)
        self.get_creds = get_creds_fn
        self.invoices  = []
        self.worker    = None
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 32, 40, 32)
        outer.setSpacing(24)

        # Header
        header = QVBoxLayout()
        header.setSpacing(8)
        header.addWidget(make_label("Download eTIMS Receipts", "title"))
        header.addWidget(make_label("Fetch and save receipt PDFs from the KRA portal", "sub"))
        header.addWidget(hline())
        outer.addLayout(header)

        # Content
        content = QVBoxLayout()
        content.setSpacing(24)
        
        # Date range card
        date_card = QGroupBox("📅 Date Range")
        date_card.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 20px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        dg_layout = QVBoxLayout(date_card)
        dg_layout.setSpacing(16)
        dg_layout.setContentsMargins(24, 32, 24, 24)

        # Quick date buttons
        quick_row = QHBoxLayout()
        quick_row.setSpacing(12)
        for label, fn in [("Today", "today"), ("Yesterday", "yesterday"), ("This Week", "week"), ("This Month", "month")]:
            b = make_button(label, "ghost", small=True)
            b.setFixedHeight(32)
            b.clicked.connect(lambda _, f=fn: self._quick_date(f))
            quick_row.addWidget(b)
        quick_row.addStretch()
        dg_layout.addLayout(quick_row)

        # Date pickers grid
        date_grid = QGridLayout()
        date_grid.setSpacing(16)
        date_grid.setColumnStretch(0, 1)
        date_grid.setColumnStretch(1, 1)
        
        self.start_date = QDateEdit(QDate.currentDate())
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("dd/MM/yyyy")
        self.end_date = QDateEdit(QDate.currentDate())
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("dd/MM/yyyy")
        self.invc_filter = QLineEdit()
        self.invc_filter.setPlaceholderText("Leave blank for all invoices")
        
        date_grid.addLayout(field_row("Start Date", self.start_date), 0, 0)
        date_grid.addLayout(field_row("End Date", self.end_date), 0, 1)
        date_grid.addLayout(field_row("Invoice No (optional)", self.invc_filter), 1, 0, 1, 2)
        dg_layout.addLayout(date_grid)

        # Output folder row
        out_row = QHBoxLayout()
        out_row.setSpacing(12)
        out_row.setAlignment(Qt.AlignVCenter)
        self.out_dir_edit = QLineEdit("./downloaded_receipts")
        browse_btn = make_button("Browse...", "ghost", small=True)
        browse_btn.setFixedWidth(100)
        browse_btn.setFixedHeight(38)
        browse_btn.clicked.connect(self._browse_out)
        out_row.addLayout(field_row("Output Folder", self.out_dir_edit), 1)
        
        browse_wrap = QVBoxLayout()
        browse_wrap.setSpacing(0)
        browse_wrap.addSpacing(22)   # aligns button to input row (label height)
        browse_wrap.addWidget(browse_btn)
        out_row.addLayout(browse_wrap)
        dg_layout.addLayout(out_row)
        
        content.addWidget(date_card)

        # Fetch button
        btn_row = QHBoxLayout()
        self.fetch_btn = make_button("🔍 Fetch Invoice List", "primary", 200)
        self.fetch_btn.clicked.connect(self._fetch_list)
        btn_row.addWidget(self.fetch_btn)
        btn_row.addStretch()
        content.addLayout(btn_row)

        # Invoices card
        inv_card = QGroupBox("📄 Invoices")
        inv_card.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 20px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        ig_layout = QVBoxLayout(inv_card)
        ig_layout.setSpacing(12)
        ig_layout.setContentsMargins(16, 28, 16, 16)

        # Selection controls
        sel_row = QHBoxLayout()
        self.inv_count_lbl = QLabel("0 invoices found")
        self.inv_count_lbl.setStyleSheet(f"color: {TEXT2}; font-size: 12px; font-weight: 500;")
        sel_row.addWidget(self.inv_count_lbl)
        sel_row.addStretch()
        
        sel_all = make_button("Select All", "ghost", small=True)
        sel_none = make_button("Deselect All", "ghost", small=True)
        sel_all.setFixedHeight(30)
        sel_none.setFixedHeight(30)
        sel_all.clicked.connect(lambda: self._select_all(True))
        sel_none.clicked.connect(lambda: self._select_all(False))
        sel_row.addWidget(sel_all)
        sel_row.addWidget(sel_none)
        ig_layout.addLayout(sel_row)

        self.invoice_table = QTableWidget(0, 5)
        self.invoice_table.setHorizontalHeaderLabels(["", "Invoice No.", "Date/Time", "Customer", "Amount (KES)"])
        self.invoice_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.invoice_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.invoice_table.setColumnWidth(0, 45)
        self.invoice_table.setColumnWidth(2, 170)
        self.invoice_table.setColumnWidth(4, 120)
        self.invoice_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.invoice_table.verticalHeader().setVisible(False)
        self.invoice_table.setAlternatingRowColors(True)
        self.invoice_table.setMinimumHeight(250)
        ig_layout.addWidget(self.invoice_table)

        # Download progress
        self.dl_progress = QProgressBar()
        self.dl_progress.setVisible(False)
        self.dl_progress.setFixedHeight(8)
        ig_layout.addWidget(self.dl_progress)

        # Download button row
        dl_btn_row = QHBoxLayout()
        self.dl_btn = make_button("⬇️ Download Selected PDFs", "primary", 220)
        self.dl_btn.clicked.connect(self._download)
        self.dl_status_lbl = QLabel("")
        self.dl_status_lbl.setStyleSheet(f"color: {TEXT2}; font-size: 12px;")
        dl_btn_row.addWidget(self.dl_btn)
        dl_btn_row.addWidget(self.dl_status_lbl)
        dl_btn_row.addStretch()
        ig_layout.addLayout(dl_btn_row)
        
        content.addWidget(inv_card)
        content.addStretch()
        outer.addLayout(content)

        # Console
        console_card = QGroupBox("📟 Activity Log")
        console_card.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 8px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        console_layout = QVBoxLayout(console_card)
        console_layout.setContentsMargins(8, 16, 8, 8)
        self.log_console = self._make_log_console()
        console_layout.addWidget(self.log_console)
        outer.addWidget(console_card)

    def _quick_date(self, mode):
        today = QDate.currentDate()
        if mode == "today":
            self.start_date.setDate(today)
            self.end_date.setDate(today)
        elif mode == "yesterday":
            y = today.addDays(-1)
            self.start_date.setDate(y)
            self.end_date.setDate(y)
        elif mode == "week":
            self.start_date.setDate(today.addDays(-6))
            self.end_date.setDate(today)
        elif mode == "month":
            self.start_date.setDate(QDate(today.year(), today.month(), 1))
            self.end_date.setDate(today)

    def _browse_out(self):
        d = QFileDialog.getExistingDirectory(self, "Select Output Folder", self.out_dir_edit.text())
        if d:
            self.out_dir_edit.setText(d)

    def _fetch_list(self):
        cfg = self.get_creds()
        if not cfg:
            QMessageBox.warning(self, "No Credentials", "Save your credentials first.")
            return
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching...")
        self.invoice_table.setRowCount(0)
        self.invoices = []

        start = self.start_date.date().toString("dd/MM/yyyy")
        end   = self.end_date.date().toString("dd/MM/yyyy")
        invc  = self.invc_filter.text().strip()

        self.worker = DownloadWorker(cfg, start, end, invc, mode="list")
        self.worker.log_signal.connect(self.log)
        self.worker.result.connect(self._populate_invoices)
        self.worker.finished.connect(lambda: (
            self.fetch_btn.setEnabled(True),
            self.fetch_btn.setText("🔍 Fetch Invoice List")
        ))
        self.worker.start()

    def _populate_invoices(self, invoices):
        self.invoices = invoices
        self.inv_count_lbl.setText(f"{len(invoices)} invoice(s) found")
        self.invoice_table.setRowCount(len(invoices))

        for i, inv in enumerate(invoices):
            cb = QCheckBox()
            cb.setChecked(True)
            cb.setStyleSheet("margin-left: 12px;")
            self.invoice_table.setCellWidget(i, 0, cb)

            for col, val in enumerate([inv.get("invcNo",""), inv.get("rcptDt",""),
                                        inv.get("custNm",""), inv.get("totAmt","")], 1):
                item = QTableWidgetItem(str(val))
                item.setFlags(Qt.ItemIsEnabled)
                if col == 1:
                    item.setForeground(QColor(ACCENT2))
                    item.setFont(QFont("Consolas", 11))
                if col == 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    item.setForeground(QColor(ACCENT))
                self.invoice_table.setItem(i, col, item)

            self.invoice_table.setRowHeight(i, 42)

    def _select_all(self, checked):
        for i in range(self.invoice_table.rowCount()):
            cb = self.invoice_table.cellWidget(i, 0)
            if cb:
                cb.setChecked(checked)

    def _download(self):
        cfg = self.get_creds()
        if not cfg:
            QMessageBox.warning(self, "No Credentials", "Save your credentials first.")
            return

        selected = []
        for i in range(self.invoice_table.rowCount()):
            cb = self.invoice_table.cellWidget(i, 0)
            if cb and cb.isChecked():
                selected.append(self.invoices[i])

        if not selected:
            QMessageBox.warning(self, "None Selected", "Select at least one invoice to download.")
            return

        out_dir = self.out_dir_edit.text() or "./downloaded_receipts"
        self.dl_progress.setVisible(True)
        self.dl_progress.setRange(0, len(selected))
        self.dl_progress.setValue(0)
        self.dl_btn.setEnabled(False)
        self.dl_btn.setText("Downloading...")

        self.worker = DownloadWorker(cfg, "", "", out_dir=out_dir, mode="download")
        self.worker.selected_invoices = selected
        self.worker.log_signal.connect(self.log)
        self.worker.progress.connect(lambda c, t: (
            self.dl_progress.setValue(c),
            self.dl_status_lbl.setText(f"{c} / {t}")
        ))
        self.worker.finished_dl.connect(self._download_done)
        self.worker.start()

    def _download_done(self, count):
        self.dl_btn.setEnabled(True)
        self.dl_btn.setText("⬇️ Download Selected PDFs")
        self.dl_progress.setVisible(False)
        self.dl_status_lbl.setText("")
        QMessageBox.information(self, "Done", f"{count} PDF(s) downloaded to:\n{self.out_dir_edit.text()}")


# ── GRN Process page ────────────────────────────────────────────────────────────

class ProcessPage(QWidget, LogMixin):
    def __init__(self, get_creds_fn, parent=None):
        super().__init__(parent)
        self.get_creds   = get_creds_fn
        self.grn_files   = []   # list of {path, status, grn}
        self.active_idx  = None
        self.parse_worker  = None
        self.submit_worker = None
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 32, 40, 32)
        outer.setSpacing(24)

        # Header
        header = QVBoxLayout()
        header.setSpacing(8)
        header.addWidget(make_label("Process GRN Files", "title"))
        header.addWidget(make_label("Load GRN PDFs or images, review items, then submit to KRA", "sub"))
        header.addWidget(hline())
        outer.addLayout(header)

        # Content
        content = QVBoxLayout()
        content.setSpacing(24)
        
        # File queue card
        file_card = QGroupBox("📂 GRN File Queue")
        file_card.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 20px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        fl = QVBoxLayout(file_card)
        fl.setSpacing(12)
        fl.setContentsMargins(16, 28, 16, 16)

        # Add buttons row
        add_row = QHBoxLayout()
        add_row.setSpacing(12)
        self.add_btn = make_button("➕ Add Files...", "primary", 140)
        self.add_dir_btn = make_button("📁 Add Folder...", "ghost", 140)
        self.clear_btn = make_button("🗑 Clear All", "ghost", 120)
        self.add_btn.clicked.connect(self._add_files)
        self.add_dir_btn.clicked.connect(self._add_folder)
        self.clear_btn.clicked.connect(self._clear_queue)
        add_row.addWidget(self.add_btn)
        add_row.addWidget(self.add_dir_btn)
        add_row.addWidget(self.clear_btn)
        add_row.addStretch()
        fl.addLayout(add_row)

        self.file_table = QTableWidget(0, 5)
        self.file_table.setHorizontalHeaderLabels(["Filename", "Type", "Size", "Status", "Actions"])
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_table.setColumnWidth(1, 70)
        self.file_table.setColumnWidth(2, 90)
        self.file_table.setColumnWidth(3, 110)
        self.file_table.setColumnWidth(4, 170)
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.setMinimumHeight(200)
        self.file_table.setSelectionMode(QAbstractItemView.NoSelection)
        fl.addWidget(self.file_table)
        content.addWidget(file_card)

        # Review panel (initially hidden)
        self.review_grp = QGroupBox("🔍 GRN Review")
        self.review_grp.setVisible(False)
        self.review_grp.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 20px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        rv = QVBoxLayout(self.review_grp)
        rv.setSpacing(16)
        rv.setContentsMargins(16, 28, 16, 16)

        self.review_filename_lbl = QLabel("")
        self.review_filename_lbl.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-weight: 700;")
        rv.addWidget(self.review_filename_lbl)

        # Meta info grid
        self.meta_grid = QGridLayout()
        self.meta_grid.setSpacing(12)
        rv.addLayout(self.meta_grid)

        rv.addWidget(hline())
        rv.addWidget(QLabel("Non-Fiscal Information"))
        QLabel().setStyleSheet(f"color: {TEXT2}; font-weight: 600;")

        nf_row = QHBoxLayout()
        nf_row.setSpacing(16)
        self.nf_invoice = QLineEdit()
        self.nf_invoice.setPlaceholderText("e.g. 193")
        self.nf_store = QLineEdit()
        self.nf_store.setPlaceholderText("e.g. 110")
        nf_row.addLayout(field_row("Invoice No", self.nf_invoice), 1)
        nf_row.addLayout(field_row("Store No", self.nf_store), 1)
        nf_row.addStretch()
        rv.addLayout(nf_row)

        rv.addWidget(hline())
        rv.addWidget(QLabel("Line Items"))
        
        self.items_table = QTableWidget(0, 8)
        self.items_table.setHorizontalHeaderLabels(
            ["#", "Item Code", "Description", "UOM", "Qty", "Unit Price", "Net Amount", "Tax"]
        )
        self.items_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.items_table.setColumnWidth(0, 45)
        self.items_table.setColumnWidth(1, 120)
        self.items_table.setColumnWidth(3, 65)
        self.items_table.setColumnWidth(4, 70)
        self.items_table.setColumnWidth(5, 100)
        self.items_table.setColumnWidth(6, 110)
        self.items_table.setColumnWidth(7, 60)
        self.items_table.verticalHeader().setVisible(False)
        self.items_table.setAlternatingRowColors(True)
        self.items_table.setMinimumHeight(200)
        rv.addWidget(self.items_table)

        # Totals row
        self.total_lbl = QLabel("")
        self.total_lbl.setAlignment(Qt.AlignRight)
        self.total_lbl.setStyleSheet(f"color: {ACCENT}; font-size: 18px; font-weight: 700; padding: 8px 4px;")
        rv.addWidget(self.total_lbl)

        # Submit buttons
        sub_row = QHBoxLayout()
        sub_row.setSpacing(16)
        self.submit_btn = make_button("🚀 Submit to KRA eTIMS", "primary", 220)
        self.cancel_review_btn = make_button("✕ Close Review", "ghost")
        self.submit_status_lbl = QLabel("")
        self.submit_status_lbl.setStyleSheet(f"color: {TEXT2}; font-size: 12px;")
        self.submit_btn.clicked.connect(self._submit)
        self.cancel_review_btn.clicked.connect(self._close_review)
        sub_row.addWidget(self.submit_btn)
        sub_row.addWidget(self.cancel_review_btn)
        sub_row.addStretch()
        sub_row.addWidget(self.submit_status_lbl)
        rv.addLayout(sub_row)

        content.addWidget(self.review_grp)
        content.addStretch()
        outer.addLayout(content)

        # Console
        console_card = QGroupBox("📟 Activity Log")
        console_card.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 8px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        console_layout = QVBoxLayout(console_card)
        console_layout.setContentsMargins(8, 16, 8, 8)
        self.log_console = self._make_log_console()
        console_layout.addWidget(self.log_console)
        outer.addWidget(console_card)

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select GRN Files", "",
            "GRN Files (*.pdf *.jpg *.jpeg *.png *.tif *.tiff *.bmp *.webp)"
        )
        self._enqueue(paths)

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select GRN Folder")
        if folder:
            exts = {".pdf",".jpg",".jpeg",".png",".tif",".tiff",".bmp",".webp"}
            paths = [str(p) for p in Path(folder).iterdir() if p.suffix.lower() in exts]
            self._enqueue(paths)
            self.log(f"Added {len(paths)} file(s) from {folder}", "info")

    def _enqueue(self, paths):
        existing = {f["path"] for f in self.grn_files}
        added = 0
        for p in paths:
            if p not in existing:
                self.grn_files.append({"path": p, "status": "pending", "grn": None})
                added += 1
        if added:
            self._refresh_file_table()
            self.log(f"Added {added} file(s) to queue", "info")

    def _clear_queue(self):
        self.grn_files = []
        self._refresh_file_table()
        self._close_review()

    def _refresh_file_table(self):
        self.file_table.setRowCount(len(self.grn_files))
        status_colors = {
            "pending":   (WARNING, "Pending"),
            "parsed":    (SUCCESS, "Parsed ✓"),
            "submitted": (ACCENT,  "Submitted ✓"),
            "error":     (ERROR,   "Error"),
        }
        for i, f in enumerate(self.grn_files):
            p = Path(f["path"])
            ext = p.suffix.upper().lstrip(".")
            size_kb = p.stat().st_size // 1024 if p.exists() else 0
            color, status_text = status_colors.get(f["status"], (TEXT2, f["status"]))

            items_data = [
                (p.name, TEXT, None),
                (ext, ACCENT2, None),
                (f"{size_kb} KB", TEXT2, None),
                (status_text, color, None),
            ]
            for col, (val, c, _) in enumerate(items_data):
                it = QTableWidgetItem(val)
                it.setFlags(Qt.ItemIsEnabled)
                it.setForeground(QColor(c))
                if col == 1:
                    it.setFont(QFont("Consolas", 10))
                self.file_table.setItem(i, col, it)

            # Action buttons
            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(6, 4, 6, 4)
            btn_layout.setSpacing(8)

            parse_btn = make_button("Parse", "ghost", small=True)
            parse_btn.setFixedSize(70, 30)
            parse_btn.clicked.connect(lambda _, idx=i: self._parse_file(idx))

            review_btn = make_button("Review", "ghost", small=True)
            review_btn.setFixedSize(70, 30)
            review_btn.setEnabled(f["grn"] is not None)
            review_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent;
                    color: {ACCENT if f['grn'] else TEXT3};
                    border: 1px solid {BORDER};
                    border-radius: 6px;
                    padding: 5px 12px;
                    font-size: 11px;
                }}
                QPushButton:hover {{
                    background-color: {SURFACE2};
                }}
            """)
            review_btn.clicked.connect(lambda _, idx=i: self._open_review(idx))

            btn_layout.addWidget(parse_btn)
            btn_layout.addWidget(review_btn)
            self.file_table.setCellWidget(i, 4, btn_widget)
            self.file_table.setRowHeight(i, 48)

    def _parse_file(self, idx):
        self.parse_worker = ParseWorker(self.grn_files[idx]["path"])
        self.parse_worker.log_signal.connect(self.log)
        self.parse_worker.parsed.connect(lambda grn, path: self._on_parsed(grn, path, idx))
        self.parse_worker.start()

    def _on_parsed(self, grn, path, idx):
        self.grn_files[idx]["grn"]    = grn
        self.grn_files[idx]["status"] = "parsed"
        self._refresh_file_table()
        self._open_review(idx)

    def _open_review(self, idx):
        f   = self.grn_files[idx]
        grn = f["grn"]
        if not grn:
            return

        self.active_idx = idx
        self.review_filename_lbl.setText(f"📄 {Path(f['path']).name}")

        # Clear meta grid
        for i in reversed(range(self.meta_grid.count())):
            w = self.meta_grid.itemAt(i).widget()
            if w:
                w.deleteLater()

        meta_fields = [
            ("LPO Number", grn.get("lpo_number", "—")),
            ("GRN / RVN No.", grn.get("receipt_voucher_no", "—")),
            ("Delivery Note", grn.get("delivery_invoice_no", "—")),
            ("Date", grn.get("receipt_date", "—")),
            ("Supplier", (grn.get("supplier") or {}).get("company_name", "—")),
            ("Store", " ".join(filter(None, [
                (grn.get("store") or {}).get("company_name", ""),
                (grn.get("store") or {}).get("store_name", ""),
            ])) or "—"),
        ]
        for col, (label, val) in enumerate(meta_fields):
            container = QWidget()
            container.setStyleSheet(f"""
                background-color: {SURFACE2};
                border: 1px solid {BORDER};
                border-radius: 8px;
            """)
            cv = QVBoxLayout(container)
            cv.setContentsMargins(12, 10, 12, 10)
            cv.setSpacing(4)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {TEXT3}; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; background: transparent; border: none;")
            val_lbl = QLabel(str(val) if val else "—")
            val_lbl.setStyleSheet(f"color: {TEXT}; font-size: 12px; font-weight: 500; background: transparent; border: none;")
            val_lbl.setWordWrap(True)
            cv.addWidget(lbl)
            cv.addWidget(val_lbl)
            self.meta_grid.addWidget(container, col // 3, col % 3)

        # Non-fiscal
        self.nf_invoice.setText(grn.get("invoice_no", ""))
        self.nf_store.setText(grn.get("store_no", ""))

        # Items
        items = grn.get("items", [])
        self.items_table.setRowCount(len(items))
        total = 0.0
        for r, it in enumerate(items):
            try:
                net = float(it.get("net_amount", 0))
            except (TypeError, ValueError):
                net = 0.0
            total += net

            row_data = [
                (str(it.get("no", r+1)), TEXT3, False),
                (str(it.get("item_code", "")), ACCENT2, False),
                (str(it.get("description", "")), TEXT, True),
                (str(it.get("uom", "PCS")), TEXT2, True),
                (str(it.get("qty_received", 1)), TEXT, True),
                (f"{float(it.get('unit_price', 0)):.2f}", TEXT, True),
                (f"{net:,.2f}", ACCENT, False),
                (str(it.get("tax_ty_cd", "D")), TEXT2, True),
            ]
            for col, (val, color, editable) in enumerate(row_data):
                item = QTableWidgetItem(val)
                item.setForeground(QColor(color))
                if not editable:
                    item.setFlags(Qt.ItemIsEnabled)
                self.items_table.setItem(r, col, item)
            self.items_table.setRowHeight(r, 40)

        self.total_lbl.setText(f"Total Supply: KES {total:,.2f}")
        self.review_grp.setVisible(True)

    def _close_review(self):
        self.review_grp.setVisible(False)
        self.active_idx = None

    def _submit(self):
        cfg = self.get_creds()
        if not cfg:
            QMessageBox.warning(self, "No Credentials", "Save your credentials first.")
            return
        if self.active_idx is None:
            return

        grn = self.grn_files[self.active_idx]["grn"]

        # Sync edited items back to grn
        items = grn.get("items", [])
        for r, it in enumerate(items):
            for col, field in [(2,"description"), (3,"uom"), (4,"qty_received"), (5,"unit_price"), (7,"tax_ty_cd")]:
                cell = self.items_table.item(r, col)
                if cell:
                    val = cell.text()
                    if field in ("qty_received", "unit_price"):
                        try:
                            val = float(val)
                        except ValueError:
                            pass
                    it[field] = val

        invoice_no = self.nf_invoice.text().strip()
        store_no   = self.nf_store.text().strip()

        self.submit_btn.setEnabled(False)
        self.submit_btn.setText("Submitting...")
        self.submit_status_lbl.setText("Connecting to KRA portal...")

        self.submit_worker = SubmitWorker(cfg, grn, invoice_no, store_no)
        self.submit_worker.log_signal.connect(self.log)
        self.submit_worker.done.connect(self._on_submit_done)
        self.submit_worker.start()

    def _on_submit_done(self, success, msg):
        self.submit_btn.setEnabled(True)
        self.submit_btn.setText("🚀 Submit to KRA eTIMS")
        self.submit_status_lbl.setText("")

        if success:
            self.grn_files[self.active_idx]["status"] = "submitted"
            self._refresh_file_table()
            self._close_review()
            QMessageBox.information(self, "Success ✓", msg)
        else:
            self.grn_files[self.active_idx]["status"] = "error"
            self._refresh_file_table()
            QMessageBox.critical(self, "Submission Failed", msg)


# ── Activity log page ───────────────────────────────────────────────────────────

class LogPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.stats = {"total": 0, "ok": 0, "err": 0, "dl": 0}
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 32, 40, 32)
        outer.setSpacing(24)

        # Header
        header = QVBoxLayout()
        header.setSpacing(8)
        header.addWidget(make_label("Activity Log", "title"))
        header.addWidget(make_label("All operations and KRA portal responses", "sub"))
        header.addWidget(hline())
        outer.addLayout(header)

        # Stats row
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(16)
        self.stat_widgets = {}
        
        stats_data = [
            ("total", "Total Submitted", TEXT),
            ("ok", "Successful", SUCCESS),
            ("err", "Errors", ERROR),
            ("dl", "Downloaded", ACCENT2),
        ]
        
        for key, label, color in stats_data:
            card = QGroupBox(label)
            card.setStyleSheet(f"""
                QGroupBox {{
                    background-color: {SURFACE};
                    border: 1px solid {BORDER};
                    border-radius: 12px;
                    margin-top: 20px;
                    padding: 16px;
                }}
                QGroupBox::title {{
                    subcontrol-origin: margin;
                    left: 12px;
                    top: -10px;
                    padding: 0 6px;
                    background-color: {SURFACE};
                    color: {TEXT2};
                    font-size: 11px;
                    font-weight: 600;
                }}
            """)
            cv = QVBoxLayout(card)
            cv.setContentsMargins(8, 16, 8, 8)
            val_lbl = QLabel("0")
            val_lbl.setFont(QFont("Segoe UI", 32, QFont.Bold))
            val_lbl.setStyleSheet(f"color: {color};")
            val_lbl.setAlignment(Qt.AlignCenter)
            cv.addWidget(val_lbl)
            self.stat_widgets[key] = val_lbl
            stats_layout.addWidget(card, 1)
        
        outer.addLayout(stats_layout)

        # Console card
        console_card = QGroupBox("📟 Console")
        console_card.setStyleSheet(f"""
            QGroupBox {{
                background-color: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 12px;
                margin-top: 20px;
                padding: 8px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 16px;
                top: -10px;
                padding: 0 8px;
                background-color: {SURFACE};
                color: {TEXT};
                font-weight: 700;
                font-size: 13px;
            }}
        """)
        lg = QVBoxLayout(console_card)
        lg.setSpacing(8)
        lg.setContentsMargins(8, 16, 8, 8)

        ctrl_row = QHBoxLayout()
        clear_btn = make_button("Clear Log", "ghost", small=True)
        clear_btn.setFixedHeight(32)
        clear_btn.setFixedWidth(100)
        clear_btn.clicked.connect(self._clear)
        ctrl_row.addStretch()
        ctrl_row.addWidget(clear_btn)
        lg.addLayout(ctrl_row)

        self.console = QTextEdit()
        self.console.setObjectName("LogConsole")
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(400)
        lg.addWidget(self.console)
        outer.addWidget(console_card)

    def add_log(self, msg, level="sys"):
        colors = {"info": ACCENT2, "ok": SUCCESS, "error": ERROR,
                  "warn": WARNING, "sys": TEXT3}
        ts    = datetime.now().strftime("%H:%M:%S")
        color = colors.get(level, TEXT2)
        html  = f'<span style="color:{TEXT3};">[{ts}]</span> <span style="color:{color};">{msg}</span>'
        self.console.append(html)
        self.console.ensureCursorVisible()

    def _clear(self):
        self.console.clear()

    def update_stats(self, stats):
        self.stats = stats
        for k, lbl in self.stat_widgets.items():
            lbl.setText(str(stats.get(k, 0)))


# ── Main Window ─────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("eTIMS Portal — KRA Receipt Manager")
        self.setMinimumSize(1200, 800)
        self.resize(1360, 900)
        self.setStyleSheet(STYLE)

        self.stats = {"total": 0, "ok": 0, "err": 0, "dl": 0}

        self._build_ui()
        self._connect_logs()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Sidebar ──────────────────────────────────────────────────────────
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(240)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(0, 0, 0, 0)
        sb_layout.setSpacing(0)

        # Logo area
        logo_area = QWidget()
        logo_area.setFixedHeight(70)
        logo_area.setStyleSheet(f"border-bottom: 1px solid {BORDER};")
        la = QHBoxLayout(logo_area)
        la.setContentsMargins(20, 0, 20, 0)
        
        icon_box = QLabel("eT")
        icon_box.setFixedSize(36, 36)
        icon_box.setAlignment(Qt.AlignCenter)
        icon_box.setStyleSheet(f"""
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 {ACCENT}, stop:1 {ACCENT2});
            color: #0d1117;
            border-radius: 10px;
            font-weight: 800;
            font-size: 14px;
        """)
        
        title_lbl = QLabel("eTIMS Portal")
        title_lbl.setStyleSheet(f"color: {TEXT}; font-size: 18px; font-weight: 700; margin-left: 8px;")
        
        la.addWidget(icon_box)
        la.addWidget(title_lbl)
        la.addStretch()
        sb_layout.addWidget(logo_area)

        # Navigation section
        nav_section = QWidget()
        nav_layout = QVBoxLayout(nav_section)
        nav_layout.setContentsMargins(0, 16, 0, 0)
        nav_layout.setSpacing(4)
        
        nav_items = [
            ("⚙️ Credentials", "settings"),
            ("⬇️ Download eTIMS", "download"),
            ("📂 Process GRNs", "process"),
            ("📋 Activity Log", "log"),
        ]
        
        self.nav_buttons = []
        for label, page_id in nav_items:
            btn = QPushButton(label)
            btn.setObjectName("NavBtn")
            btn.setFixedHeight(44)
            btn.setCursor(QCursor(Qt.PointingHandCursor))
            btn.clicked.connect(lambda _, pid=page_id: self._switch_page(pid))
            nav_layout.addWidget(btn)
            self.nav_buttons.append((btn, page_id))
        
        nav_layout.addStretch()
        sb_layout.addWidget(nav_section)

        # Credential status in sidebar
        self.cred_status_lbl = QLabel("⚪ No credentials")
        self.cred_status_lbl.setStyleSheet(f"""
            color: {TEXT3};
            font-size: 11px;
            padding: 16px 20px;
            border-top: 1px solid {BORDER};
            margin-top: 8px;
        """)
        self.cred_status_lbl.setWordWrap(True)
        sb_layout.addWidget(self.cred_status_lbl)

        root.addWidget(sidebar)

        # ── Pages ─────────────────────────────────────────────────────────────
        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        self.creds_page = CredentialsPage()
        self.download_page = DownloadPage(self.creds_page.get_credentials)
        self.process_page = ProcessPage(self.creds_page.get_credentials)
        self.log_page = LogPage()

        for page in [self.creds_page, self.download_page, self.process_page, self.log_page]:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            scroll.setStyleSheet(f"""
                QScrollArea {{
                    border: none;
                    background: {BG};
                }}
                QScrollArea > QWidget > QWidget {{
                    background: {BG};
                }}
            """)
            self.stack.addWidget(scroll)

        self.creds_page.credentials_saved.connect(self._on_creds_saved)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready • Place fill_kra.py, download_invoice.py, read_salesReceipt.py in the same folder")
        self.status_bar.setStyleSheet(f"""
            QStatusBar {{
                background: {SURFACE};
                border-top: 1px solid {BORDER};
                color: {TEXT2};
                font-size: 11px;
                padding: 0 16px;
            }}
        """)

        # Activate first page
        self._switch_page("settings")

    def _connect_logs(self):
        """Forward logs from sub-pages to the global log page."""
        def fwd(msg, level):
            self.log_page.add_log(msg, level)
            if level == "ok":
                self.stats["total"] += 1
                self.stats["ok"] += 1
            elif level == "error":
                self.stats["err"] += 1
            self.log_page.update_stats(self.stats)

        self.creds_page._global_log = fwd
        self.download_page._global_log = fwd
        self.process_page._global_log = fwd

    def _switch_page(self, page_id):
        pages = {"settings": 0, "download": 1, "process": 2, "log": 3}
        idx = pages.get(page_id, 0)
        self.stack.setCurrentIndex(idx)

        for btn, pid in self.nav_buttons:
            btn.setProperty("active", str(pid == page_id).lower())
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _on_creds_saved(self, cfg):
        self.cred_status_lbl.setText(f"🟢 {cfg['pin']}")
        self.cred_status_lbl.setStyleSheet(f"""
            color: {SUCCESS};
            font-size: 11px;
            padding: 16px 20px;
            border-top: 1px solid {BORDER};
            margin-top: 8px;
            font-weight: 600;
        """)
        self.status_bar.showMessage(f"Credentials saved — PIN: {cfg['pin']}")


# ── Entry point ─────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("eTIMS Portal")
    app.setApplicationVersion("1.0")

    # High-DPI support
    app.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()