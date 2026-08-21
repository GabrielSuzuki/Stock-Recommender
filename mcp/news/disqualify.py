"""Mechanical disqualify rules.

architecture.md is explicit that these are "non-negotiable and mechanical, not
judgment calls", so they are implemented as deterministic Python, not as a
model instruction. A chart can look perfect and be uninvestable for reasons
only the filings know, and the decision to skip it should not depend on how a
prompt happened to land that morning.

The model still sees the news and may add its own concerns. It cannot clear a
disqualification the rules have set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

# Holding window for a swing trade. Earnings inside this window is the single
# most common way a good technical setup turns into a coin flip overnight.
DEFAULT_HOLD_DAYS = 21

# Forms that mean dilution is registered or imminent.
DILUTION_FORMS = ("S-1", "S-3", "424B")
# Forms that mean the equity's future is being decided by something other than
# its fundamentals.
CONTROL_FORMS = ("SC 13E3",)

_PATTERNS = {
    "reverse_split": re.compile(
        r"\breverse\s+(stock\s+)?split\b|\b1[-\s]for[-\s]\d+\b", re.I),
    "going_concern": re.compile(
        r"\bgoing[-\s]concern\b|\bsubstantial doubt\b", re.I),
    "acquisition_fixed_price": re.compile(
        r"\b(definitive (merger )?agreement|to be acquired|agreed to be acquired|"
        r"all[-\s]cash (deal|transaction|acquisition))\b", re.I),
    "offering": re.compile(
        r"\b(public offering|secondary offering|at[-\s]the[-\s]market|"
        r"\bATM\b program|registered direct|convertible notes offering)\b"),
    "bankruptcy": re.compile(r"\bchapter 11\b|\bbankrupt", re.I),
    "delisting": re.compile(r"\bdelisting\b|\bnotice of noncompliance\b", re.I),
}

DISQUALIFY_RULES = {
    "earnings_in_window": "earnings inside the holding window",
    "dilution_filing": "registration or takedown filed (dilution)",
    "reverse_split": "reverse split announced",
    "going_concern": "going-concern language",
    "acquisition_fixed_price": "acquisition at a fixed price",
    "offering": "offering announced",
    "bankruptcy": "bankruptcy proceedings",
    "delisting": "delisting or listing-compliance notice",
    "going_private": "going-private transaction",
}


@dataclass
class DisqualifyResult:
    disqualified: bool
    rules_hit: list[str]
    detail: list[str]

    def to_dict(self) -> dict:
        return {"disqualified": self.disqualified, "rules_hit": self.rules_hit,
                "detail": self.detail}

    @property
    def summary(self) -> str:
        if not self.disqualified:
            return ""
        return "; ".join(DISQUALIFY_RULES.get(r, r) for r in self.rules_hit)


def check_disqualifiers(
    context,
    *,
    as_of: date | None = None,
    hold_days: int = DEFAULT_HOLD_DAYS,
) -> DisqualifyResult:
    """Apply every rule to one symbol's news context.

    `context` is a NewsContext (or anything with `.headlines`, `.earnings_date`
    and `.filings`).
    """
    as_of = as_of or date.today()
    hits: list[str] = []
    detail: list[str] = []

    # -- earnings ----------------------------------------------------------
    earnings = getattr(context, "earnings_date", None)
    if earnings is not None:
        days = (earnings - as_of).days
        if 0 <= days <= hold_days:
            hits.append("earnings_in_window")
            detail.append(f"earnings on {earnings} ({days}d away, window {hold_days}d)")

    # -- filings by form type ---------------------------------------------
    for filing in getattr(context, "filings", []) or []:
        form = str(filing.get("form", ""))
        if any(form.startswith(f) for f in DILUTION_FORMS):
            if "dilution_filing" not in hits:
                hits.append("dilution_filing")
            detail.append(f"{form} filed {filing.get('filed', '?')}")
        if any(form.startswith(f) for f in CONTROL_FORMS):
            if "going_private" not in hits:
                hits.append("going_private")
            detail.append(f"{form} filed {filing.get('filed', '?')}")

    # -- headline text -----------------------------------------------------
    corpus = " ".join(
        f"{h.get('headline', '')} {h.get('summary', '')}"
        for h in (getattr(context, "headlines", []) or [])
    )
    for rule, pattern in _PATTERNS.items():
        match = pattern.search(corpus)
        if match:
            if rule not in hits:
                hits.append(rule)
            detail.append(f"{rule}: matched {match.group(0)!r}")

    return DisqualifyResult(disqualified=bool(hits), rules_hit=hits, detail=detail)


def upcoming_earnings_note(context, as_of: date | None = None) -> str | None:
    """Earnings outside the window but still worth mentioning in the brief."""
    as_of = as_of or date.today()
    earnings = getattr(context, "earnings_date", None)
    if earnings is None:
        return None
    days = (earnings - as_of).days
    if days > DEFAULT_HOLD_DAYS:
        return f"earnings {earnings} ({days}d out)"
    return None


def hold_window_end(as_of: date, hold_days: int = DEFAULT_HOLD_DAYS) -> date:
    return as_of + timedelta(days=hold_days)
