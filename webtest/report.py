"""Turn findings into things a developer can act on: a readable report, and a script that reproduces each bug on its own."""
from __future__ import annotations
import json
import os

REPRO_TEMPLATE = '''"""Reproduces: {title}
Run:  python {filename}
Exit code 1 = the bug is present, 0 = the bug did not appear.   Needs: pip install playwright
"""
import re
import sys
import time
from playwright.sync_api import sync_playwright

BASE = {base!r}
ERROR_WORDS = re.compile(r"(invalid|error|required|must|do not match|don't match|already exists|incorrect|not valid|too short|failed)", re.I)
SUCCESS_WORDS = re.compile(r"(account created|welcome|success|thank you|thanks for)", re.I)
ATTEMPTS = {attempts}
last = {{"outcome": None}}
statuses, errors = [], []


def submit(page, selector):
    before, status = page.url, None
    try:
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.startswith(BASE), timeout=8000) as ri:
            page.click(selector)
        status = ri.value.status
        page.wait_for_load_state("load")
    except Exception:
        pass
    page.wait_for_timeout(100)
    alerts = page.eval_on_selector_all("[role=alert], .error, [aria-invalid=true]", "els => els.filter(e => e.offsetParent !== null).map(e => e.innerText)")
    body = page.inner_text("body")
    if status is None: outcome = "blocked"
    elif status >= 500: outcome = "server_error"
    elif 400 <= status < 500: outcome = "rejected"
    elif any(ERROR_WORDS.search(t) for t in alerts): outcome = "rejected"
    elif page.url != before or SUCCESS_WORDS.search(body): outcome = "accepted"
    else: outcome = "unclear"
    last["outcome"] = outcome


def attempt(page):
    elapsed = None
{steps}
{oracle}


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    results = []
    for _ in range(ATTEMPTS):
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("response", lambda r: statuses.append(r.status) if r.url.startswith(BASE) else None)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "favicon" not in m.text.lower() and "favicon" not in m.location.get("url", "") else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        results.append(attempt(page))
        ctx.close()
    browser.close()
{verdict}
print("BUG PRESENT" if bug else "bug did not appear", results)
sys.exit(1 if bug else 0)
'''


def _step_code(s: dict) -> str:
    op = s["op"]
    if op == "goto":
        return f'    t0 = time.perf_counter(); page.goto(BASE + {s["path"]!r}, wait_until="load"); elapsed = time.perf_counter() - t0'
    if op == "fill":
        return f'    page.fill({s["selector"]!r}, {s["value"]!r})'
    if op == "check":
        return f'    page.check({s["selector"]!r})'
    if op == "no_client_validation":
        return '    page.evaluate("document.querySelectorAll(\'form\').forEach(f => f.noValidate = true)")'
    if op == "submit":
        return f'    submit(page, {s["selector"]!r})'
    if op == "press":
        return f'    page.press({s["selector"]!r}, {s["key"]!r}); page.wait_for_load_state("load")'
    raise ValueError(op)


def repro_script(f: dict, base: str, filename: str) -> str:
    chk = f["check"]
    t = chk["type"]
    steps = "\n".join(_step_code(s) for s in f["steps"]) or "    pass"
    attempts = 12 if t == "flaky" else 1
    if t == "status5xx":
        oracle, verdict = "    return any(s >= 500 for s in statuses)", "bug = any(results)"
    elif t == "console_error":
        oracle, verdict = "    return len(errors) > 0", "bug = any(results)"
    elif t == "slow":
        oracle, verdict = f"    return elapsed is not None and elapsed > {chk['budget']}", "bug = any(results)"
    elif t == "dom_exists":
        oracle, verdict = f"    return page.locator({chk['selector']!r}).count() > 0", "bug = any(results)"
    elif t == "no_accessible_name":
        js = ("el => !(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.getAttribute('title') || "
              "(el.id && document.querySelector('label[for=\"' + el.id + '\"]')) || el.closest('label'))")
        oracle, verdict = f"    return page.eval_on_selector({chk['selector']!r}, {js!r})", "bug = any(results)"
    elif t == "link_broken":
        steps = "    pass"
        oracle, verdict = f"    return page.context.request.get(BASE + {chk['path']!r}).status >= 400", "bug = any(results)"
    elif t == "outcome_is":
        cond = f"last['outcome'] != {chk['expect_not']!r}" if chk.get("expect_not") else f"last['outcome'] == {chk['outcome']!r}"
        oracle, verdict = f"    return {cond}", "bug = any(results)"
    elif t == "flaky":
        oracle = "    return last['outcome']"
        verdict = "bug = len(set(results)) > 1 and 'server_error' in results"
    else:
        raise ValueError(t)
    return REPRO_TEMPLATE.format(title=f["title"], filename=filename, base=base, attempts=attempts, steps=steps, oracle=oracle, verdict=verdict)


def _human_steps(steps: list[dict]) -> list[str]:
    out = []
    for s in steps:
        o = s["op"]
        out.append({"goto": f"Open {s.get('path')}", "fill": f"Type {s.get('value')!r} into {s.get('selector')}", "check": f"Tick {s.get('selector')}",
                    "no_client_validation": "Turn off the browser's own form checks (to test the server)", "submit": "Press the submit button",
                    "press": f"Press {s.get('key')} in {s.get('selector')}"}[o])
    return out


def write_outputs(result: dict, out_dir: str) -> dict:
    os.makedirs(os.path.join(out_dir, "repro"), exist_ok=True)
    for f in result["findings"]:
        name = f"repro_{f['id']}_{f['oracle']}.py"
        path = os.path.join(out_dir, "repro", name)
        open(path, "w", encoding="utf-8").write(repro_script(f, result["base"], name))
        f["repro"] = os.path.join("repro", name)
    json.dump(result, open(os.path.join(out_dir, "findings.json"), "w"), indent=1)
    lines = [f"# Test report for {result['base']}", "",
             f"{len(result['findings'])} problem(s) found in {result['seconds']}s across {result['pages']} pages.", ""]
    if result["skipped_risky"]:
        lines += ["Not clicked (looks destructive, needs a person's approval): " + "; ".join(result["skipped_risky"]), ""]
    lines += ["| # | Severity | Problem | Page |", "|---|---|---|---|"]
    lines += [f"| {f['id']} | {f['severity']} | {f['title']} | {f['page']} |" for f in result["findings"]]
    for f in result["findings"]:
        lines += ["", f"## {f['id']}  {f['title']}", f"Severity: **{f['severity']}**  ·  Check: `{f['oracle']}`", "", "Steps:"]
        lines += [f"{i}. {t}" for i, t in enumerate(_human_steps(f["steps"]), 1)]
        lines += ["", f"Expected: {f['expected']}", f"Actual: {f['actual']}"]
        if f["console"]:
            lines += ["Console: `" + f["console"][0][:150].replace("`", "'") + "`"]
        if f["screenshot"]:
            lines += [f"Screenshot: `{os.path.basename(f['screenshot'])}`"]
        lines += [f"Reproduce: `python {f['repro']}` (exit code 1 means the bug is present)"]
    open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return result


def compare_runs(old: dict, new: dict) -> dict:
    """Regression view: which problems are new, which are fixed, which are still there."""
    sig = lambda f: f"{f['oracle']}|{f['page']}|{f['check'].get('case', '')}"
    o, n = {sig(f): f for f in old["findings"]}, {sig(f): f for f in new["findings"]}
    return dict(new=[n[k]["title"] for k in n if k not in o], fixed=[o[k]["title"] for k in o if k not in n],
                still_failing=[n[k]["title"] for k in n if k in o])
