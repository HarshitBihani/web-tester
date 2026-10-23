"""Measure the agent against the demo app with known, seeded bugs.

    python evaluate.py         (about 10 minutes)

For every bug, start the app with only that bug switched on, run the agent, and check whether it reported the right kind of problem.
Then run it on the bug-free build three times: anything it reports there is a false alarm.
Finally run the generated reproduction script for every found bug against the buggy app (should exit 1) and the clean app (should exit 0).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

from webtest.agent import Agent
from webtest.oracles import EXPECTED_ORACLE
from webtest.report import write_outputs

PORT = 5100


def start(bugs: str):
    global PORT
    PORT += 1
    p = subprocess.Popen([sys.executable, "demo_app/server.py", "--port", str(PORT), "--bugs", bugs], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://localhost:{PORT}"
    for _ in range(50):
        try:
            urllib.request.urlopen(url, timeout=1)
            return p, url
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def run_repro(path: str, url: str) -> int:
    src = open(path, encoding="utf-8").read()
    old = src.split("BASE = ")[1].split("\n")[0]
    src = src.replace("BASE = " + old, f"BASE = {url!r}")
    tmp = path + ".tmp.py"
    open(tmp, "w", encoding="utf-8").write(src)
    try:
        return subprocess.run([sys.executable, tmp], capture_output=True, timeout=120).returncode
    finally:
        os.remove(tmp)


def main():
    os.makedirs("outputs/eval", exist_ok=True)
    rows, t0 = [], time.time()
    for bug, oracle in EXPECTED_ORACLE.items():
        p, url = start(bug)
        res = Agent(url, f"outputs/eval/{bug}").run()
        write_outputs(res, f"outputs/eval/{bug}")
        p.terminate()
        hits = [f for f in res["findings"] if f["oracle"] == oracle]
        repro_bug = repro_clean = None
        if hits:
            script = os.path.join(f"outputs/eval/{bug}", hits[0]["repro"])
            fresh_p, fresh_url = start(bug)               # replay on a FRESH copy of the buggy app, as a developer would
            repro_bug = run_repro(script, fresh_url)
            fresh_p.terminate()
            fresh_c, fresh_c_url = start("")              # and on a fresh bug-free copy: it must NOT report the bug
            repro_clean = run_repro(script, fresh_c_url)
            fresh_c.terminate()
        row = dict(bug=bug, expected=oracle, detected=bool(hits), other_findings=[f["oracle"] for f in res["findings"] if f["oracle"] != oracle],
                   seconds=res["seconds"], repro_exit_on_buggy=repro_bug, repro_exit_on_clean=repro_clean)
        rows.append(row)
        print(f"{bug:<4} {oracle:<30} detected={row['detected']}  extra={row['other_findings']}  repro bug/clean={repro_bug}/{repro_clean}  ({res['seconds']}s)")

    control = []
    for i in range(3):
        p, url = start("")
        res = Agent(url, f"outputs/eval/control{i}").run()
        write_outputs(res, f"outputs/eval/control{i}")
        control.append(dict(run=i, findings=[f["title"] for f in res["findings"]], skipped_risky=res["skipped_risky"], clicked_risky=res["clicked_risky"]))
        users = json.load(urllib.request.urlopen(url + "/state"))["users"]
        control[-1]["accounts_still_exist_after_run"] = users
        p.terminate()
        print(f"control {i}: {len(res['findings'])} findings, skipped risky: {res['skipped_risky']}, accounts left: {users}")

    p, url = start("B1,B2,B3,B4,B5,B6,B7,B8,B9,B10,B11,B12")
    allres = Agent(url, "outputs/eval/all").run()
    write_outputs(allres, "outputs/eval/all")
    all_found = sorted({f["oracle"] for f in allres["findings"]})
    p.terminate()

    # approval works: with approval the destructive button IS clicked
    p, url = start("")
    appr = Agent(url, "outputs/eval/approved", approve=lambda label, u: True).run()
    users_after = json.load(urllib.request.urlopen(url + "/state"))["users"]
    p.terminate()

    out = dict(bugs=rows, detected=sum(r["detected"] for r in rows), total_bugs=len(rows), control=control,
               control_false_alarms=sum(len(c["findings"]) for c in control), all_bugs_run=dict(found_kinds=all_found, expected_kinds=sorted(set(EXPECTED_ORACLE.values()))),
               approval_demo=dict(clicked=appr["clicked_risky"], accounts_after=users_after), minutes=round((time.time() - t0) / 60, 1))
    json.dump(out, open("outputs/eval_results.json", "w"), indent=1)
    print(json.dumps({k: v for k, v in out.items() if k not in ("bugs", "control")}, indent=1))


if __name__ == "__main__":
    main()
