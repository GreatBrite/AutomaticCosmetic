from __future__ import annotations

import re


BUDGET_RE = re.compile(r"(?P<currency>[$€£])\s?(?P<amount>\d[\d,]*(?:\.\d+)?)|(?P<amount2>\d[\d,]*(?:\.\d+)?)\s?(?P<currency2>USD|EUR|GBP)", re.I)


SUSPICIOUS_TERMS = [
    "unpaid",
    "free trial",
    "fake review",
    "bypass",
    "ban evasion",
    "captcha solving",
    "rent account",
    "crypto wallet seed",
    "telegram accounts",
    "outside escrow",
]

CLIENT_INFRA_TERMS = [
    "domain",
    "hosting",
    "server",
    "vps",
    "aws",
    "digitalocean",
    "proxy",
    "twilio",
    "openai api",
    "api key",
    "developer account",
]


COMPLEXITY_TERMS = {
    "telegram": 1,
    "bot": 1,
    "scraping": 2,
    "parser": 1,
    "automation": 1,
    "fastapi": 1,
    "api": 1,
    "dashboard": 2,
    "crm": 2,
    "openai": 1,
    "llm": 1,
    "browser": 2,
    "proxy": 2,
    "payment": 2,
}

JOB_TERMS = [
    "full-time",
    "full time",
    "employee",
    "employment",
    "salary",
    "senior software engineer",
    "product manager",
    "devops engineer",
    "director",
    "manager",
    "staff engineer",
    "founding",
    "hiring",
    "resume",
    "interview",
]

PROJECT_TERMS = [
    "build",
    "create",
    "develop",
    "fix",
    "integrate",
    "automation",
    "scraper",
    "scraping",
    "bot",
    "telegram",
    "api",
    "script",
    "dashboard",
    "website",
    "tool",
    "gateway",
    "pipeline",
]


def parse_budget_usd(text: str) -> int | None:
    amounts: list[float] = []
    for match in BUDGET_RE.finditer(text):
        raw = match.group("amount") or match.group("amount2")
        if not raw:
            continue
        try:
            amounts.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    if not amounts:
        return None
    return int(max(amounts))


def estimate_lead(lead: dict) -> dict:
    text = " ".join(str(lead.get(key, "")) for key in ("title", "company", "budget", "description", "matches")).lower()
    lead_type = lead.get("lead_type") or classify_lead_type(text, str(lead.get("source", "")))
    apply_channel = classify_apply_channel(str(lead.get("source", "")), str(lead.get("url", "")))
    budget = parse_budget_usd(text)
    complexity = 1 + sum(weight for term, weight in COMPLEXITY_TERMS.items() if term in text)
    days_min = max(1, min(10, complexity // 2))
    days_max = max(days_min + 1, min(21, complexity + 2))

    suspicious_hits = [term for term in SUSPICIOUS_TERMS if term in text]
    client_infra = [term for term in CLIENT_INFRA_TERMS if term in text]
    risk = "low"
    if suspicious_hits:
        risk = "high"
    elif any(term in text for term in ["proxy", "browser", "scraping", "captcha", "account"]):
        risk = "medium"

    if budget:
        day_rate = max(120, round(budget / days_max))
    else:
        day_rate = 180 if risk == "low" else 250

    budget_note = f"${budget}" if budget else "budget unknown"
    decision = "good_fit"
    if suspicious_hits:
        decision = "skip"
    elif day_rate < 100:
        decision = "maybe_skip"

    return {
        "lead_type": lead_type,
        "apply_channel": apply_channel,
        "budget_usd": budget,
        "budget_note": budget_note,
        "days_min": days_min,
        "days_max": days_max,
        "day_rate_usd": day_rate,
        "risk": risk,
        "suspicious_hits": suspicious_hits,
        "client_infra": client_infra,
        "decision": decision,
    }


def classify_lead_type(text: str, source: str = "") -> str:
    source_l = source.lower()
    if source_l in {"remoteok", "remotive", "weworkremotely"}:
        return "job"
    project_hits = sum(1 for term in PROJECT_TERMS if term in text)
    job_hits = sum(1 for term in JOB_TERMS if term in text)
    if project_hits >= max(1, job_hits):
        return "project"
    return "job"


def classify_apply_channel(source: str, url: str = "") -> str:
    source_l = source.lower()
    if source_l == "freelancer":
        return "research_only"
    if source_l in {"guru", "freelancehunt"}:
        return "can_apply_if_account_ok"
    if source_l in {"remoteok", "remotive", "weworkremotely"}:
        return "job_board"
    return "unknown"
