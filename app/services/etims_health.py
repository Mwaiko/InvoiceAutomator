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
_LOGIN_POST_PATH  = "/basic/login/loginProc"
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


def _check_login(
    sess: requests.Session,
    username: str,
    password: str,
    timeout: float,
) -> CheckResult:
    """
    Authenticate against /basic/login/loginProc using the same two-step flow
    as fill_kra.login():

      1. GET  /basic/login/indexLogin  → seeds JSESSIONID + BIGip cookies
      2. POST /basic/login/loginProc   → submits mbrId / mbrPwd

    A successful login is confirmed by the presence of JSESSIONID in the
    session cookies AND resultCd == "000" in the JSON response body.
    """
    t0 = time.perf_counter()

    # Step 1: seed the session cookie (same as fill_kra.login step 1)
    seed_url = f"{_BASE_URL}{_LOGIN_PAGE_PATH}"
    try:
        r0 = sess.get(seed_url, timeout=timeout)
        if r0.status_code != 200:
            return CheckResult(
                name="eTIMS login",
                passed=False,
                message=f"Login page returned HTTP {r0.status_code} (expected 200)",
                elapsed=time.perf_counter() - t0,
            )
    except requests.exceptions.Timeout:
        return CheckResult(
            name="eTIMS login",
            passed=False,
            message=f"Login page timed out after {timeout}s",
            elapsed=time.perf_counter() - t0,
        )
    except requests.exceptions.ConnectionError as exc:
        return CheckResult(
            name="eTIMS login",
            passed=False,
            message=f"Login page connection error: {exc}",
            elapsed=time.perf_counter() - t0,
        )

    # Step 2: POST credentials (mirrors fill_kra.login step 2 exactly)
    login_url = f"{_BASE_URL}{_LOGIN_POST_PATH}"
    hdrs = {
        "Content-Type":   "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept":         "application/json, text/javascript, */*; q=0.01",
        "Origin":         _BASE_URL,
        "Referer":        seed_url,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    try:
        r = sess.post(
            login_url,
            data={"mbrId": username, "mbrPwd": password},
            headers=hdrs,
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return CheckResult(
            name="eTIMS login",
            passed=False,
            message=f"Login POST timed out after {timeout}s",
            elapsed=time.perf_counter() - t0,
        )
    except requests.exceptions.ConnectionError as exc:
        return CheckResult(
            name="eTIMS login",
            passed=False,
            message=f"Login POST connection error: {exc}",
            elapsed=time.perf_counter() - t0,
        )

    elapsed = time.perf_counter() - t0

    # JSESSIONID must be present after a successful login
    if "JSESSIONID" not in sess.cookies:
        return CheckResult(
            name="eTIMS login",
            passed=False,
            message="Login POST succeeded but no JSESSIONID cookie — check credentials",
            elapsed=elapsed,
        )

    # Check KRA's own result code in the JSON body
    try:
        body = r.json()
        rc = str(body.get("resultCd", "000")).strip()
        if rc != "000":
            return CheckResult(
                name="eTIMS login",
                passed=False,
                message=f"KRA rejected login: resultCd={rc} msg={body.get('resultMsg', 'n/a')}",
                elapsed=elapsed,
            )
    except ValueError:
        pass  # Non-JSON body — JSESSIONID presence is sufficient evidence

    jsession_prefix = sess.cookies["JSESSIONID"][:8]
    return CheckResult(
        name="eTIMS login",
        passed=True,
        message=f"Login OK — JSESSIONID={jsession_prefix}… in {elapsed:.2f}s",
        elapsed=elapsed,
    )


def _check_sales_endpoint(sess: requests.Session, timeout: float) -> CheckResult:
    """
    Verify the sales receipt endpoint is reachable with an authenticated
    session.  The session must already be logged in (via _check_login) before
    calling this — otherwise the portal returns 551 for unauthenticated requests.

    A 200 is the only fully-healthy response for an authenticated session.
    A 302 (redirect back to login) means the session was not accepted.
    """
    url = f"{_BASE_URL}{_SALES_INDEX_PATH}"
    t0  = time.perf_counter()
    try:
        r = sess.get(url, timeout=timeout, allow_redirects=False)
        elapsed = time.perf_counter() - t0
        if r.status_code == 200:
            return CheckResult(
                name="Sales endpoint",
                passed=True,
                message=f"Sales endpoint reachable (HTTP 200) in {elapsed:.2f}s",
                elapsed=elapsed,
            )
        if r.status_code == 302:
            return CheckResult(
                name="Sales endpoint",
                passed=False,
                message="Sales endpoint redirected to login — session was not accepted",
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

def probe_etims(
    timeout:  float = _DEFAULT_TIMEOUT,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> HealthReport:
    """
    Run connectivity checks (DNS → TCP → TLS → HTTP → session-cookie →
    login → sales-endpoint) against etims.kra.go.ke and return a HealthReport.

    When ``username`` and ``password`` are supplied the probe performs a real
    login (POST /basic/login/loginProc) before hitting the sales endpoint, which
    is the only reliable way to confirm the authenticated sales path is healthy.
    Without credentials the probe stops after the session-cookie check and
    ``site_up`` reflects basic reachability only.

    Credentials should be passed from env-vars — never hard-coded:

        import os
        from functools import partial
        report = await loop.run_in_executor(
            None,
            partial(
                probe_etims,
                timeout=8,
                username=os.environ.get("KRA_USERNAME"),
                password=os.environ.get("KRA_PASSWORD"),
            ),
        )

    ``report.site_up`` is True only when ALL executed checks pass (including
    the authenticated sales-endpoint check when credentials are provided).
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

    # ── Authenticated checks (only when credentials are available) ────────────
    if username and password:
        login_result = _check_login(sess, username, password, timeout)
        report.add(login_result)
        if login_result.passed:
            # Session is now authenticated — sales endpoint should return 200.
            report.add(_check_sales_endpoint(sess, timeout))
        else:
            # Login failed: record the sales endpoint as skipped/failed.
            report.add(CheckResult(
                name="Sales endpoint",
                passed=False,
                message="Skipped — login did not succeed",
            ))
    else:
        # No credentials supplied: connectivity-only probe.
        # The unauthenticated GET returns 551, so we skip the sales check
        # and note that credentials are required for the full probe.
        report.add(CheckResult(
            name="Sales endpoint",
            passed=False,
            message="Skipped — provide KRA_USERNAME / KRA_PASSWORD for authenticated check",
        ))

    # Verdict: all checks must pass.  A skipped sales endpoint is treated as
    # failed so callers must supply credentials for a full green report.
    report.site_up = all(c.passed for c in report.checks)
    return report