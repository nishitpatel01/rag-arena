"""Level 4 ``verify_citations`` core: extract numeric and clause claims from a draft and check each one against the
text of the chunks the draft cites (chunks that tools actually returned during the run).

* Clause claims (``CLIN-0004-AB``, ``SEC-4.2.1``, ``DATA-SLA-3.1``, ``CTRL-SOC2-CC6.1`` …) are supported when the id
  occurs (case-insensitive) in a cited chunk's text, ``section_id`` or ``amends_section``.
* Numeric claims (``$465,500.00``, ``5%``, ``2,000 hours``, ``6,000 TB`` …) are supported when the same value occurs
  among the numbers of the cited chunks, or equals a result the run's calculator produced (derived figures).
Pure functions; bounded input sizes.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Iterable, Sequence

MAX_DRAFT_CHARS = 20000
MAX_CLAIMS = 80
_MULTIPLIERS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
CLAUSE_RE = re.compile(r"\b(?:[A-Z][A-Z0-9]{1,9}-)+(?:\d+(?:\.\d+)*|[A-Z]{1,3}\d+(?:\.\d+)*)(?:-[A-Z]{2})?\b")
_MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)(?:\s?(k|m|b|thousand|million|billion)\b)?", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?%")
_QUANTITY_RE = re.compile(
    r"(?<![\w.$-])(\d[\d,]*(?:\.\d+)?)\s?(hours?|hrs?|hr|TB|PB|GB|nodes?|slots?|minutes?|mins?|seconds?|days?|months?|years?)\b",
    re.IGNORECASE,
)
_ANY_NUMBER_RE = re.compile(r"(?<![A-Za-z])(\d[\d,]*(?:\.\d+)?)")


@dataclass(frozen=True)
class Claim:
    text: str
    kind: str  # "clause" | "money" | "percent" | "quantity"
    value: float | None = None


@dataclass(frozen=True)
class ClaimCheck:
    claim: Claim
    supported_by: str  # "chunk" | "calculator" | ""


@dataclass(frozen=True)
class VerificationReport:
    checks: tuple[ClaimCheck, ...]
    unknown_chunk_ids: tuple[str, ...]
    cited_chunk_ids: tuple[str, ...]

    @property
    def unsupported(self) -> tuple[Claim, ...]:
        return tuple(c.claim for c in self.checks if not c.supported_by)

    @property
    def verified(self) -> bool:
        return bool(self.cited_chunk_ids) and not self.unsupported and not self.unknown_chunk_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "verified": self.verified,
            "checked_claims": len(self.checks),
            "supported_by_chunk": sum(1 for c in self.checks if c.supported_by == "chunk"),
            "supported_by_calculator": sum(1 for c in self.checks if c.supported_by == "calculator"),
            "unsupported_claims": [{"claim": c.text, "kind": c.kind} for c in self.unsupported],
            "unknown_chunk_ids": list(self.unknown_chunk_ids),
            "cited_chunk_ids": list(self.cited_chunk_ids),
        }


def _to_float(raw: str, suffix: str | None = None) -> float | None:
    """Parse a regex-matched number (``\\d[\\d,]*(\\.\\d+)?`` — always a valid float once commas are removed)."""
    value = float(raw.replace(",", ""))
    if suffix:
        value *= _MULTIPLIERS.get(suffix.lower(), 1.0)
    return value if math.isfinite(value) else None


def _spans_overlap(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in taken)


def extract_claims(draft: str) -> tuple[Claim, ...]:
    """Distinct clause ids, money amounts, percentages and unit quantities in ``draft`` (first ``MAX_CLAIMS``)."""
    if not isinstance(draft, str):
        raise TypeError("draft must be a string")
    text = draft[:MAX_DRAFT_CHARS]
    claims: list[Claim] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in (("clause", CLAUSE_RE), ("money", _MONEY_RE), ("percent", _PERCENT_RE), ("quantity", _QUANTITY_RE)):
        for m in pattern.finditer(text):
            if _spans_overlap(m.span(), taken):
                continue
            taken.append(m.span())
            if kind == "clause":
                claims.append(Claim(text=m.group(0), kind=kind))
                continue
            value = _to_float(m.group(1), m.group(2) if kind == "money" and m.lastindex and m.lastindex >= 2 else None)
            if value is not None:
                claims.append(Claim(text=m.group(0).strip(), kind=kind, value=value))
    unique = list({(c.kind, c.text.upper()): c for c in claims}.values())
    return tuple(unique[:MAX_CLAIMS])


def numbers_in(texts: Iterable[str]) -> frozenset[float]:
    """Every number written in ``texts`` (plus value/100 for percentages, so 5% also matches 0.05)."""
    out: set[float] = set()
    for text in texts:
        for m in _ANY_NUMBER_RE.finditer(text):
            value = _to_float(m.group(1))
            if value is not None:
                out.add(round(value, 6))
        for m in _PERCENT_RE.finditer(text):
            value = _to_float(m.group(1))
            if value is not None:
                out.add(round(value / 100.0, 6))
    return frozenset(out)


def _matches(value: float, pool: Iterable[float]) -> bool:
    return any(math.isclose(value, p, rel_tol=1e-9, abs_tol=0.005) for p in pool)


def _clause_supported(claim: Claim, haystack_upper: str) -> bool:
    return claim.text.upper() in haystack_upper


def check_claims(
    draft: str,
    cited_texts: Sequence[str],
    calculator_values: Iterable[float] = (),
) -> tuple[ClaimCheck, ...]:
    """Check every claim in ``draft`` against ``cited_texts`` (and calculator outputs for numeric claims)."""
    haystack = "\n".join(cited_texts).upper()
    chunk_numbers = numbers_in(cited_texts)
    calc = tuple(round(float(v), 6) for v in calculator_values)
    checks: list[ClaimCheck] = []
    for claim in extract_claims(draft):
        if claim.kind == "clause":
            checks.append(ClaimCheck(claim, "chunk" if _clause_supported(claim, haystack) else ""))
            continue
        assert claim.value is not None
        candidates = (claim.value, claim.value / 100.0) if claim.kind == "percent" else (claim.value,)
        if any(_matches(v, chunk_numbers) for v in candidates):
            checks.append(ClaimCheck(claim, "chunk"))
        elif any(_matches(v, calc) for v in candidates):
            checks.append(ClaimCheck(claim, "calculator"))
        else:
            checks.append(ClaimCheck(claim, ""))
    return tuple(checks)


__all__ = [
    "CLAUSE_RE",
    "Claim",
    "ClaimCheck",
    "MAX_DRAFT_CHARS",
    "VerificationReport",
    "check_claims",
    "extract_claims",
    "numbers_in",
]
