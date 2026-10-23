"""The testing agent: explores a site, plans tests from what it finds, runs them in a real browser, and reports bugs.

How it "decides" what to test: it looks at each page's structure, recognises forms by their fields (a signup form has a
password and a confirmation or name field, a login form has a lone password, a search form has a search box) and writes a
set of test cases for each kind. It follows rules, not a language model. Replacing the planner with a model that reads the page
is possible, but every test below would still need the same browser, oracles, safety check and report.

Safety: anything that looks destructive (delete, pay, log out ...) is never clicked unless an approval function says yes.
The default function says no.
"""
from __future__ import annotations
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, sync_playwright

from . import oracles

XSS_PROBE = '<b id="xss-probe">probe</b>'
FLAKY_RUNS = 8


@dataclass
class Finding:
    id: str
    oracle: str
    severity: str
    title: str
    page: str
    expected: str
    actual: str
    steps: list[dict] = field(default_factory=list)
    check: dict = field(default_factory=dict)          # how to re-check it in the repro script
    screenshot: str = ""
    console: list[str] = field(default_factory=list)

    def signature(self) -> str:
        return f"{self.oracle}|{self.page}|{self.check.get('case', '')}"


def deny_all(label: str, url: str) -> bool:
    return False


class Agent:
    def __init__(self, base_url: str, out_dir: str = "outputs/run", approve: Callable[[str, str], bool] = deny_all, headless: bool = True):
        self.base = base_url.rstrip("/")
        self.out = out_dir
        self.approve = approve
        self.headless = headless
        self.findings: list[Finding] = []
        self.log: list[str] = []
        self.skipped_risky: list[str] = []
        self.clicked_risky: list[str] = []
        os.makedirs(os.path.join(out_dir, "shots"), exist_ok=True)

    # ------------------------------------------------------------------ session
    def run(self, goals=("crawl", "search", "signup", "login")) -> dict:
        t0 = time.time()
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=self.headless)
            self.browser = browser
            pages = self._crawl(browser) if "crawl" in goals else {"/": None}
            forms = self._find_forms(browser, pages)
            if "search" in goals and "search" in forms:
                self._test_search(browser, forms["search"])
            if "signup" in goals and "signup" in forms:
                self._test_signup(browser, forms["signup"], forms.get("login"))
            if "login" in goals and "login" in forms and "signup" in forms:
                self._test_login(browser, forms["login"], forms["signup"])
            browser.close()
        return dict(base=self.base, seconds=round(time.time() - t0, 1), pages=len(pages), forms={k: v["path"] for k, v in forms.items()},
                    findings=[asdict(f) for f in self.findings], skipped_risky=self.skipped_risky,
                    clicked_risky=self.clicked_risky, log=self.log)

    def _new_page(self, browser):
        ctx = browser.new_context()
        page = ctx.new_page()
        page._errors, page._statuses = [], []
        def on_console(m):
            url = m.location.get("url", "")
            if m.type != "error" or not oracles.relevant_console_error(m.text, url):
                return
            if m.text.startswith("Failed to load resource") and url == page.url:
                return                      # the page itself returned an error status: that is reported as a status problem, not a console error
            page._errors.append(m.text)
        page.on("console", on_console)
        page.on("pageerror", lambda e: page._errors.append(f"uncaught: {e}"))
        page.on("response", lambda r: page._statuses.append((r.status, r.url)) if r.url.startswith(self.base) else None)
        return ctx, page

    def _shot(self, page: Page, name: str) -> str:
        path = os.path.join(self.out, "shots", f"{name}-{uuid.uuid4().hex[:6]}.png")
        try:
            page.screenshot(path=path)
        except Exception:
            return ""
        return path

    def _add(self, **kw) -> Finding:
        f = Finding(id=f"F{len(self.findings) + 1:02d}", severity=oracles.SEVERITY.get(kw["oracle"], "medium"), **kw)
        if any(x.signature() == f.signature() for x in self.findings):
            return f
        self.findings.append(f)
        return f

    # ------------------------------------------------------------------ crawl and per-page checks
    def _crawl(self, browser, max_pages: int = 12) -> dict:
        seen, queue, pages = set(), ["/"], {}
        ctx, page = self._new_page(browser)
        while queue and len(pages) < max_pages:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            page._errors.clear(); page._statuses.clear()
            t0 = time.perf_counter()
            resp = page.goto(self.base + path, wait_until="load", timeout=30000)
            took = time.perf_counter() - t0
            page.wait_for_timeout(150)
            if resp is None or resp.status >= 400:
                self.log.append(f"page {path} returned {resp.status if resp else 'nothing'}")
            pages[path] = dict(status=resp.status if resp else None, seconds=took)
            steps = [dict(op="goto", path=path)]
            if resp is not None and resp.status >= 500:
                self._add(oracle="server_error", title=f"Server error on {path}", page=path, expected="a page", actual=f"HTTP {resp.status}",
                          steps=steps, check=dict(type="status5xx", path=path, case="load"), screenshot=self._shot(page, "5xx"))
            if oracles.is_slow(took):
                self._add(oracle="slow_response", title=f"{path} took {took:.1f}s to load", page=path, expected=f"under {oracles.SLOW_SECONDS}s",
                          actual=f"{took:.1f}s", steps=steps, check=dict(type="slow", path=path, budget=oracles.SLOW_SECONDS, case="load"))
            if page._errors:
                self._add(oracle="console_error", title=f"JavaScript error on {path}", page=path, expected="no errors in the browser console",
                          actual=page._errors[0][:200], steps=steps, check=dict(type="console_error", path=path, case="load"),
                          screenshot=self._shot(page, "console"), console=page._errors[:5])
            for sel in self._unlabelled_controls(page):
                self._add(oracle="missing_accessible_name", title=f"Form field without a label on {path}", page=path,
                          expected="every input has a label or aria-label", actual=f"{sel} has no accessible name", steps=steps,
                          check=dict(type="no_accessible_name", path=path, selector=sel, case=sel))
            # links: collect, then check each same-origin target
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.getAttribute('href'), e.innerText.trim()])")
            for href, text in hrefs:
                absu = urljoin(self.base + path, href)
                if urlparse(absu).netloc != urlparse(self.base).netloc:
                    continue
                p2 = urlparse(absu).path or "/"
                if p2 not in seen:
                    queue.append(p2)
                st = ctx.request.get(absu).status
                if st >= 400:
                    self._add(oracle="broken_link", title=f"Link '{text or href}' on {path} leads to HTTP {st}", page=path, expected="a working page",
                              actual=f"{absu} returned {st}", steps=[dict(op="goto", path=path)], check=dict(type="link_broken", url=absu, path=p2, case=p2))
            self._smoke_click_buttons(page, path)
        ctx.close()
        return pages

    def _unlabelled_controls(self, page: Page) -> list[str]:
        js = """() => [...document.querySelectorAll('input, select, textarea')].filter(e => {
            const t = (e.type || '').toLowerCase();
            if (['hidden','submit','button','reset','image'].includes(t)) return false;
            if (e.getAttribute('aria-label') || e.getAttribute('aria-labelledby') || e.getAttribute('title')) return false;
            if (e.id && document.querySelector('label[for="' + e.id + '"]')) return false;
            if (e.closest('label')) return false;
            return true;
        }).map(e => e.id ? '#' + e.id : (e.name ? '[name="' + e.name + '"]' : e.tagName.toLowerCase()))"""
        return page.evaluate(js)

    def _smoke_click_buttons(self, page: Page, path: str):
        """Click plain buttons to see if anything breaks. Risky-looking ones are skipped unless a human approves."""
        for b in page.locator("button[type=button]").all():
            label = (b.inner_text() or "").strip()
            if oracles.is_risky(label) and not self.approve(label, self.base + path):
                self.skipped_risky.append(f"{label} on {path}")
                continue
            if oracles.is_risky(label):
                self.clicked_risky.append(f"{label} on {path}")
            try:
                b.click(timeout=2000)
            except Exception:
                pass

    # ------------------------------------------------------------------ form discovery
    def _find_forms(self, browser, pages) -> dict:
        found = {}
        ctx, page = self._new_page(browser)
        for path in pages:
            page.goto(self.base + path, wait_until="load")
            info = page.evaluate("""() => [...document.forms].map((f, i) => ({
                i, action: f.getAttribute('action') || location.pathname, method: f.method,
                inputs: [...f.querySelectorAll('input, textarea, select')].map(e => ({type: e.type, name: e.name, id: e.id})),
                button: !!f.querySelector('[type=submit], button:not([type=button])')}))""")
            for f in info:
                kind = self._form_kind(f["inputs"])
                if kind and kind not in found:
                    found[kind] = dict(path=path, index=f["i"], inputs=f["inputs"])
        ctx.close()
        return found

    @staticmethod
    def _form_kind(inputs) -> str | None:
        pw = [i for i in inputs if i["type"] == "password"]
        names = " ".join((i["name"] + " " + i["id"]).lower() for i in inputs)
        if any(i["type"] == "search" for i in inputs) or re.search(r"\b(q|query|search)\b", names) and not pw:
            return "search"
        if pw and (len(pw) >= 2 or re.search(r"(full)?name", names.replace("email", ""))):
            return "signup"
        if len(pw) == 1:
            return "login"
        return None

    @staticmethod
    def _field_selectors(form) -> dict:
        sel = {}
        for i in form["inputs"]:
            ident = f"#{i['id']}" if i["id"] else f"[name='{i['name']}']"
            n = (i["name"] + i["id"]).lower()
            if i["type"] == "password":
                sel.setdefault("password", ident) if "password" not in sel else sel.setdefault("confirm", ident)
            elif i["type"] == "checkbox":
                sel.setdefault("terms", ident)
            elif i["type"] == "email" or "email" in n:
                sel.setdefault("email", ident)
            elif "name" in n:
                sel.setdefault("name", ident)
            elif i["type"] == "search" or i["name"].lower() in ("q", "query", "search") or i["id"].lower() in ("q", "query", "search"):
                sel.setdefault("search", ident)
        return sel

    # ------------------------------------------------------------------ actions that are recorded as steps
    def _do(self, page: Page, steps: list, op: str, **kw):
        steps.append(dict(op=op, **kw))
        if op == "goto":
            page.goto(self.base + kw["path"], wait_until="load")
        elif op == "fill":
            page.fill(kw["selector"], kw["value"])
        elif op == "check":
            page.check(kw["selector"])
        elif op == "no_client_validation":
            page.evaluate("document.querySelectorAll('form').forEach(f => f.noValidate = true)")

    def _submit(self, page: Page, steps: list, button="form [type=submit], form button:not([type=button])") -> dict:
        steps.append(dict(op="submit", selector=button))
        before = page.url
        status = None
        try:
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.startswith(self.base), timeout=8000) as ri:
                page.click(button)
            status = ri.value.status
            page.wait_for_load_state("load")
        except Exception:
            pass
        page.wait_for_timeout(100)
        alerts = page.eval_on_selector_all("[role=alert], .error, [aria-invalid=true]", "els => els.filter(e => e.offsetParent !== null).map(e => e.innerText)")
        body = page.inner_text("body")
        outcome = oracles.classify_outcome(status, before, page.url, alerts, body)
        return dict(status=status, outcome=outcome, alerts=alerts, url=page.url)

    # ------------------------------------------------------------------ search
    def _test_search(self, browser, form):
        ctx, page = self._new_page(browser)
        sel = self._field_selectors(form)
        steps = []
        self._do(page, steps, "goto", path=form["path"])
        self._do(page, steps, "fill", selector=sel["search"], value=XSS_PROBE)
        steps.append(dict(op="press", selector=sel["search"], key="Enter"))
        page.press(sel["search"], "Enter")
        page.wait_for_load_state("load")
        if page.locator("#xss-probe").count() > 0:
            self._add(oracle="reflected_html_injection", title="Search page renders typed HTML instead of showing it as text", page=form["path"],
                      expected="typed text is escaped", actual="a <b> element typed into the box appeared on the page",
                      steps=steps, check=dict(type="dom_exists", selector="#xss-probe", case="xss"), screenshot=self._shot(page, "xss"))
        ctx.close()

    # ------------------------------------------------------------------ signup
    def _signup(self, browser, form, name="Test User", email=None, password="Passw0rd!x", confirm=None, no_validation=True):
        ctx, page = self._new_page(browser)
        sel = self._field_selectors(form)
        steps = []
        self._do(page, steps, "goto", path=form["path"])
        email = email or f"user{uuid.uuid4().hex[:8]}@example.com"
        if "name" in sel:
            self._do(page, steps, "fill", selector=sel["name"], value=name)
        if "email" in sel:
            self._do(page, steps, "fill", selector=sel["email"], value=email)
        self._do(page, steps, "fill", selector=sel["password"], value=password)
        if "confirm" in sel:
            self._do(page, steps, "fill", selector=sel["confirm"], value=confirm if confirm is not None else password)
        if "terms" in sel:
            self._do(page, steps, "check", selector=sel["terms"])
        if no_validation:
            self._do(page, steps, "no_client_validation")
        res = self._submit(page, steps)
        res.update(steps=steps, email=email, password=password, page=page, ctx=ctx)
        return res

    def _test_signup(self, browser, form, login_form):
        path = form["path"]
        outcomes = []
        for _ in range(FLAKY_RUNS):                                 # valid signups, repeated: do they all behave the same?
            r = self._signup(browser, form)
            outcomes.append(r["outcome"])
            first = r if len(outcomes) == 1 else first
            if len(outcomes) > 1:
                r["ctx"].close()
        base_r = first
        if base_r["outcome"] == "server_error" or "server_error" in outcomes:
            if len(set(outcomes)) > 1:
                self._add(oracle="flaky_behaviour", title="Signup works only some of the time", page=path, expected="the same valid signup always succeeds",
                          actual=f"{FLAKY_RUNS} identical valid signups gave: {', '.join(outcomes)}", steps=base_r["steps"],
                          check=dict(type="flaky", case="valid_signup"), screenshot=self._shot(base_r["page"], "flaky"))
            else:
                self._add(oracle="server_error", title="Valid signup causes a server error", page=path, expected="account created", actual="HTTP 5xx",
                          steps=base_r["steps"], check=dict(type="status5xx", case="valid_signup"), screenshot=self._shot(base_r["page"], "5xx"))
        good_email, good_pw = base_r["email"], base_r["password"]
        setup_steps = list(base_r["steps"])             # the signup that registers good_email, so later steps can be replayed from scratch
        base_r["ctx"].close()

        cases = [
            ("accepted_invalid_email", dict(email="not-an-email"), "an invalid email address is rejected"),
            ("accepted_password_mismatch", dict(confirm="Different1!"), "mismatched passwords are rejected"),
            ("accepted_duplicate_email", dict(email=good_email), "an email that is already registered is rejected"),
        ]
        for oracle, kwargs, expectation in cases:
            if oracle == "accepted_password_mismatch" and "confirm" not in self._field_selectors(form):
                continue
            r = self._signup(browser, form, **kwargs)
            if r["outcome"] == "accepted":
                full_steps = (setup_steps + r["steps"]) if oracle == "accepted_duplicate_email" else r["steps"]
                self._add(oracle=oracle, title=f"Signup form does not enforce: {expectation}", page=path, expected="rejected with a clear message",
                          actual="account created", steps=full_steps, check=dict(type="outcome_is", outcome="accepted", case=oracle),
                          screenshot=self._shot(r["page"], oracle))
            elif r["outcome"] == "server_error":
                self._add(oracle="server_error", title=f"Server error when testing: {expectation}", page=path, expected="rejected with a message",
                          actual="HTTP 5xx", steps=r["steps"], check=dict(type="status5xx", case=oracle), screenshot=self._shot(r["page"], "5xx"))
            r["ctx"].close()

        # special characters in a name must never crash the server
        r = self._signup(browser, form, name="O'Brien \"Q\" <Test>")
        if r["outcome"] == "server_error":
            self._add(oracle="server_error", title="A name with an apostrophe crashes the server", page=path, expected="accepted or rejected cleanly",
                      actual="HTTP 5xx", steps=r["steps"], check=dict(type="status5xx", case="special_chars"), screenshot=self._shot(r["page"], "5xx"))
        r["ctx"].close()

        # round trip: a created account must be able to log in
        if login_form:
            self._roundtrip(browser, form, login_form)

    def _login_steps(self, page, login_form, email, password, steps):
        sel = self._field_selectors(login_form)
        self._do(page, steps, "goto", path=login_form["path"])
        self._do(page, steps, "fill", selector=sel["email"], value=email)
        self._do(page, steps, "fill", selector=sel["password"], value=password)
        self._do(page, steps, "no_client_validation")
        return self._submit(page, steps)

    def _roundtrip(self, browser, form, login_form):
        r = self._signup(browser, form)
        if r["outcome"] != "accepted":
            r["ctx"].close()
            return
        steps = list(r["steps"])
        res = self._login_steps(r["page"], login_form, r["email"], r["password"], steps)
        if res["outcome"] != "accepted":
            self._add(oracle="signup_not_saved", title="An account that was 'created' cannot log in", page=form["path"],
                      expected="login succeeds with the credentials just registered", actual=f"login outcome: {res['outcome']}",
                      steps=steps, check=dict(type="outcome_is", outcome="rejected", expect_not="accepted", case="roundtrip_login"),
                      screenshot=self._shot(r["page"], "roundtrip"))
        r["ctx"].close()
        self._roundtrip_email, self._roundtrip_pw = r["email"], r["password"]

    # ------------------------------------------------------------------ login
    def _test_login(self, browser, login_form, signup_form):
        s = self._signup(browser, signup_form)
        email, pw = s["email"], s["password"]
        s["ctx"].close()
        ctx, page = self._new_page(browser)
        steps = list(s["steps"])                         # register the account first, so the replay starts from nothing
        res = self._login_steps(page, login_form, email, "totally-wrong-password", steps)
        if res["outcome"] == "accepted":
            self._add(oracle="login_accepts_wrong_password", title="Login accepts a wrong password", page=login_form["path"],
                      expected="login rejected", actual="logged in", steps=steps,
                      check=dict(type="outcome_is", outcome="accepted", case="wrong_password"), screenshot=self._shot(page, "wrongpw"))
        ctx.close()
