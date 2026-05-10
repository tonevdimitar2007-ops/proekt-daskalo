import json
import re
import logging
from typing import List, Dict, Any, Optional, Tuple

import requests
import feedparser

log = logging.getртреъетрътLogger("pipeline")

CLICKUP_API = "https://api.clickup.com/api/v2"

# -----------------------------
# HARD EXCLUDE: SECURITY TOPICS
# -----------------------------
SECURITY_NOISE_WORDS = [
    "security", "сигурност", "vulnerability", "уязвим", "cve", "exploit",
    "malware", "малуер"тъхтр, "virus", "вирус", "ransomware", "phishing",
    "zero-trust", "zerotrust", "siem", "edr", "xdr", "threat",
    "intrusion", "hardening", "patch", "пач"
]


# -----------------------------
# UTIL
# -----------------------------
def load_json(path: str, default=None):
    try:
        with open(path, хт6х"r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

ъхйердг
def fetch_feed_items(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    feed = feedparser.parse(source["url"])
    items = []
    max_items = int(source.get("max_items", 3))

    for entry in feed.entries[:max_items]:
        items.append({
            "source": source.get("name", "unknown"),
            "title": entry.get("title", "") or "",
            "url": entry.get("link", "") or "",
            "text": (entry.get("summary", "") or "")[:2400],
            "source_weight": float(source.get("weight", 1.0)),
        })
    return items


def ingest_all_items(sources: List[Dict[str, Any]], max_items_total: int = 200) -> List[Dict[str, Any]]:
    all_items: List[Dict[str, Any]] = []
    for s in sources:
        try:
            items = fetch_feed_items(s)
            all_items.extend(items)
            log.info(f"INGEST | {s.get('name')} | {len(items)}")
        except Exception as e:
            log.warning(f"INGEST_FAIL | {s.get('name')} | {type(e).__name__}: {e}")

    return all_items[:max_items_total]


def clickup_create_task(token: str, list_id: str, name: str, description: str):
    url = f"{CLICKUP_API}/list/{list_id}/task"
    headers = {"Authorization": token, "Content-Type": "application/json"}
    payload = {"name": name[:240], "description": description}

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code >= 400:
            log.warning(f"CLICKUP_FAIL | status={r.status_code}")
    except Exception as e:
        log.warning(f"CLICKUP_EXC | {type(e).__name__}: {e}")


def safe_json_load(s: str) -> Optional[Dict[str, Any]]:
    if not s:
        return None

    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)

    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    chunk = s[start:end + 1]

    try:
        return json.loads(chunk)
    except Exception:
        return None


def clamp_int(x, lo, hi, default=None):
    try:
        x = int(x)
        return max(lo, min(hi, x))
    except Exception:
        return default


# -----------------------------
# SCORING
# -----------------------------
def score_item(d: Dict[str, Any], source_weight: float) -> float:
    leverage = float(d.get("leverage_score", 5))
    conc = float(d.get("concreteness_score", 5))
    tech = float(d.get("techiness_score", 5))
    bs = float(d.get("risk_of_bs", 5))
    saved = float(d.get("effort_saved_per_week_minutes", 0))

    saved_bonus = min(4.0, max(0.0, saved / 150.0))

    base = (leverage * 2.2) + (conc * 1.8) + (tech * 1.4) + saved_bonus
    penalty = (bs * 2.0)

    return (base - penalty) * float(source_weight or 1.0)


# -----------------------------
# EXTRACT DISCOVERY
# -----------------------------
def extract_discovery(
    item: Dict[str, Any],
    ai_call_fn,
    system_prompt: str,
    branches: List[str],
) -> Optional[Dict[str, Any]]:

    prompt = f"""
SOURCE: {item.get("source")}
TITLE: {item.get("title")}
URL: {item.get("url")}
TEXT: {item.get("text")}
""".strip()

    raw = ai_call_fn(prompt, system_prompt)
    d = safe_json_load(raw)
    if not d:
        return None

    if d.get("is_useful") is not True:
        return None

    # SECURITY DROP
    blob = " ".join([
        str(d.get("headline") or ""),
        str(d.get("what_to_build") or ""),
        str(item.get("title") or ""),
        str(item.get("text") or "")
    ]).lower()

    if any(w in blob for w in SECURITY_NOISE_WORDS):
        return None

    area = (d.get("affected_area") or "").strip()
    if area not in branches:
        return None

    # Basic sanity checks
    headline = (d.get("headline") or "").strip()
    steps = d.get("setup_steps")
    metric = (d.get("metric") or "").strip()

    if len(headline) < 6:
        return None
    if not isinstance(steps, list) or len(steps) < 2:
        return None
    if len(metric) < 6:
        return None

    d["effort_minutes"] = clamp_int(d.get("effort_minutes"), 5, 600, 60) or 60
    d["effort_saved_per_week_minutes"] = clamp_int(
        d.get("effort_saved_per_week_minutes"), 0, 2000, 0
    ) or 0

    for k in ["leverage_score", "concreteness_score", "techiness_score", "risk_of_bs"]:
        d[k] = clamp_int(d.get(k), 1, 10, 5) or 5

    d["_origin"] = item
    d["_source_weight"] = float(item.get("source_weight", 1.0))
    d["_score"] = score_item(d, d["_source_weight"])

    return d


# -----------------------------
# FORMAT
# -----------------------------
def format_discovery_task(d: Dict[str, Any]) -> Tuple[str, str]:
    origin = d.get("_origin") or {}
    steps = d.get("setup_steps") or []
    stack = d.get("tech_stack") or []

    steps_txt = "\n".join([f"- {s}" for s in steps])
    stack_txt = ", ".join(stack) if stack else "(не е посочено)"

    name = f"[DISC] {d.get('affected_area')} | {d.get('headline')}"[:240]

    desc = (
        f"Клон: {d.get('affected_area')}\n"
        f"Effort: {d.get('effort_minutes')} мин\n"
        f"Saved/week: {d.get('effort_saved_per_week_minutes')} мин\n\n"
        f"Какво внедряваш:\n{d.get('what_to_build')}\n\n"
        f"Tech stack:\n{stack_txt}\n\n"
        f"Стъпки:\n{steps_txt}\n\n"
        f"Метрика:\n{d.get('metric')}\n\n"
        f"---\n"
        f"Източник: {origin.get('source')}\n"
        f"Пост: {origin.get('title')}\n"
        f"URL: {origin.get('url')}\n"
    )

    return name, desc


# -----------------------------
# MAIN DISCOVERY PIPELINE
# -----------------------------
def run_pipeline_discovery(
    items: List[Dict[str, Any]],
    ai_call_fn,
    system_prompt: str,
    clickup_token: str,
    clickup_list_id: str,
    brain: Dict[str, Any],
    progress_every: int = 15,
):

    branches: List[str] = brain.get("life_branches") or []
    total = len(items)
    picks: List[Dict[str, Any]] = []

    # Extract candidates
    for i, it in enumerate(items, start=1):
        if progress_every and (i % progress_every == 0):
            log.info(f"ITEM_PROGRESS | item={i}/{total}")

        d = extract_discovery(it, ai_call_fn, system_prompt, branches)
        if d:
            picks.append(d)

    if not picks:
        log.info("Няма валидни DISCOVERY предложения.")
        return

    picks.sort(key=lambda x: x["_score"], reverse=True)

    # 1 най-добра задача на клон
    best_per_branch: Dict[str, Dict[str, Any]] = {}
    for d in picks:
        b = d.get("affected_area")
        if b not in best_per_branch:
            best_per_branch[b] = d

    created = 0
    for b in branches:
        if b in best_per_branch:
            d = best_per_branch[b]
            name, desc = format_discovery_task(d)
            if clickup_token and clickup_list_id:
                clickup_create_task(clickup_token, clickup_list_id, name, desc)
            log.info(f"CREATED | {name}")
            created += 1

    log.info(f"DONE | one_per_branch created={created} candidates={len(picks)} total_items={total}")
