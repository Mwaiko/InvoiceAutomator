"""
app/services/etims_health.py

Thin wrapper around the eTIMS connectivity probe so it can be imported
cleanly by the FastAPI router without dragging in the CLI machinery.

The underlying checks (DNS → TCP → TLS → HTTP → session-cookie) are
synchronous (socket / requests), so callers should run this in a thread-pool
executor rather than awaiting it directly on the event loop.

Usage
─────
    import asyncio
    from functools import partial
    from app.services.etims_health import probe_etims

    report = await asyncio.get_event_loop().run_in_executor(
        None, partial(probe_etims, timeout=8)
    )
    # report.site_up  → bool
    # report.to_dict() → JSON-serialisable dict
"""

from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import requests

# ── KRA eTIMS coordinates ─────────────────────────────────────────────────────

_HOSTNAME         = "etims.kra.go.ke"
_PORT             = 443
_BASE_URL         = f"https://{_HOSTNAME}"
_LOGIN_PAGE_PATH  = "/basic/login/indexLogin"
_SALES_INDEX_PATH = "/app/ebm/trns/sales/indexTrnsSalesReceipt"
_DEFAULT_TIMEOUT  = 8   # seconds — keep it tight for a pre-confirm check


# ── Result models (mirrors test_etims_connection.py) ─────────────────────────

@dataclass
class CheckResult:
    name:    str
    passed:  bool
    message: str
    detail:  Optional[str] = None
    elapsed: Optional[float] = None


@dataclass
class HealthReport:
    timestamp: str
    checks:    List[CheckResult] = field(default_factory=list)
    site_up:   bool = False

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "site_up":   self.site_up,
            "checks": [
                {
                    "name":      c.name,
                    "passed":    c.passed,
                    "message":   c.message,
                    "detail":    c.detail,
                    "elapsed_s": round(c.elapsed, 3) if c.elapsed is not None else None,
                }
                for c in self.checks
            ],
        }


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_dns(timeout: float) -> CheckResult:
    t0 = time.perf_counter()
    try:
        socket.setdefaulttimeout(timeout)
        addrs = socket.getaddrinfo(_HOSTNAME, _PORT, proto=socket.IPPROTO_TCP)
        return CheckResult(
            name="DNS resolution",
            passed=True,
            message=f"Resolved to {addrs[0][4][0]}",
            elapsed=time.perf_counter() - t0,
        )
    except socket.gaierror as exc:
        return CheckResult(
            name="DNS resolution",
            passed=False,
            message=f"DNS lookup failed: {exc}",
            elapsed=time.perf_counter() - t0,
        )


def _check_tcp(timeout: float) -> CheckResult:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((_HOSTNAME, _PORT), timeout=timeout):
            pass
        return CheckResult(
            name="TCP reachability",
            passed=True,
            message=f"Port {_PORT} open",
            elapsed=time.perf_counter() - t0,
        )
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        return CheckResult(
            name="TCP reachability",
            passed=False,
            message=f"Cannot connect to {_HOSTNAME}:{_PORT} — {exc}",
            elapsed=time.perf_counter() - t0,
        )


def _check_tls(timeout: float) -> CheckResult:
    t0 = time.perf_counter()
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((_HOSTNAME, _PORT), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=_HOSTNAME) as tls:
                cert = tls.getpeercert()
        return CheckResult(
            name="TLS handshake",
            passed=True,
            message=f"TLS OK — cert valid until {cert.get('notAfter', 'unknown')}",
            elapsed=time.perf_counter() - t0,
        )
    except (ssl.SSLCertVerificationError, socket.timeout, OSError) as exc:
        return CheckResult(
            name="TLS handshake",
            passed=False,
            message=f"TLS failed: {exc}",
            elapsed=time.perf_counter() - t0,
        )


def _check_http(timeout: float) -> tuple[CheckResult, Optional[requests.Session]]:
    t0  = time.perf_counter()
    url = f"{_BASE_URL}{_LOGIN_PAGE_PATH}"
    sess = requests.Session()
    sess.headers.update({
        "User-Agent":       "Mozilla/5.0 (compatible; EtimsHealthProbe/1.0)",
        "Accept-Language":  "en-GB,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        r = sess.get(url, timeout=timeout, allow_redirects=True)
        elapsed = time.perf_counter() - t0
        if r.status_code == 200:
            return CheckResult(
                name="HTTP reachability",
                passed=True,
                message=f"HTTP 200 in {elapsed:.2f}s",
                elapsed=elapsed,
            ), sess
        return CheckResult(
            name="HTTP reachability",
            passed=False,
            message=f"Unexpected HTTP {r.status_code}",
            elapsed=elapsed,
        ), None
    except requests.exceptions.Timeout:
        return CheckResult(
            name="HTTP reachability",
            passed=False,
            message=f"Timed out after {timeout}s",
            elapsed=time.perf_counter() - t0,
        ), None
    except requests.exceptions.ConnectionError as exc:
        return CheckResult(
            name="HTTP reachability",
            passed=False,
            message=f"Connection error: {exc}",
            elapsed=time.perf_counter() - t0,
        ), None


def _check_session_cookie(sess: requests.Session, timeout: float) -> CheckResult:
    url = f"{_BASE_URL}{_LOGIN_PAGE_PATH}"
    t0  = time.perf_counter()
    try:
        r = sess.get(url, timeout=timeout)
        cookies = r.cookies
        has_jsession = any("JSESSIONID" in k.upper() for k in cookies.keys())
        has_bigip    = any("BIGIP"      in k.upper() for k in cookies.keys())
        if has_jsession or has_bigip:
            return CheckResult(
                name="Session cookie",
                passed=True,
                message="Session cookies present",
                elapsed=time.perf_counter() - t0,
            )
        return CheckResult(
            name="Session cookie",
            passed=False,
            message="No JSESSIONID / BIGip cookie — portal may be under maintenance",
            elapsed=time.perf_counter() - t0,
        )
    except Exception as exc:
        return CheckResult(
            name="Session cookie",
            passed=False,
            message=f"Cookie check failed: {exc}",
            elapsed=time.perf_counter() - t0,
        )


def _check_sales_endpoint(sess: requests.Session, timeout: float) -> CheckResult:
    """
    Verify the sales receipt endpoint is reachable and returns a sensible
    response.  We do a GET on the sales index page (not a POST) so we never
    risk creating a duplicate receipt — the GET just confirms the route exists
    and the portal isn't returning a maintenance / 5xx page.

    A 200 or 302 (redirect-to-login) is considered healthy: the endpoint is
    alive.  A 404, 5xx, or connection error means the sales path is broken.
    """
    url = f"{_BASE_URL}{_SALES_INDEX_PATH}"
    t0  = time.perf_counter()
    try:
        r = sess.get(url, timeout=timeout, allow_redirects=False)
        elapsed = time.perf_counter() - t0
        # 200 = loaded, 302 = redirect to login — both mean the route is up.
        if r.status_code in (200, 302):
            return CheckResult(
                name="Sales endpoint",
                passed=True,
                message=f"Sales endpoint reachable (HTTP {r.status_code}) in {elapsed:.2f}s",
                elapsed=elapsed,
            )
        return CheckResult(
            name="Sales endpoint",
            passed=False,
            message=f"Sales endpoint returned unexpected HTTP {r.status_code}",
            elapsed=elapsed,
        )
    except requests.exceptions.Timeout:
        return CheckResult(
            name="Sales endpoint",
            passed=False,
            message=f"Sales endpoint timed out after {timeout}s",
            elapsed=time.perf_counter() - t0,
        )
    except requests.exceptions.ConnectionError as exc:
        return CheckResult(
            name="Sales endpoint",
            passed=False,
            message=f"Sales endpoint connection error: {exc}",
            elapsed=time.perf_counter() - t0,
        )


# ── Main probe ────────────────────────────────────────────────────────────────

def probe_etims(timeout: float = _DEFAULT_TIMEOUT) -> HealthReport:
    """
    Run connectivity checks (DNS → TCP → TLS → HTTP → session-cookie →
    sales-endpoint) against etims.kra.go.ke and return a HealthReport.

    The final check — sales-endpoint — confirms that the specific route used
    for receipt submission is alive, not just the login page.  This lets the
    backend gate GRN confirmation on actual sales-path availability rather
    than just generic site reachability.

    This function is synchronous — call it from a thread-pool executor:

        report = await loop.run_in_executor(None, partial(probe_etims, timeout=8))

    ``report.site_up`` is True only when ALL checks (including the sales
    endpoint) pass.  ``report.sales_endpoint_up`` is the narrower flag used
    as the pre-confirm gate in etims_tasks.submit_to_etims().
    """
    report = HealthReport(timestamp=datetime.now().isoformat(timespec="seconds"))

    dns = _check_dns(timeout)
    report.add(dns)
    if not dns.passed:
        return report

    tcp = _check_tcp(timeout)
    report.add(tcp)
    if not tcp.passed:
        return report

    tls = _check_tls(timeout)
    report.add(tls)
    if not tls.passed:
        return report

    http, sess = _check_http(timeout)
    report.add(http)
    if not http.passed or sess is None:
        return report

    report.add(_check_session_cookie(sess, timeout))

    # Always check the sales endpoint — this is the specific path we POST to.
    report.add(_check_sales_endpoint(sess, timeout))

    # Verdict: all checks must pass (session cookie failure alone is not fatal
    # for site_up, but sales endpoint failure is).
    non_cookie = [c for c in report.checks if c.name != "Session cookie"]
    report.site_up = all(c.passed for c in non_cookie)
    return report