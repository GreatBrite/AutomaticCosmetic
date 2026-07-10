from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass


USER_AGENT = "Mozilla/5.0 freelance-leads-bot/1.0"


@dataclass(frozen=True)
class RawLead:
    source: str
    title: str
    url: str
    company: str = ""
    budget: str = ""
    description: str = ""
    lead_type: str = "project"


def fetch_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def lead_id(source: str, url: str, title: str) -> str:
    digest = hashlib.sha256(f"{source}|{url}|{title}".encode("utf-8")).hexdigest()
    return digest[:24]


def remoteok() -> list[RawLead]:
    data = json.loads(fetch_text("https://remoteok.com/api"))
    leads: list[RawLead] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("position"):
            continue
        tags = ", ".join(item.get("tags") or [])
        leads.append(
            RawLead(
                source="RemoteOK",
                title=clean(item.get("position", "")),
                url=item.get("url") or f"https://remoteok.com/remote-jobs/{item.get('id', '')}",
                company=clean(item.get("company", "")),
                budget=clean(item.get("salary", "")),
                description=clean(f"{tags} {item.get('description', '')}"),
                lead_type="job",
            )
        )
    return leads


def remotive() -> list[RawLead]:
    data = json.loads(fetch_text("https://remotive.com/api/remote-jobs?category=software-dev"))
    leads: list[RawLead] = []
    for item in data.get("jobs", []):
        leads.append(
            RawLead(
                source="Remotive",
                title=clean(item.get("title", "")),
                url=item.get("url", ""),
                company=clean(item.get("company_name", "")),
                budget=clean(item.get("salary", "")),
                description=clean(f"{item.get('candidate_required_location', '')} {item.get('description', '')}"),
                lead_type="job",
            )
        )
    return leads


def weworkremotely() -> list[RawLead]:
    xml = fetch_text("https://weworkremotely.com/categories/remote-programming-jobs.rss")
    root = ET.fromstring(xml)
    leads: list[RawLead] = []
    for item in root.findall("./channel/item"):
        title = clean(item.findtext("title", ""))
        url = clean(item.findtext("link", ""))
        description = clean(item.findtext("description", ""))
        company = title.split(":", 1)[0] if ":" in title else ""
        leads.append(
            RawLead(
                source="WeWorkRemotely",
                title=title,
                url=url,
                company=company,
                description=description,
                lead_type="job",
            )
        )
    return leads


def absolute_url(base: str, href: str) -> str:
    if href.startswith("http"):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return base.rstrip("/") + href


def freelancer_projects() -> list[RawLead]:
    data = fetch_text("https://www.freelancer.com/jobs/python/")
    leads: list[RawLead] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a[^>]+href="(?P<href>/projects/[^"]+)"[^>]*>(?P<body>.*?)</a>', data, re.S | re.I):
        href = match.group("href").split("?")[0]
        title = clean(match.group("body"))
        if not title or href in seen:
            continue
        if len(title) < 8 or title.lower().startswith(("python", "api", "web scraping", "automation")):
            continue
        seen.add(href)
        start = max(0, match.start() - 1200)
        end = min(len(data), match.end() + 1800)
        context = clean(data[start:end])
        budget_match = re.search(r"(?:\$|€|£)\s?\d[\d,]*(?:\s?-\s?(?:\$|€|£)?\s?\d[\d,]*)?", context)
        leads.append(
            RawLead(
                source="Freelancer",
                title=title[:180],
                url=absolute_url("https://www.freelancer.com", href),
                budget=budget_match.group(0) if budget_match else "",
                description=context[:1800],
                lead_type="project",
            )
        )
        if len(leads) >= 35:
            break
    return leads


def guru_projects() -> list[RawLead]:
    data = fetch_text("https://www.guru.com/d/jobs/c/programming-development/")
    leads: list[RawLead] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a[^>]+href="(?P<href>/jobs/[^"]+)"[^>]*>(?P<body>.*?)</a>', data, re.S | re.I):
        href = match.group("href").split("&")[0]
        title = clean(match.group("body"))
        if not title or href in seen:
            continue
        seen.add(href)
        start = max(0, match.start() - 1000)
        end = min(len(data), match.end() + 1700)
        context = clean(data[start:end])
        budget_match = re.search(r"(?:\$|€|£)\s?\d[\d,]*(?:\s?-\s?(?:\$|€|£)?\s?\d[\d,]*)?", context)
        leads.append(
            RawLead(
                source="Guru",
                title=title[:180],
                url=absolute_url("https://www.guru.com", href),
                budget=budget_match.group(0) if budget_match else "",
                description=context[:1800],
                lead_type="project",
            )
        )
        if len(leads) >= 25:
            break
    return leads


def freelancehunt_projects() -> list[RawLead]:
    xml = fetch_text("https://freelancehunt.com/projects.rss")
    root = ET.fromstring(xml)
    leads: list[RawLead] = []
    for item in root.findall("./channel/item")[:40]:
        title = clean(item.findtext("title", ""))
        url = clean(item.findtext("link", ""))
        description = clean(item.findtext("description", ""))
        if not title or not url:
            continue
        leads.append(
            RawLead(
                source="Freelancehunt",
                title=title,
                url=url,
                description=description,
                lead_type="project",
            )
        )
    return leads


SOURCES = [freelancer_projects, guru_projects, freelancehunt_projects, remoteok, remotive, weworkremotely]
