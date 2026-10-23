import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webtest import oracles
from webtest.agent import Agent
from webtest.report import compare_runs, repro_script, write_outputs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------- oracles (no browser needed) ----------------
@pytest.mark.parametrize("label,risky", [
    ("Delete account", True), ("Remove item", True), ("Pay now", True), ("Place order", True), ("Log out", True), ("Cancel subscription", True),
    ("Search", False), ("Create account", False), ("Sign up", False), ("Next", False), ("Add to wishlist", False), ("Deliver to my address", False),
])
def test_risky_labels(label, risky):
    assert oracles.is_risky(label) == risky


@pytest.mark.parametrize("status,before,after,alerts,body,expected", [
    (500, "/a", "/a", [], "", "server_error"),
    (400, "/a", "/a", ["Invalid email"], "", "rejected"),
    (401, "/login", "/login", [], "", "rejected"),
    (200, "/a", "/a", ["Passwords do not match"], "", "rejected"),
    (302, "/signup", "/welcome", [], "Account created", "accepted"),
    (200, "/a", "/a", [], "nothing special", "unclear"),
    (None, "/a", "/a", [], "", "blocked"),
])
def test_classify_outcome(status, before, after, alerts, body, expected):
    assert oracles.classify_outcome(status, before, after, alerts, body) == expected


def test_slow_and_console_noise():
    assert oracles.is_slow(3.5) and not oracles.is_slow(0.4)
    assert not oracles.relevant_console_error("Failed to load resource: 404", "http://x/favicon.ico")
    assert oracles.relevant_console_error("Uncaught TypeError: x is null", "http://x/app.js")


def test_every_seeded_bug_has_an_oracle_and_severity():
    assert len(set(oracles.EXPECTED_ORACLE.values())) == len(oracles.EXPECTED_ORACLE)
    assert all(o in oracles.SEVERITY for o in oracles.EXPECTED_ORACLE.values())


# ---------------- form recognition ----------------
def test_form_kinds():
    f = Agent._form_kind
    assert f([{"type": "text", "name": "name", "id": ""}, {"type": "email", "name": "email", "id": ""},
              {"type": "password", "name": "pw", "id": ""}, {"type": "password", "name": "pw2", "id": ""}]) == "signup"
    assert f([{"type": "email", "name": "email", "id": ""}, {"type": "password", "name": "password", "id": ""}]) == "login"
    assert f([{"type": "search", "name": "q", "id": "q"}]) == "search"
    assert f([{"type": "text", "name": "comment", "id": ""}]) is None


def test_field_selectors():
    form = {"inputs": [{"type": "text", "name": "name", "id": "name"}, {"type": "email", "name": "email", "id": "email"},
                       {"type": "password", "name": "password", "id": "password"}, {"type": "password", "name": "confirm", "id": "confirm"},
                       {"type": "checkbox", "name": "terms", "id": ""}]}
    sel = Agent._field_selectors(form)
    assert sel == {"name": "#name", "email": "#email", "password": "#password", "confirm": "#confirm", "terms": "[name='terms']"}


# ---------------- reports and reproduction scripts ----------------
CHECKS = [dict(type="status5xx", case="x"), dict(type="console_error", case="x"), dict(type="slow", budget=2.0, case="x"),
          dict(type="dom_exists", selector="#xss-probe", case="x"), dict(type="no_accessible_name", selector="#email", case="x"),
          dict(type="link_broken", url="http://localhost:1/privacy", path="/privacy", case="x"), dict(type="outcome_is", outcome="accepted", case="x"),
          dict(type="outcome_is", outcome="rejected", expect_not="accepted", case="x"), dict(type="flaky", case="x")]
STEPS = [dict(op="goto", path="/signup"), dict(op="fill", selector="#email", value="a'b\"c"), dict(op="check", selector="[name='terms']"),
         dict(op="no_client_validation"), dict(op="submit", selector="form [type=submit]"), dict(op="press", selector="#q", key="Enter")]


@pytest.mark.parametrize("check", CHECKS, ids=[c["type"] + str(i) for i, c in enumerate(CHECKS)])
def test_every_repro_script_is_valid_python(check):
    finding = dict(title="t", oracle="o", check=check, steps=STEPS)
    compile(repro_script(finding, "http://localhost:5055", "repro.py"), "repro.py", "exec")       # quotes in values survive


def test_report_files_and_regression_view(tmp_path):
    def finding(i, oracle, case="c"):
        return dict(id=f"F0{i}", oracle=oracle, severity="high", title=f"problem {oracle}", page="/p", expected="e", actual="a", steps=STEPS[:2],
                    check=dict(type="status5xx", case=case), screenshot="", console=[])
    old = dict(base="http://x", seconds=1, pages=1, skipped_risky=[], findings=[finding(1, "server_error"), finding(2, "broken_link")])
    new = dict(base="http://x", seconds=1, pages=1, skipped_risky=["Delete account on /dashboard"], findings=[finding(1, "server_error"), finding(3, "slow_response")])
    write_outputs(new, str(tmp_path))
    md = (tmp_path / "report.md").read_text()
    assert "problem slow_response" in md and "Delete account" in md and (tmp_path / "repro").exists()
    diff = compare_runs(old, new)
    assert diff == dict(new=["problem slow_response"], fixed=["problem broken_link"], still_failing=["problem server_error"])


# ---------------- end to end on a clean app (real browser) ----------------
@pytest.fixture(scope="module")
def clean_app():
    port = 5391
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "demo_app", "server.py"), "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://localhost:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    yield url
    p.terminate()


def test_clean_app_has_no_findings_and_the_destructive_button_is_not_clicked(clean_app, tmp_path):
    res = Agent(clean_app, str(tmp_path)).run()
    assert res["findings"] == [], [f["title"] for f in res["findings"]]
    assert res["skipped_risky"] and res["clicked_risky"] == []
    assert json.load(urllib.request.urlopen(clean_app + "/state"))["users"] > 0      # the accounts the test created still exist
