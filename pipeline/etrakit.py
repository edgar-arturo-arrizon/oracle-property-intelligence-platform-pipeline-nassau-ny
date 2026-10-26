"""Playwright client for the Town of Oyster Bay eTRAKiT permit search.

One request at a time (~1/s), exponential backoff on transient errors, and a hard stop
(``PortalBlocked``) on any captcha, challenge, 403/429, or lockout wording. Credentials
come from ``.env`` only and never reach logs, saved HTML, or exceptions (see etrakit_session).
"""
from __future__ import annotations

import re
import time
from collections import Counter

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from . import etrakit_parse as P
from .etrakit_session import BASE, Credentials, LoginFailed, Pacer, PortalBlocked

SEARCH_URL = BASE + "Search/permit.aspx"
LOGIN_URL = BASE + "login.aspx?lt=either&rd=~/Search/permit.aspx"
FIELD_SBL = "Permit_Main.SITE_APN"
FIELD_PERMIT = "Permit_Main.PERMIT_NO"
GRID = "#ctl00_cplMain_rgSearchRslts"

# Strong block signals only, matched against *visible* text (not page source: the page always
# loads Google's reCAPTCHA script, which is not a challenge).
STRONG_BLOCK_RX = re.compile(
    r"verify (that )?you are (a )?human|are you a robot|complete the captcha|solve the captcha|"
    r"just a moment|checking your browser|access denied|you have been blocked|too many requests|"
    r"too many (login )?attempts|account (has been |is )?(temporarily )?locked|locked out|"
    r"unusual traffic|rate limit exceeded", re.I)


class HarvestError(RuntimeError):
    """Transient failures exhausted their retries for one action."""


class EtrakitClient:
    def __init__(self, creds: Credentials | None = None, headless: bool = True, max_logins: int = 3):
        self.creds = creds or Credentials()
        self.pacer = Pacer()
        self.headless = headless
        self.max_logins = max_logins
        self.logins = 0
        self.requests: Counter = Counter()      # portal document/xhr/fetch requests by phase
        self.statuses: Counter = Counter()
        self.phase = "login"
        self._flag: str | None = None
        self._pw = self._browser = self.page = None
        self._field = None
        self._oper = None
        self._page_no = 1

    # ---------------------------------------------------------------- lifecycle
    def __enter__(self) -> "EtrakitClient":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.page = self._browser.new_context().new_page()
        self.page.set_default_timeout(60000)
        # politeness: no images, fonts or media
        self.page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "font", "media") else r.continue_())
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.login()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _on_request(self, req) -> None:
        if "aspgov.com" in req.url and req.resource_type in ("document", "xhr", "fetch"):
            self.requests[self.phase] += 1

    def _on_response(self, resp) -> None:
        if "aspgov.com" in resp.url and resp.request.resource_type in ("document", "xhr", "fetch"):
            self.statuses[resp.status] += 1
            if resp.status in (403, 429):
                self._flag = f"HTTP {resp.status} from portal ({self.phase})"

    # ---------------------------------------------------------------- guards
    def guard(self, where: str) -> None:
        if self._flag:
            raise PortalBlocked(self._flag)
        page = self.page
        body = page.inner_text("body")
        m = STRONG_BLOCK_RX.search(body)
        if m:
            raise PortalBlocked(f"block wording '{m.group(0)}' at {where}")
        if page.locator("#cplMain_UpdatePanelreCAPTCHA").count() and page.inner_html("#cplMain_UpdatePanelreCAPTCHA").strip():
            raise PortalBlocked(f"reCAPTCHA panel populated at {where}")
        if page.locator("iframe[src*='recaptcha'], iframe[title*='reCAPTCHA' i], iframe[src*='challenges.cloudflare']").count():
            raise PortalBlocked(f"captcha/challenge iframe at {where}")

    def _act(self, label: str, fn, retries: int = 3):
        """Run one portal action with pacing, a block guard, and exponential backoff."""
        last = None
        for attempt in range(retries + 1):
            self.pacer.wait()
            try:
                out = fn()
                self.guard(label)
                return out
            except (PortalBlocked, LoginFailed):
                raise
            except (PWTimeout, PWError) as exc:
                last = exc
                self.guard(label + " (after error)")
                time.sleep(2.0 * (2 ** attempt))
        raise HarvestError(f"{label}: {self.creds.scrub(str(last))[:200]}")

    # ---------------------------------------------------------------- login
    def login(self) -> None:
        if self.logins >= self.max_logins:
            raise LoginFailed("login limit for this run reached (no more attempts)")
        self.logins += 1
        self.phase = "login"
        page = self.page
        self._act("login page", lambda: page.goto(LOGIN_URL, wait_until="domcontentloaded"))
        self._act("fill user", lambda: page.fill("#cplMain_txtPublicUserName", self.creds.username))
        self._act("fill password", lambda: page.fill("#cplMain_txtPublicPassword", self.creds.password))
        self._act("submit login", lambda: (page.click("#cplMain_btnPublicLogin"), page.wait_for_load_state("domcontentloaded")))
        time.sleep(1.5)
        body = page.inner_text("body")
        if "Invalid Login" in body:
            raise LoginFailed("portal answered Invalid Login (no retry)")
        m = re.search(r"LOGGED IN AS:\s*([^\n]+)", body)
        if m:  # the account holder's display name is personal data: scrub it from everything saved
            self.creds._secrets.append(m.group(1).strip())
        if "Search/permit.aspx" not in page.url:
            self._act("open permit search", lambda: page.goto(SEARCH_URL, wait_until="domcontentloaded"))
        self.guard("after login")
        self._field = self._oper = None

    def _ensure_session(self) -> None:
        if "login.aspx" in self.page.url.lower():
            self.login()

    # ---------------------------------------------------------------- search
    def _post(self, trigger, label: str, timeout: int = 90000, settle: bool = True):
        """Fire an action that causes one UpdatePanel postback; wait for it (and, if ``settle``, network idle)."""
        def run():
            with self.page.expect_response(lambda r: r.request.method == "POST" and "permit.aspx" in r.url, timeout=timeout):
                trigger()
            if settle:
                self.page.wait_for_load_state("networkidle", timeout=timeout)
        return self._act(label, run)

    _SIG_JS = """() => { const g = document.getElementById('ctl00_cplMain_rgSearchRslts');
        const rows = g ? [...g.querySelectorAll('tr.rgRow, tr.rgAltRow')].map(r => r.innerText.replace(/\\s+/g, ' ')).join('|') : '';
        const m = document.getElementById('cplMain_lblMoreResults');
        const pg = (document.body.innerText.match(/page\\s+\\d+\\s+of\\s+\\d+/i) || [''])[0];
        return rows + '#' + (m ? m.innerText : '') + '#' + pg; }"""

    def _grid_sig(self) -> str:
        return self.page.evaluate(self._SIG_JS)

    def _wait_results(self, expect_page: int | None = None, prev_sig: str | None = None) -> None:
        """Wait until the grid shows NEW results: rows that differ from ``prev_sig`` (stale grids linger after a
        new search), on the expected page, or the portal's "no results" text."""
        js = """([expectPage, prevSig, sigFn]) => {
            const txt = document.body.innerText;
            if (/no results to match your search/i.test(txt)) return true;
            const g = document.getElementById('ctl00_cplMain_rgSearchRslts');
            if (!(g && g.querySelectorAll('tr.rgRow, tr.rgAltRow').length > 0)) return false;
            if (expectPage) { const m = txt.match(/page\\s+(\\d+)\\s+of\\s+\\d+/i); if (!(m && parseInt(m[1], 10) === expectPage)) return false; }
            if (prevSig !== null && prevSig !== undefined) { return (eval('(' + sigFn + ')'))() !== prevSig; }
            return true; }"""
        def run():
            try:
                self.page.wait_for_function(js, arg=[expect_page, prev_sig, self._SIG_JS], timeout=60000 if prev_sig is None else 25000)
            except PWTimeout:
                if prev_sig is None:
                    raise
                # identical results to the previous query (e.g. a repeated search): accept the current grid
        self._act("wait for results", run)

    def grid_snapshot(self) -> tuple[str, dict]:
        """Raw grid HTML (scrubbed) plus parsed rows/counts; the count message is read before any row click."""
        page = self.page
        grid = page.evaluate("() => { const g = document.getElementById('ctl00_cplMain_rgSearchRslts'); return g ? g.outerHTML : ''; }")
        msg = page.evaluate("() => { const m = document.getElementById('cplMain_lblMoreResults'); return m ? m.outerHTML : ''; }")
        raw = self.creds.scrub(msg + "\n" + grid)
        parsed = P.parse_grid(raw)
        return raw, parsed

    def search(self, field: str, oper: str, value: str) -> tuple[str, dict]:
        """Run one search; returns the raw grid HTML and its parsed first page."""
        self._ensure_session()
        self.phase = "search"
        page = self.page
        if self._field != field:
            self._post(lambda: page.select_option("#cplMain_ddSearchBy", field), "set search-by")
            self._field, self._oper = field, None
        if self._oper != oper:
            self._act("set operator", lambda: page.select_option("#cplMain_ddSearchOper", oper))
            self._oper = oper
        self._act("fill value", lambda: page.fill("#cplMain_txtSearchString", value))
        prev = self._grid_sig()
        self._post(lambda: page.click("#ctl00_cplMain_btnSearch"), f"search {value}")
        self._wait_results(prev_sig=prev)
        raw, g = self.grid_snapshot()
        # the grid keeps its page index across searches (it opens on the last page if the previous query ended
        # deeper than this one has): always start from page 1 so no first-page rows are skipped
        if g["page"] and g["page"] > 1:
            self.phase = "page"
            self._post(lambda: self.page.evaluate("changePage('first')"), "first page")
            self._wait_results(expect_page=1)
            raw, g = self.grid_snapshot()
        self._page_no = g["page"] or 1
        return raw, g

    def next_page(self) -> tuple[str, dict]:
        self.phase = "page"
        self._post(lambda: self.page.evaluate("changePage('next')"), "next page")
        self._page_no += 1
        self._wait_results(expect_page=self._page_no)
        return self.grid_snapshot()

    def last_page(self) -> tuple[str, dict]:
        self.phase = "page"
        self._post(lambda: self.page.evaluate("changePage('last')"), "last page")
        self._wait_results()
        raw, g = self.grid_snapshot()
        self._page_no = g["page"] or self._page_no
        return raw, g

    # ---------------------------------------------------------------- detail
    def open_detail(self, row_index: int, permit_no: str | None = None) -> str:
        """Click a grid row; returns the scrubbed detail-panel HTML (all tabs arrive in one response)."""
        self.phase = "detail"
        page = self.page
        # no networkidle here: the targeted waits below (panel is for this permit, tabs/rows rendered) are the settle signal
        self._post(lambda: page.click(f"{GRID}_ctl00__{row_index} td:first-child"), f"open row {row_index}", settle=False)
        if permit_no:  # the panel's site-address link carries ActivityNo=<permit>; confirm it is the clicked permit
            try:
                page.wait_for_function("(no) => { const a = document.querySelector(\"[id$='hlSiteAddress']\"); "
                                       "return !!a && (a.getAttribute('onclick') || '').includes('ActivityNo=' + no); }",
                                       arg=permit_no, timeout=8000)
            except PWTimeout:
                pass  # verification in the harvester records a mismatch if the panel is stale
        # tabs and grids render client-side shortly after the postback: wait until each tab's
        # "(N)" count equals the number of rows actually rendered (best effort; parser flags a gap)
        try:
            page.wait_for_function("""() => {
                const root = document.getElementById('divSearchDetails'); if (!root) return false;
                const labs = [...root.querySelectorAll('.rtsTxt')].map(e => e.textContent.trim());
                if (labs.length < 5) return false;
                const want = (re) => { const l = labs.find(x => re.test(x)); const m = l && l.match(/\\((\\d+)\\)/); return m ? parseInt(m[1], 10) : 0; };
                const have = (frag) => root.querySelectorAll(`tr[id*='${frag}_ctl00__']`).length;
                return have('rgContactInfo') >= want(/^Contacts/) && have('rgInspectionInfo') >= want(/^Inspections/); }""", timeout=20000)
        except PWTimeout:
            pass
        html = page.inner_html("#divSearchDetails")   # tab strip + every pane (Permit/Site/Contacts/Fees/Inspections/Conditions)
        return self.creds.scrub(html)
