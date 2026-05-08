"""Topic extractor — agent-specific topical categories.

The framework's behavioral categories (standard / edge / adversarial / safety /
honesty / multi_turn / performance / custom) describe HOW a test stresses an
agent. Topical categories describe WHAT the test is about — the business
domains the agent actually operates in.

For the Movate FAQ agent these might be: "Movate Services", "Company
Information", "Career & Hiring". For SanDisk Returns Manager they might be:
"Order Lookup", "Refund Eligibility", "Image Validation". These come FROM the
agent definition itself — the LLM reads role / instructions / KB names /
managed_agents and proposes a topic taxonomy specific to this agent.

Topics are an orthogonal axis to behavioral categories: a single test can be
"adversarial × Career & Hiring" or "happy-path × Movate Services". This module
just extracts the topic list; combining it with behavioral categories happens
in the Mix Designer + propose-one flow.

Architecture mirrors the scoring-profile advisor:
  - Cached by agent-definition fingerprint (same agent → same topics, $0)
  - LLM-backed (Claude Haiku for cost) with deterministic-heuristic fallback
  - Public API: `extract_topics(agent_definition) -> TopicExtractionResult`
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ..evaluators.judges import cache as judge_cache


log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_PROVIDER = "anthropic"

# Cap on returned topics — more than 10 makes Mix Designer UI unusable
MAX_TOPICS = 10

# Default total test budget the Mix Designer should aim for when populating
# default per-topic counts. Bolt UI displays this as the initial "Total: N"
# the user starts from, then adjusts each topic count as needed.
DEFAULT_TOTAL_BUDGET = 13


@dataclass
class Topic:
    """One topical category. Bolt renders these as filter chips / sliders."""
    name: str                                # display name, e.g., "Movate Services"
    slug: str                                # canonical slug, e.g., "movate_services"
    description: str                         # 1-sentence what this covers
    relevance: float                         # 0..1, how central this topic is to the agent's purpose
    example_queries: list[str] = field(default_factory=list)
    recommended_count: int = 0               # how many tests the Mix Designer should default to
                                             # for this topic. Computed via Hamilton's method
                                             # against DEFAULT_TOTAL_BUDGET with a floor of 1
                                             # per topic — guarantees every extracted topic
                                             # gets coverage (especially per-sub-agent topics
                                             # for manager agents). Bolt UI populates from this.


@dataclass
class TopicExtractionResult:
    topics: list[Topic]
    source: Literal["llm", "cached", "heuristic"] = "heuristic"
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- LLM contract


_TOPIC_SYSTEM_PROMPT = """\
You are an AI evaluation analyst extracting topical categories from an AI agent's definition.

The agent's evaluation will need a behavioral test mix (standard / edge / adversarial / safety) crossed with topical categories (what the agent is actually about). Your job is to read the agent's role / instructions / knowledge bases / managed_agents and propose a clean topical taxonomy.

Rules:
- 3-8 topics for a single agent. For a MANAGER agent (one that has `managed_agents`), produce one topic PER managed_agent PLUS up to 4 cross-cutting topics that exercise the manager's own delegation logic. So a manager with 5 sub-agents should yield 5-9 topics total.
- Topic names are short noun phrases (2-5 words). Title Case. No emojis.
- Don't invent topics the agent doesn't claim. If the agent's instructions don't mention "Career", don't add a "Career" topic.
- For each topic include 2 example_queries — actual user questions in that topic.
- Relevance is 0-1: how central is this topic to the agent's purpose. Sum need not normalize to 1.
- Order topics by relevance descending.

Manager-agent rule (CRITICAL when `managed_agents` is non-empty):
  Each managed_agent represents a delegation target the manager routes work to. For every entry in `managed_agents`, produce ONE topic that exercises THAT specific sub-agent's responsibility. Phrase the topic name from the sub-agent's `name` and `usage_description`. Example: a sub-agent named "OCR Agent" with usage_description "Identify product type and extract visible text" should yield a topic like "Image OCR & Text Extraction" with the sub-agent's responsibility in the description. Then add separate cross-cutting topics that test the MANAGER's logic (e.g., "Routing & Escalation", "Multi-step Workflow Coordination").

Output JSON only, no prose:
{
  "topics": [
    {
      "name": "Movate Services",
      "slug": "movate_services",
      "description": "Questions about Movate's service lines (CX, IT, ER&D)",
      "relevance": 0.95,
      "example_queries": ["What does Movate do for IT?", "Do you offer customer support?"]
    }
  ]
}
"""


def _system_prompt() -> str:
    return _TOPIC_SYSTEM_PROMPT


def _prompt_sha() -> str:
    return hashlib.sha256(_TOPIC_SYSTEM_PROMPT.encode()).hexdigest()


def _agent_def_summary(agent_def: dict[str, Any]) -> str:
    """Compact representation for the LLM — same shape the scoring-profile
    advisor uses, since both read the same fields."""
    relevant = {
        k: v for k, v in agent_def.items()
        if k in {
            "name", "description", "agent_role", "agent_instructions",
            "agent_goal", "agent_context", "tools", "managed_agents",
            "features",
        }
    }
    s = json.dumps(relevant, default=str, indent=2)
    if len(s) > 6000:
        s = s[:5800] + "\n... (truncated)"
    return s


def _cache_key(agent_def: dict[str, Any]) -> str:
    fingerprint = json.dumps({k: v for k, v in agent_def.items() if k != "api_key"},
                              sort_keys=True, default=str)
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def _slugify(name: str) -> str:
    """Convert a display name to a slug. Used as fallback if LLM omits slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return s or "topic"


def _recommend_counts(topics: list[Topic], total_budget: int = DEFAULT_TOTAL_BUDGET) -> None:
    """Mutate `topics` in-place, setting `recommended_count` on each.

    Algorithm (Hamilton's largest-remainder, with a floor of 1 per topic):
      1. Every topic gets `floor = 1` first. This is the coverage guarantee —
         every extracted topic ends up with at least one test scenario, so
         no part of the agent's surface area goes untested by default.
      2. Remaining budget (`total_budget - len(topics)`) is distributed by
         relevance using Hamilton's largest-remainder method (same algorithm
         the 2D mix expansion uses for behavioral category allocation).
      3. If `total_budget < len(topics)`, every topic still gets 1 (so the
         floor wins over the budget — better to over-allocate than starve
         a topic of coverage).

    Notes:
      - Topics are assumed already sorted by relevance descending; we don't
        re-sort here.
      - `relevance` of 0 is treated as 0.001 to avoid division-by-zero when
        every topic somehow has relevance 0 (heuristic fallback edge case).
    """
    n = len(topics)
    if n == 0:
        return
    # Floor: every topic gets 1
    for t in topics:
        t.recommended_count = 1

    if total_budget <= n:
        # Budget is tight — keep the floor of 1 each, no leftover to distribute.
        return

    leftover = total_budget - n
    # Allocate leftover proportionally to relevance.
    relevances = [max(t.relevance, 0.001) for t in topics]
    norm = sum(relevances)
    fractional = [(leftover * r / norm) for r in relevances]
    floors = [int(f) for f in fractional]
    remainder = leftover - sum(floors)

    # Distribute the remainder by largest fractional part (Hamilton).
    remainders = sorted(
        ((i, fractional[i] - floors[i]) for i in range(n)),
        key=lambda kv: (-kv[1], kv[0]),
    )
    for i in range(remainder):
        floors[remainders[i % n][0]] += 1

    for i, t in enumerate(topics):
        t.recommended_count += floors[i]


def _strip_lyzr_hash(rag_name: str) -> str:
    """Remove the 4-char hash suffix Lyzr appends to KB names.

    Lyzr-generated KBs have names like `movate_website_knowledge_baseyg9f`
    or `sandisk_ordersvnzb`, where the trailing 4 chars are a random hash
    glued onto the last word. We strip to get the human-readable form
    (`movate_website_knowledge_base`, `sandisk_orders`).

    Strip criteria — the suffix must be:
      - exactly 4 lowercase alphanumeric chars (so `2024`-style year endings
        which are pure-numeric still get caught — they have at least one
        digit, see below)
      - either contain a digit (e.g., `yg9f` — Lyzr's mixed alpha-num style)
        OR contain no vowels (e.g., `vnzb` — Lyzr's letters-only style; real
        English 4-char endings nearly always have a vowel)

    AND the prefix must end in a real underscore-bounded word of reasonable
    length (so we don't over-strip from names like `assistantv3` with no
    separator, or from names where stripping would leave a 1-char word).
    """
    if len(rag_name) < 8:
        return rag_name
    suffix = rag_name[-4:]
    if not re.match(r"^[a-z0-9]{4}$", suffix):
        return rag_name
    has_digit = any(c.isdigit() for c in suffix)
    has_vowel = any(c in "aeiou" for c in suffix)
    if not has_digit and has_vowel:
        # Likely a real word ending (e.g. `base`, `lite`, `pro_`) — keep.
        return rag_name
    prefix = rag_name[:-4]
    last_sep = max(prefix.rfind("_"), prefix.rfind("-"))
    if last_sep == -1:
        return rag_name
    trailing_word_len = len(prefix) - last_sep - 1
    if trailing_word_len < 2 or trailing_word_len > 12:
        return rag_name
    return prefix


def _parse_llm_response(raw: dict[str, Any], source: str) -> TopicExtractionResult:
    """Defensive parse — tolerates malformed LLM output."""
    topics: list[Topic] = []
    seen_slugs: set[str] = set()
    for entry in (raw.get("topics") or [])[:MAX_TOPICS]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()[:60]
        if not name:
            continue
        slug = str(entry.get("slug") or "").strip()
        if not slug:
            slug = _slugify(name)
        else:
            slug = _slugify(slug)
        # Deduplicate slugs — keep first occurrence
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        try:
            relevance = float(entry.get("relevance", 0.5))
        except (TypeError, ValueError):
            relevance = 0.5
        relevance = max(0.0, min(1.0, relevance))
        examples = [str(q).strip()[:200] for q in (entry.get("example_queries") or [])[:5]
                    if str(q).strip()]
        topics.append(Topic(
            name=name,
            slug=slug,
            description=str(entry.get("description") or "").strip()[:300],
            relevance=relevance,
            example_queries=examples,
        ))

    # Sort by relevance descending; stable secondary by name
    topics.sort(key=lambda t: (-t.relevance, t.name))
    return TopicExtractionResult(topics=topics, source=source)  # type: ignore[arg-type]


# ---------------------------------------------------------------- heuristic fallback


def _heuristic_extract(agent_def: dict[str, Any]) -> TopicExtractionResult:
    """Deterministic fallback when LLM unavailable.

    Strategy:
      1. Read knowledge_base names from features.lyzr_rag.rag_name — those
         encode topical scope (e.g. "movate_website_knowledge_base")
      2. Read managed_agents names — each managed agent typically corresponds
         to a topical responsibility (e.g. "OCR Agent" → "Image Processing")
      3. If neither yields topics, derive ONE topic from the agent's name +
         description so the result is never empty.
    """
    topics: list[Topic] = []
    notes: list[str] = ["LLM advisor unavailable; topics derived heuristically from agent definition."]

    features = agent_def.get("features") or []
    if isinstance(features, list):
        for feat in features:
            if not isinstance(feat, dict):
                continue
            cfg = feat.get("config") or {}
            rag = (cfg.get("lyzr_rag") if isinstance(cfg, dict) else None) or {}
            rag_name = str(rag.get("rag_name") or "").strip()
            if rag_name:
                # Lyzr appends a 4-char hash like "yg9f" glued to the last
                # word of rag_name. Strip it before titling: only when the
                # resulting prefix still ends in a real underscore-bounded word
                # (so we don't over-strip names like "assistantv3").
                cleaned = _strip_lyzr_hash(rag_name)
                # "movate_website_knowledge_base" → "Movate Website Knowledge Base"
                name = re.sub(r"[_\-]+", " ", cleaned).title().strip()
                topics.append(Topic(
                    name=name[:60],
                    slug=_slugify(name),
                    description=f"Questions answered from the '{rag_name}' knowledge base",
                    relevance=0.85,
                    example_queries=[],
                ))

    managed = agent_def.get("managed_agents") or []
    if isinstance(managed, list):
        for entry in managed:
            if not isinstance(entry, dict):
                continue
            sub_name = str(entry.get("name") or "").strip()
            sub_use = str(entry.get("usage_description") or "").strip()
            if not sub_name:
                continue
            # Strip Lyzr decorations like "(R) OCR Agent [Returns Mgr]"
            cleaned = re.sub(r"[\(\[].*?[\)\]]", "", sub_name).strip()
            if not cleaned:
                cleaned = sub_name
            topics.append(Topic(
                name=cleaned[:60],
                slug=_slugify(cleaned),
                description=sub_use[:300] or f"Tasks handled by the {cleaned} sub-agent",
                relevance=0.75,
                example_queries=[],
            ))

    # Fallback: derive one general topic from name + role
    if not topics:
        agent_name = str(agent_def.get("name") or "").strip()
        agent_role = str(agent_def.get("agent_role") or "").strip()
        topic_name = (agent_name or agent_role or "General").split("(")[0].strip()[:60]
        topics.append(Topic(
            name=topic_name,
            slug=_slugify(topic_name),
            description=str(agent_def.get("description") or agent_role)[:300],
            relevance=0.6,
            example_queries=[],
        ))
        notes.append("No knowledge bases or managed agents detected; produced a single general topic.")

    # Dedup by slug + cap at MAX_TOPICS
    seen: set[str] = set()
    unique: list[Topic] = []
    for t in topics:
        if t.slug in seen:
            continue
        seen.add(t.slug)
        unique.append(t)
        if len(unique) >= MAX_TOPICS:
            break

    unique.sort(key=lambda t: (-t.relevance, t.name))
    return TopicExtractionResult(topics=unique, source="heuristic", notes=notes)


# ---------------------------------------------------------------- public API


# ---------------------------------------------------------------- managed_agents coverage


def _strip_managed_agent_decorations(name: str) -> str:
    """Strip Lyzr-style decorations: '(R) OCR Agent [Returns Mgr]' → 'OCR Agent'."""
    cleaned = re.sub(r"[\(\[].*?[\)\]]", "", name).strip()
    return cleaned or name


def _is_managed_agent_covered(
    sub_name: str, sub_usage: str, topics: list[Topic],
) -> bool:
    """Heuristic check: does any existing topic plausibly correspond to this
    managed_agent? Match by (in order, any one is sufficient):

      1. Slug equality on the cleaned name
      2. Cleaned-name (sans trailing 'Agent') as a case-insensitive substring
         of any topic's name+description. Catches the common case where the
         LLM phrased a topic like 'Image OCR Processing' for an 'OCR Agent'
         sub-agent — the substring 'OCR' appears verbatim in the topic.
      3. Salient words (len >= 3, non-stopword) from cleaned_name OR the
         first sentence of usage_description appearing in any topic's
         name+description. Threshold is 3 (not 4) so common acronyms like
         OCR, KB, API match — agent names commonly use these.
    """
    cleaned_name = _strip_managed_agent_decorations(sub_name)
    target_slug = _slugify(cleaned_name)
    if any(t.slug == target_slug for t in topics):
        return True

    # 2. Whole-cleaned-name substring match. Strip trailing 'Agent' so we
    # don't false-positive on every topic that mentions 'agent'. Also strip
    # leading articles. Require the resulting core to be at least 2 chars
    # so we don't false-positive on single-letter agent names.
    core = re.sub(r"\s+(?:Agent|Bot|Service|Module)$", "", cleaned_name, flags=re.IGNORECASE)
    core = core.strip()
    if len(core) >= 2:
        core_lower = core.lower()
        for t in topics:
            haystack = (t.name + " " + t.description).lower()
            if core_lower in haystack:
                return True

    # 3. Salient-word match (broad fallback for multi-word names where the
    # whole-name substring didn't match — e.g. an "OCR Agent" might be
    # covered by a topic mentioning "extraction" but not "OCR" verbatim).
    STOPWORDS = {
        "agent", "tool", "task", "tasks", "this", "that", "with", "from",
        "their", "your", "user", "users", "system", "main", "role",
        "and", "any", "the", "for", "bot",
    }
    words: set[str] = set()
    for src in (cleaned_name, sub_usage.split(".")[0] if sub_usage else ""):
        for w in re.findall(r"[A-Za-z][A-Za-z]+", src):
            wl = w.lower()
            if len(wl) >= 3 and wl not in STOPWORDS:
                words.add(wl)
    if not words:
        return False

    for t in topics:
        haystack = (t.name + " " + t.description).lower()
        if any(w in haystack for w in words):
            return True
    return False


def _ensure_managed_agent_coverage(
    result: TopicExtractionResult, agent_def: dict[str, Any],
) -> TopicExtractionResult:
    """Post-process: for each managed_agent in the agent definition, ensure
    at least one topic covers it. If the LLM/heuristic missed one, synthesise
    a topic from the sub-agent's name + usage_description.

    This guarantees that a manager agent's evaluation has a test target for
    every sub-agent it delegates to — the user's mental model is "I want to
    test how the manager hands off to each sub-agent," and that requires at
    least one topic per sub-agent.
    """
    managed = agent_def.get("managed_agents") or []
    if not isinstance(managed, list) or not managed:
        # Non-manager — no coverage adjustment needed, but still recommend
        # default counts so the Mix Designer can populate sensible defaults.
        _recommend_counts(result.topics)
        return result

    added_for: list[str] = []
    for entry in managed:
        if not isinstance(entry, dict):
            continue
        sub_name = str(entry.get("name") or "").strip()
        if not sub_name:
            continue
        sub_usage = str(entry.get("usage_description") or "").strip()
        if _is_managed_agent_covered(sub_name, sub_usage, result.topics):
            continue
        # Synthesise — relevance 0.7 (mid-tier; LLM-derived topics that the
        # LLM thought were salient should still rank above synthesised ones).
        cleaned = _strip_managed_agent_decorations(sub_name)
        result.topics.append(Topic(
            name=cleaned[:60],
            slug=_slugify(cleaned),
            description=(
                sub_usage[:300]
                or f"Tasks delegated to the {cleaned} sub-agent in this manager system"
            ),
            relevance=0.70,
            example_queries=[],
        ))
        added_for.append(cleaned)

    if added_for:
        result.notes.append(
            "Added topic(s) for uncovered managed_agent(s) to ensure each "
            "sub-agent has a dedicated test target: " + ", ".join(added_for)
        )
        # Re-sort by relevance descending (synthesised topics will appear
        # below LLM-derived ones at the same relevance).
        result.topics.sort(key=lambda t: (-t.relevance, t.name))
        # Honor MAX_TOPICS but allow growth when the manager has many subs:
        # the cap is `max(MAX_TOPICS, num_managed_agents + 4)` so a manager
        # with 8 sub-agents can return 12 topics if needed.
        cap = max(MAX_TOPICS, len(managed) + 4)
        if len(result.topics) > cap:
            result.topics = result.topics[:cap]

    # Recommended per-topic test counts. Always runs (single-task agents too)
    # so every topic ships with `recommended_count >= 1` — that's the coverage
    # guarantee: no extracted topic is orphaned at zero by default.
    _recommend_counts(result.topics)
    return result


# ---------------------------------------------------------------- public API


def extract_topics(agent_definition: dict[str, Any]) -> TopicExtractionResult:
    """Sync entry point. Use from CLI / tests."""
    return asyncio.run(extract_topics_async(agent_definition))


async def extract_topics_async(agent_definition: dict[str, Any]) -> TopicExtractionResult:
    """Async entry point — preferred from FastAPI handlers.

    Cached by agent-definition fingerprint. LLM-backed with heuristic fallback;
    always returns a non-empty topic list. For manager agents (those with
    `managed_agents`), guarantees one topic per sub-agent — synthesising any
    the LLM/heuristic missed (see `_ensure_managed_agent_coverage`).
    """
    cache_key = _cache_key(agent_definition)
    cached = judge_cache.get(DEFAULT_PROVIDER, DEFAULT_MODEL,
                             _system_prompt(), cache_key, 0.0)
    if cached:
        result = _parse_llm_response(cached, source="cached")
        return _ensure_managed_agent_coverage(result, agent_definition)

    try:
        from ..evaluators.judges.llm_clients import call_judge
    except ImportError:
        result = _heuristic_extract(agent_definition)
        return _ensure_managed_agent_coverage(result, agent_definition)

    user_msg = "# Agent definition\n```json\n" + _agent_def_summary(agent_definition) + "\n```"
    try:
        raw = await call_judge(
            DEFAULT_PROVIDER, DEFAULT_MODEL,
            _system_prompt(), user_msg, temperature=0.0,
            # Manager-aware mode produces 8-12 topics for large managers
            # (one per sub-agent + cross-cutting). Each topic is ~300 bytes
            # of JSON. 1024 was tight; 2048 leaves headroom.
            max_tokens=2048,
        )
    except Exception as e:
        log.warning(f"topic_extractor LLM failed: {type(e).__name__}: {e}; using heuristic")
        result = _heuristic_extract(agent_definition)
        result.notes.append(f"LLM call failed: {type(e).__name__}")
        return _ensure_managed_agent_coverage(result, agent_definition)

    judge_cache.put(DEFAULT_PROVIDER, DEFAULT_MODEL,
                    _system_prompt(), cache_key, 0.0, raw)
    parsed = _parse_llm_response(raw, source="llm")
    if not parsed.topics:
        # LLM returned empty / malformed → use heuristic
        result = _heuristic_extract(agent_definition)
        result.notes.append("LLM returned no parseable topics; used heuristic fallback.")
        return _ensure_managed_agent_coverage(result, agent_definition)
    return _ensure_managed_agent_coverage(parsed, agent_definition)
