"""Test a website.

    python run_agent.py http://localhost:5055 --out outputs/run1
    python run_agent.py http://localhost:5055 --out outputs/run2 --compare outputs/run1/findings.json
    python run_agent.py http://localhost:5055 --approve-risky      (lets it click things like 'Delete account')
"""
import argparse
import json

from webtest.agent import Agent
from webtest.report import compare_runs, write_outputs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", default="outputs/run")
    ap.add_argument("--compare", help="findings.json of an earlier run, to show what is new or fixed")
    ap.add_argument("--approve-risky", action="store_true")
    a = ap.parse_args()
    approve = (lambda label, url: True) if a.approve_risky else (lambda label, url: False)
    result = Agent(a.url, a.out, approve=approve).run()
    write_outputs(result, a.out)
    print(f"{len(result['findings'])} problems, {result['seconds']}s, report in {a.out}/report.md")
    for f in result["findings"]:
        print(f"  [{f['severity']:<8}] {f['title']}")
    if result["skipped_risky"]:
        print("not clicked without approval:", result["skipped_risky"])
    if a.compare:
        print("compared with earlier run:", json.dumps(compare_runs(json.load(open(a.compare)), result), indent=1))


if __name__ == "__main__":
    main()
