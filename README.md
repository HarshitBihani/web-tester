# Website Testing Agent: finds bugs, proves them, and never clicks "Delete account"

Point it at a website. It explores the pages, works out which forms are a signup, a login or a search, writes tests for them, runs them in a real Chrome browser, and gives you a bug report where **every bug comes with a script that reproduces it**, plus a screenshot and the browser's error messages.

It follows rules, not a language model: it recognises forms by their fields and applies a set of checks (called oracles) to decide what counts as a bug. I chose this so that I could measure it exactly. A language model could replace the planner that decides what to test, but the browser control, the checks, the safety rule and the reports would stay the same.

## How well does it work?

Testing a testing tool needs a site where the bugs are known. I built a small demo shop (signup, login, search, dashboard) with **12 seeded bugs** that can each be switched on, and a bug-free build to check for false alarms.

| Question | Result |
|---|---|
| Bugs found, each switched on alone | **12 of 12** |
| False alarms on the bug-free build (3 runs) | **0** |
| Reproduction script fails (exit 1) on the buggy app | **12 of 12** |
| Reproduction script passes (exit 0) on a bug-free app | **12 of 12** |
| Time per full test run | about 20 seconds (51 s when a page is deliberately slow) |
| Destructive "Delete account" button clicked without approval | **never** (skipped in all 3 control runs; clicked only when I switched approval on) |

The 12 seeded bugs: signup accepts an invalid email, accepts mismatched passwords, accepts a duplicate email; a name with an apostrophe crashes the server; a broken footer link; a JavaScript error on the signup page; an input with no label; login accepts a wrong password; the search page prints typed HTML as real HTML (cross-site scripting); a page that takes 3.5 seconds; signup that fails randomly about one time in three; signup that says "Account created" but saves nothing.

**Where it is weaker, which I think matters more than the score:**
- With all 12 bugs switched on at once it found **11 of 12 kinds**. It missed "login accepts a wrong password" because signup saves nothing in that build, so there is no account to try the wrong password on. When a flow that other tests depend on is broken, those other tests go blind. A person would fix signup and re-run.
- The random failure (one in three) is found by repeating the same valid signup 8 times. The chance that all 8 happen to succeed is about 3.6%, so on a given run it can slip through. That is a limit of the method, not a bug I can remove.
- Secondary symptoms appear as extra findings: the random-failure build also reports server errors from the other tests, and "signup saves nothing" also reports that duplicates are accepted. These are real consequences of the same fault, not false alarms, but a developer would see them as noise.
- The test site is small and written by me. A real site has far more variety. The score shows the approach works on known bugs, not how many bugs it would find on your site.

## What it checks

| Check | How it decides |
|---|---|
| Server errors | any same-site response with status 500 or above |
| Broken links | fetches every same-site link and flags 400 and above |
| JavaScript errors | the browser console and uncaught errors (ignoring favicon noise) |
| Slow pages | load time over 2 seconds |
| Missing labels | form inputs with no label, aria-label or title |
| Cross-site scripting | types a small HTML tag into the search box and looks for it in the page |
| Signup rules | invalid email, mismatched passwords and duplicate email must be rejected; a name with an apostrophe must not crash |
| Login | a created account must be able to log in, and a wrong password must be rejected |
| Flaky behaviour | repeats the same valid signup 8 times and compares the outcomes |

To test the server and not just the browser, the agent turns off the browser's own form validation before submitting the negative cases. Otherwise the browser would block a bad email before it ever reached the server.

## Safe by default

Anything labelled like delete, remove, pay, buy, place order, cancel subscription or log out is never clicked unless an approval function says yes. The default says no, and the skipped actions are listed in the report. `--approve-risky` lets a human opt in.

## Bug reports you can act on

For every finding it writes: severity, numbered steps in plain language, expected vs actual, a screenshot, the console error, and a standalone `repro_*.py` script. Run the script: exit code 1 means the bug is present, 0 means it did not appear. The flaky bug's script tries 12 times. Each script includes the setup it needs (for example the signup that must exist before a duplicate can be tested), so it works on a fresh copy of the app. Getting that right took a fix: my first scripts silently depended on state left over from the test run.

It can also compare two runs (`--compare`) and report what is new, fixed or still failing, which is how a regression check works.

## Run it

```bash
pip install -r requirements.txt     # needs Google Chrome installed
python demo_app/server.py --port 5055 --bugs B1,B4,B9    # the demo shop with 3 bugs (leave out --bugs for a clean build)
python run_agent.py http://localhost:5055 --out outputs/run1
python evaluate.py                  # the full measurement above (about 10 minutes)
python -m pytest tests -q           # 34 tests
```

Outputs per run: `report.md`, `findings.json`, `shots/` and `repro/`.

## Files

- `webtest/agent.py`: exploring, recognising forms, running the tests.
- `webtest/oracles.py`: the rules that decide what is a bug, and which actions are risky. No browser needed, so they are unit tested directly.
- `webtest/report.py`: the report, the reproduction scripts and the run comparison.
- `demo_app/server.py`: the demo shop with the seeded bugs.
- `.github/workflows/webtest.yml`: a CI workflow that runs the agent on every pull request and nightly. I wrote it but have not run it on GitHub yet.

## Limits

- It tests what it can find by following links and recognising forms. It cannot log in to areas it was not given credentials for, and it does not understand what a page is for.
- Only signup, login, search and page-level checks are implemented. Checkout flows, file uploads and multi-page wizards are not.
- Chrome must be installed locally.
- The demo site is mine, so the results above are about the method, not about real-world coverage.

Harshit Bihani
