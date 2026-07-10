from __future__ import annotations

import json

from .config import Settings
from .evaluator import estimate_lead
from .sources import SOURCES, RawLead, lead_id
from .storage import LeadStore


def score_lead(lead: RawLead, settings: Settings) -> tuple[int, list[str]]:
    text = f"{lead.title} {lead.company} {lead.description}".lower()
    matched = [kw for kw in settings.keywords if kw.lower() in text]
    blocked = [kw for kw in settings.negative_keywords if kw.lower() in text]
    score = len(matched) - (len(blocked) * 2)
    if "contract" in text or "freelance" in text:
        score += 1
    if lead.budget:
        score += 1
    if lead.lead_type == "project":
        score += 2
    else:
        score -= 5
    return score, matched[:6]


def normalize_lead(lead: RawLead, score: int, matches: list[str]) -> dict:
    result = {
        "id": lead_id(lead.source, lead.url, lead.title),
        "source": lead.source,
        "title": lead.title,
        "url": lead.url,
        "company": lead.company,
        "budget": lead.budget,
        "description": lead.description[:2500],
        "lead_type": lead.lead_type,
        "score": score,
        "matches": matches,
    }
    result["estimate"] = estimate_lead(result)
    result["estimate_json"] = json.dumps(result["estimate"], ensure_ascii=False)
    result["status"] = "new" if result["estimate"].get("lead_type") == "project" else "job"
    result["lead_type"] = result["estimate"].get("lead_type", lead.lead_type)
    result["apply_channel"] = result["estimate"].get("apply_channel", "unknown")
    return result


def scan(settings: Settings, store: LeadStore) -> tuple[list[dict], list[str]]:
    candidates: list[dict] = []
    found: list[dict] = []
    errors: list[str] = []
    for source in SOURCES:
        try:
            for raw in source():
                if not raw.title or not raw.url:
                    continue
                score, matches = score_lead(raw, settings)
                if score < settings.min_score:
                    continue
                lead = normalize_lead(raw, score, matches)
                candidates.append(lead)
        except Exception as exc:
            errors.append(f"{source.__name__}: {exc}")
    candidates.sort(key=lambda item: item["score"], reverse=True)
    for lead in candidates:
        try:
            if store.add_if_new(lead):
                found.append(lead)
                if len(found) >= settings.max_items_per_run:
                    break
        except Exception as exc:
            errors.append(f"store: {exc}")
            break
    return found, errors
