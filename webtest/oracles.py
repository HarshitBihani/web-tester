"""The rules that decide whether something is a bug. Kept free of the browser so they can be tested on their own.

An "oracle" answers: given what I saw, was it wrong? Each one is deliberately simple and written to avoid crying wolf.
"""
from __future__ import annotations
import re

SLOW_SECONDS = 2.0
RISKY_WORDS = re.compile(r"\b(delete|remove|destroy|erase|wipe|pay|purchase|buy|checkout|place order|confirm order|cancel (account|subscription)|"
                         r"unsubscribe|deactivate|transfer|send money|reset (all|data)|log ?out|sign ?out)\b", re.I)
ERROR_WORDS = re.compile(r"(invalid|error|required|must|do not match|don't match|already exists|incorrect|not valid|too short|failed)", re.I)
SUCCESS_WORDS = re.compile(r"(account created|welcome|success|thank you|thanks for)", re.I)


def is_risky(label: str) -> bool:
    """Would clicking something with this label change or destroy data? Such clicks need a human's approval."""
    return bool(RISKY_WORDS.search(label or ""))


def classify_outcome(status: int | None, url_before: str, url_after: str, alert_texts: list[str], body_text: str) -> str:
    """What happened after a form was submitted: server_error, rejected, accepted, blocked (nothing was sent) or unclear."""
    if status is None:
        return "blocked"
    if status >= 500:
        return "server_error"
    if 400 <= status < 500:
        return "rejected"
    if any(ERROR_WORDS.search(t) for t in alert_texts if t):
        return "rejected"
    if url_after != url_before or SUCCESS_WORDS.search(body_text or ""):
        return "accepted"
    return "unclear"


def is_slow(seconds: float, budget: float = SLOW_SECONDS) -> bool:
    return seconds > budget


def relevant_console_error(text: str, location_url: str = "") -> bool:
    """Ignore noise that is not the site's fault, such as the browser asking for a favicon."""
    if "favicon" in (location_url or "").lower() or "favicon" in (text or "").lower():
        return False
    return True


# which oracle a given bug should trigger (used by the evaluation, not by the agent)
EXPECTED_ORACLE = {
    "B1": "accepted_invalid_email", "B2": "accepted_password_mismatch", "B3": "accepted_duplicate_email",
    "B4": "server_error", "B5": "broken_link", "B6": "console_error", "B7": "missing_accessible_name",
    "B8": "login_accepts_wrong_password", "B9": "reflected_html_injection", "B10": "slow_response",
    "B11": "flaky_behaviour", "B12": "signup_not_saved",
}
SEVERITY = {
    "accepted_invalid_email": "medium", "accepted_password_mismatch": "medium", "accepted_duplicate_email": "high",
    "server_error": "high", "broken_link": "low", "console_error": "medium", "missing_accessible_name": "low",
    "login_accepts_wrong_password": "critical", "reflected_html_injection": "critical", "slow_response": "medium",
    "flaky_behaviour": "high", "signup_not_saved": "high",
}
