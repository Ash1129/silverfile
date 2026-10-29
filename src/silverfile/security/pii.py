"""Social Security numbers and other PII: one validator, one redactor.

The rule (brief, section on SSNs): the system may only ever accept a number
from the 900 series, which the US government sets aside for testing. Anything
else that looks like an SSN is presumed real, is refused and is never stored.

Three ways a number is shown, from most to least revealing:
- the full test SSN lives in the intake facts only (encrypted at rest);
- `mask` (***-**-1234) is what a preparer sees on the draft and dashboard;
- `redact` ([SSN]) is what logs, traces and the security log get, with API
  keys removed as well. `install_log_redaction` applies it to every log record.
"""
from __future__ import annotations

import logging
import re
from typing import Any

# 3-2-4 digits with the same separator twice (dash, space or none). Not part of a longer
# digit run or a word (a hex id), so an EIN (2-7), a phone number (3-3-4), a ZIP+4 (5-4) or an amount does not match.
SSN_RE = re.compile(r"(?<![\w-])(\d{3})([- ]?)(\d{2})\2(\d{4})(?![\w-])")
API_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
TEST_AREA = range(900, 1000)


class RealSSNError(ValueError):
    """A number shaped like an SSN that is not in the 900 test series."""


def find_ssns(text: Any) -> list[str]:
    """Every SSN-shaped number in the text, normalised to ddd-dd-dddd."""
    return [f"{a}-{g}-{s}" for a, _, g, s in SSN_RE.findall(str(text or ""))]


def is_test_ssn(ssn: str) -> bool:
    m = SSN_RE.fullmatch(str(ssn or "").strip())
    return bool(m) and int(m.group(1)) in TEST_AREA


def validate_test_ssn(text: str) -> tuple[bool, str | None]:
    """(accepted, normalised number) for the first SSN-shaped number in `text`.

    (False, None) when there is none; (False, number) when it is not a test number.
    """
    found = find_ssns(text)
    if not found:
        return False, None
    return is_test_ssn(found[0]), found[0]


def require_test_ssn(value: Any) -> str:
    """The normalised test SSN, or RealSSNError. Empty values pass through as ''."""
    if value in (None, ""):
        return ""
    found = find_ssns(value)
    if len(found) != 1 or SSN_RE.fullmatch(str(value).strip()) is None:
        raise RealSSNError("not a Social Security number in the form 9xx-xx-xxxx")
    if not is_test_ssn(found[0]):
        raise RealSSNError("only test Social Security numbers (900 to 999) are accepted")
    return found[0]


def has_real_ssn(text: Any) -> bool:
    return any(not is_test_ssn(s) for s in find_ssns(text))


def mask(value: Any) -> str:
    """Show only the last four digits of anything that looks like an SSN."""
    if value in (None, ""):
        return ""
    return SSN_RE.sub(lambda m: f"***-**-{m.group(4)}", str(value))


def mask_deep(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: mask_deep(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_deep(v) for v in obj]
    if isinstance(obj, str):
        return mask(obj)
    return obj


def redact(value: Any) -> str:
    """For logs and traces: SSNs and API keys removed entirely."""
    s = SSN_RE.sub("[SSN]", str(value))
    return API_KEY_RE.sub("sk-ant-[REDACTED]", s)


def redact_deep(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: redact_deep(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_deep(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def remove_ssns(text: str, placeholder: str = "[number removed]") -> str:
    return SSN_RE.sub(placeholder, text or "")


_installed = False


def install_log_redaction() -> None:
    """Redact every log record at creation, whichever logger or handler it reaches.

    A record factory rather than a handler filter, so records from third-party
    loggers (uvicorn, httpx, langgraph) are covered too.
    """
    global _installed
    if _installed:
        return
    base = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base(*args, **kwargs)
        # redact the template and each argument in place, keeping their number and types:
        # formatters such as uvicorn's access log unpack record.args themselves
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: (redact(v) if isinstance(v, str) else v) for k, v in record.args.items()}
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
