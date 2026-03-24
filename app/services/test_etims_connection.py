"""
test_etims_connection.py  –  Health-check the KRA eTIMS portal before submitting.

Checks (in order)
─────────────────
1.  DNS resolution          – can we resolve etims.kra.go.ke?
2.  TCP reachability        – is port 443 open?
3.  TLS handshake           – does the certificate validate?
4.  HTTP reachability       – does the login page return HTTP 200?
5.  Session cookie seed     – do we get a JSESSIONID + BIGip cookie back?
6.  Login (optional)        – does the credential POST succeed?
7.  Sales endpoint probe    – does the sales endpoint respond (HEAD/GET)?

Each check is independent; a failure stops the chain and reports clearly.

Usage
─────
    # Quick connectivity-only check (no credentials needed):
    python test_etims_connection.py

    # Full check including login:
    python test_etims_connection.py --full

    # Custom timeout (default 15 s):
    python test_etims_connection.py --full --timeout 20

    # Repeat every N seconds (useful when waiting for the site to recover):
    python test_etims_connection.py --watch 30

    # JSON output (for scripted callers):
    python test_etims_connection.py --full --json

Credentials are read from environment variables:
    KRA_PIN, KRA_USERNAME, KRA_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL        = "https://etims.kra.go.ke"
HOSTNAME        = "etims.kra.go.ke"
PORT            = 443
LOGIN_PAGE_PATH = "/basic/login/indexLogin"
LOGIN_PATH      = "/basic/login/loginProc"
SALES_PATH      = "/app/ebm/trns/sales/insertTrnsSalesReceipt"
LIST_PATH       = "/app/ebm/trns/sales/trnsSalesReceiptList"

DEFAULT_TIMEOUT = 15   # seconds per check

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# RESULT MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name:    str
    passed:  bool
    message: str
    detail:  Optional[str] = None
    elapsed: Optional[float] = None   # seconds


@dataclass
class HealthReport:
    timestamp:  str
    checks:     List[CheckResult] = field(default_factory=list)
    site_up:    bool = False          # True only if all mandatory checks pass

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "site_up":   self.site_up,
            "checks": [
                {
                    "name":    c.name,
                    "passed":  c.passed,
                    "message": c.message,
                    "detail":  c.detail,
                    "elapsed_s": round(c.elapsed, 3) if c.elapsed is not None else None,
                }
                for c in self.checks
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# INDIVIDUAL CHECKS
# ─────────────────────────────────────────────────────────────────────────────

def check_dns(hostname: str = HOSTNAME, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Check 1 – DNS resolution."""
    t0 = time.perf_counter()
    try:
        socket.setdefaulttimeout(timeout)
        addrs = socket.getaddrinfo(hostname, PORT, proto=socket.IPPROTO_TCP)
        elapsed = time.perf_counter() - t0
        ip = addrs[0][4][0]
        return CheckResult(
            name="DNS resolution",
            passed=True,
            message=f"Resolved to {ip}",
            detail=str(addrs[0]),
            elapsed=elapsed,
        )
    except socket.gaierror as exc:
        return CheckResult(
            name="DNS resolution",
            passed=False,
            message=f"DNS lookup failed: {exc}",
            elapsed=time.perf_counter() - t0,
        )


def check_tcp(hostname: str = HOSTNAME, port: int = PORT,
              timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Check 2 – TCP port reachability."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            pass
        elapsed = time.perf_counter() - t0
        return CheckResult(
            name="TCP reachability",
            passed=True,
            message=f"Port {port} open on {hostname}",
            elapsed=elapsed,
        )
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        return CheckResult(
            name="TCP reachability",
            passed=False,
            message=f"Cannot connect to {hostname}:{port} — {exc}",
            elapsed=time.perf_counter() - t0,
        )


def check_tls(hostname: str = HOSTNAME, port: int = PORT,
              timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Check 3 – TLS certificate validation."""
    t0 = time.perf_counter()
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((hostname, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=hostname) as tls:
                cert = tls.getpeercert()
        elapsed = time.perf_counter() - t0
        expiry = cert.get("notAfter", "unknown")
        return CheckResult(
            name="TLS handshake",
            passed=True,
            message=f"TLS OK — cert valid until {expiry}",
            elapsed=elapsed,
        )
    except ssl.SSLCertVerificationError as exc:
        return CheckResult(
            name="TLS handshake",
            passed=False,
            message=f"TLS certificate error: {exc}",
            elapsed=time.perf_counter() - t0,
        )
    except (socket.timeout, OSError) as exc:
        return CheckResult(
            name="TLS handshake",
            passed=False,
            message=f"TLS connection failed: {exc}",
            elapsed=time.perf_counter() - t0,
        )


def _bare_session(timeout: float) -> requests.Session:
    """A minimal session with NO automatic retries (we want honest failure times)."""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent":       "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Accept-Language":  "en-GB,en-US;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
    })
    return sess


def check_http(timeout: float = DEFAULT_TIMEOUT) -> tuple[CheckResult, Optional[requests.Session]]:
    """Check 4 – HTTP 200 on the login page."""
    t0  = time.perf_counter()
    url = f"{BASE_URL}{LOGIN_PAGE_PATH}"
    sess = _bare_session(timeout)
    try:
        r = sess.get(url, timeout=timeout, allow_redirects=True)
        elapsed = time.perf_counter() - t0
        if r.status_code == 200:
            return CheckResult(
                name="HTTP reachability",
                passed=True,
                message=f"GET {url} → HTTP {r.status_code} ({elapsed:.2f}s)",
                elapsed=elapsed,
            ), sess
        else:
            return CheckResult(
                name="HTTP reachability",
                passed=False,
                message=f"GET {url} → unexpected HTTP {r.status_code}",
                detail=r.text[:300],
                elapsed=elapsed,
            ), None
    except requests.exceptions.SSLError as exc:
        return CheckResult(
            name="HTTP reachability",
            passed=False,
            message=f"SSL error: {exc}",
            elapsed=time.perf_counter() - t0,
        ), None
    except requests.exceptions.ConnectionError as exc:
        return CheckResult(
            name="HTTP reachability",
            passed=False,
            message=f"Connection error: {exc}",
            elapsed=time.perf_counter() - t0,
        ), None
    except requests.exceptions.Timeout:
        elapsed = time.perf_counter() - t0
        return CheckResult(
            name="HTTP reachability",
            passed=False,
            message=f"Timed out after {elapsed:.1f}s — site is likely down or overloaded",
            elapsed=elapsed,
        ), None


def check_session_cookie(sess: requests.Session,
                         timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Check 5 – JSESSIONID + BIGip cookie present after the seed GET."""
    jsid    = sess.cookies.get("JSESSIONID", "")
    bigip   = next((v for k, v in sess.cookies.items() if k.startswith("BIGip")), "")
    if jsid:
        return CheckResult(
            name="Session cookie",
            passed=True,
            message=f"JSESSIONID set ({jsid[:8]}…)  BIGip={'yes' if bigip else 'missing (may still work)'}",
        )
    else:
        return CheckResult(
            name="Session cookie",
            passed=False,
            message="No JSESSIONID cookie — portal did not seed a session. "
                    "Site may be returning a static error page.",
        )


def check_login(sess: requests.Session, pin: str, username: str, password: str,
                timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Check 6 – Credential POST to loginProc."""
    t0  = time.perf_counter()
    url = f"{BASE_URL}{LOGIN_PATH}"
    payload = {"mbrId": username, "mbrPwd": password}
    hdrs = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept":       "application/json, text/javascript, */*; q=0.01",
        "Origin":       BASE_URL,
        "Referer":      f"{BASE_URL}{LOGIN_PAGE_PATH}",
    }
    try:
        r = sess.post(url, data=payload, headers=hdrs, timeout=timeout)
        elapsed = time.perf_counter() - t0

        if r.status_code == 401:
            return CheckResult(
                name="Login",
                passed=False,
                message="HTTP 401 — invalid credentials or session expired",
                elapsed=elapsed,
            )
        if r.status_code == 403:
            return CheckResult(
                name="Login",
                passed=False,
                message="HTTP 403 — access forbidden (check PIN/branch permissions)",
                elapsed=elapsed,
            )
        if not r.ok:
            return CheckResult(
                name="Login",
                passed=False,
                message=f"HTTP {r.status_code} from loginProc",
                detail=r.text[:300],
                elapsed=elapsed,
            )

        # Try to parse JSON result code
        try:
            body = r.json()
            rc   = str(body.get("resultCd", "000")).strip()
            msg  = body.get("resultMsg", "")
            if rc == "000":
                return CheckResult(
                    name="Login",
                    passed=True,
                    message=f"Login OK — resultCd=000  ({elapsed:.2f}s)",
                    elapsed=elapsed,
                )
            else:
                return CheckResult(
                    name="Login",
                    passed=False,
                    message=f"KRA rejected login — resultCd={rc}  msg={msg}",
                    elapsed=elapsed,
                )
        except ValueError:
            # Non-JSON 200 — session cookie is the real indicator
            if "JSESSIONID" in sess.cookies:
                return CheckResult(
                    name="Login",
                    passed=True,
                    message=f"Login appears OK (non-JSON 200, JSESSIONID present)  ({elapsed:.2f}s)",
                    elapsed=elapsed,
                )
            return CheckResult(
                name="Login",
                passed=False,
                message="Login POST returned 200 but no JSESSIONID and non-JSON body — outcome uncertain",
                detail=r.text[:300],
                elapsed=elapsed,
            )

    except requests.exceptions.Timeout:
        return CheckResult(
            name="Login",
            passed=False,
            message=f"Login POST timed out after {timeout}s",
            elapsed=time.perf_counter() - t0,
        )
    except requests.exceptions.ConnectionError as exc:
        return CheckResult(
            name="Login",
            passed=False,
            message=f"Connection lost during login POST: {exc}",
            elapsed=time.perf_counter() - t0,
        )


def check_sales_endpoint(sess: requests.Session,
                         timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """
    Check 7 – Probe the sales submission endpoint.

    We send a GET (not a live POST) just to verify the endpoint is routable
    and returns something other than a 404/502.  A 405 Method Not Allowed is
    actually a *good* sign — it means the endpoint exists.
    """
    t0  = time.perf_counter()
    url = f"{BASE_URL}{SALES_PATH}"
    try:
        r = sess.get(url, timeout=timeout, allow_redirects=False)
        elapsed = time.perf_counter() - t0
        # 200, 302, 400, 405 all mean the route exists
        if r.status_code in (200, 302, 400, 401, 403, 405):
            return CheckResult(
                name="Sales endpoint probe",
                passed=True,
                message=f"Sales endpoint reachable — HTTP {r.status_code}  ({elapsed:.2f}s)",
                elapsed=elapsed,
            )
        if r.status_code in (502, 503, 504):
            return CheckResult(
                name="Sales endpoint probe",
                passed=False,
                message=f"Sales endpoint returned HTTP {r.status_code} — backend likely down",
                elapsed=elapsed,
            )
        return CheckResult(
            name="Sales endpoint probe",
            passed=False,
            message=f"Unexpected HTTP {r.status_code} from sales endpoint",
            detail=r.text[:200],
            elapsed=elapsed,
        )
    except requests.exceptions.Timeout:
        return CheckResult(
            name="Sales endpoint probe",
            passed=False,
            message=f"Sales endpoint timed out after {timeout}s",
            elapsed=time.perf_counter() - t0,
        )
    except requests.exceptions.ConnectionError as exc:
        return CheckResult(
            name="Sales endpoint probe",
            passed=False,
            message=f"Connection error probing sales endpoint: {exc}",
            elapsed=time.perf_counter() - t0,
        )


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

def run_health_check(
    *,
    full:     bool  = False,
    pin:      str   = "",
    username: str   = "",
    password: str   = "",
    timeout:  float = DEFAULT_TIMEOUT,
) -> HealthReport:
    """
    Run all checks and return a HealthReport.

    Parameters
    ----------
    full     : also run the login check (requires credentials)
    pin      : KRA PIN (read from KRA_PIN env var if empty)
    username : eTIMS username (read from KRA_USERNAME if empty)
    password : eTIMS password (read from KRA_PASSWORD if empty)
    timeout  : per-check timeout in seconds
    """
    report = HealthReport(timestamp=datetime.now().isoformat(timespec="seconds"))

    # ── 1. DNS ────────────────────────────────────────────────────────────────
    dns = check_dns(timeout=timeout)
    report.add(dns)
    if not dns.passed:
        report.site_up = False
        return report

    # ── 2. TCP ────────────────────────────────────────────────────────────────
    tcp = check_tcp(timeout=timeout)
    report.add(tcp)
    if not tcp.passed:
        report.site_up = False
        return report

    # ── 3. TLS ────────────────────────────────────────────────────────────────
    tls = check_tls(timeout=timeout)
    report.add(tls)
    if not tls.passed:
        report.site_up = False
        return report

    # ── 4. HTTP ───────────────────────────────────────────────────────────────
    http, sess = check_http(timeout=timeout)
    report.add(http)
    if not http.passed or sess is None:
        report.site_up = False
        return report

    # ── 5. Session cookie ─────────────────────────────────────────────────────
    cookie = check_session_cookie(sess, timeout=timeout)
    report.add(cookie)
    # Cookie missing is a warning, not a hard stop — carry on

    # ── 6. Login (optional) ───────────────────────────────────────────────────
    if full:
        _pin      = pin      or os.environ.get("KRA_PIN",      "")
        _username = username or os.environ.get("KRA_USERNAME",  "")
        _password = password or os.environ.get("KRA_PASSWORD",  "")

        if not _username or not _password:
            report.add(CheckResult(
                name="Login",
                passed=False,
                message="Skipped — KRA_USERNAME / KRA_PASSWORD env vars not set",
            ))
        else:
            login_result = check_login(sess, _pin, _username, _password, timeout=timeout)
            report.add(login_result)

            # ── 7. Sales endpoint probe (only if logged in) ──────────────────
            if login_result.passed:
                report.add(check_sales_endpoint(sess, timeout=timeout))

    # ── Final verdict ─────────────────────────────────────────────────────────
    mandatory = [c for c in report.checks
                 if c.name not in ("Session cookie",)]   # cookie is advisory only
    report.site_up = all(c.passed for c in mandatory)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# PRETTY PRINTER
# ─────────────────────────────────────────────────────────────────────────────

_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

def _tick(passed: bool) -> str:
    return f"{_GREEN}✅{_RESET}" if passed else f"{_RED}❌{_RESET}"


def print_report(report: HealthReport) -> None:
    print(f"\n{_BOLD}KRA eTIMS Health Check  —  {report.timestamp}{_RESET}")
    print("─" * 60)
    for c in report.checks:
        elapsed_str = f"  [{c.elapsed:.2f}s]" if c.elapsed is not None else ""
        print(f"  {_tick(c.passed)}  {c.name:<28} {c.message}{elapsed_str}")
        if c.detail:
            print(f"       {_YELLOW}↳ {c.detail}{_RESET}")
    print("─" * 60)
    if report.site_up:
        print(f"  {_GREEN}{_BOLD}RESULT: Site is UP and reachable ✅{_RESET}\n")
    else:
        print(f"  {_RED}{_BOLD}RESULT: Site is DOWN or unreachable ❌{_RESET}")
        # Give an actionable hint based on where the chain broke
        failed = next((c for c in report.checks if not c.passed), None)
        if failed:
            hints = {
                "DNS resolution":       "→ Check your internet connection or DNS settings.",
                "TCP reachability":     "→ etims.kra.go.ke port 443 is blocked or the server is offline.",
                "TLS handshake":        "→ TLS/certificate issue — the site may have an expired cert.",
                "HTTP reachability":    "→ The site is not responding over HTTPS. Try again in a few minutes.",
                "Session cookie":       "→ The portal returned a page but didn't set a session. Likely under maintenance.",
                "Login":                "→ Connectivity is OK but login failed. Check credentials or try later.",
                "Sales endpoint probe": "→ Login works but the sales endpoint is unavailable (backend may be down).",
            }
            hint = hints.get(failed.name, "")
            if hint:
                print(f"  {_YELLOW}{hint}{_RESET}")
        print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Health-check the KRA eTIMS portal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--full",    action="store_true",
                        help="Also test login and the sales endpoint (needs KRA_USERNAME/KRA_PASSWORD).")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Per-check timeout in seconds (default: {DEFAULT_TIMEOUT}).")
    parser.add_argument("--watch",   type=int, metavar="SECONDS",
                        help="Keep running every N seconds until the site is up.")
    parser.add_argument("--json",    action="store_true",
                        help="Output results as JSON (one object per run, newline-delimited).")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,     # suppress requests noise in normal output
        format="%(levelname)s  %(message)s",
    )

    def _run_once() -> HealthReport:
        report = run_health_check(
            full    = args.full,
            timeout = args.timeout,
        )
        if args.json:
            print(json.dumps(report.to_dict()))
        else:
            print_report(report)
        return report

    if args.watch:
        attempt = 0
        while True:
            attempt += 1
            if not args.json:
                print(f"\n[Attempt {attempt}]", end="")
            report = _run_once()
            if report.site_up:
                sys.exit(0)
            if not args.json:
                print(f"  ⏳ Retrying in {args.watch}s… (Ctrl-C to stop)")
            time.sleep(args.watch)
    else:
        report = _run_once()
        sys.exit(0 if report.site_up else 1)


if __name__ == "__main__":
    main()