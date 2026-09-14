"""Read the evidence and say what it means.

Regex decided what an article meant. It cannot tell "SEBI clears firm" from
"SEBI probes firm", cannot tell the target company from a namesake, and
cannot read a date out of prose. Those are comprehension problems, and they
are the one place in this pipeline where a language model is clearly better
than a rule.

What the model is asked to do: read text it has been handed and report what
is in it, one article at a time, as strict JSON.

What it is never asked to do:

  assess overall risk     that is arithmetic over findings, in risk_score.py,
                          because a compliance verdict has to be reproducible
  recall facts            it sees only the retrieved text; anything it
                          "knows" about the subject is inadmissible here
  merge events            clustering is deterministic, in cluster.py

Failure is designed for. A missing key, a malformed reply, an id that does
not match any article, an unset API key -- each leaves the article annotated
by the existing keyword path instead. Degraded, never empty.
"""

from __future__ import annotations

import json

from .. import config
from ..sources import openai_client

SYSTEM = (
    "You are an evidence analyst for a financial due-diligence tool. "
    "You are given news articles already retrieved for one target entity. "
    "For each article, report ONLY what its text states.\n\n"
    "Rules:\n"
    "- Never use knowledge from outside the supplied text.\n"
    "- If the article is about a different company or person with a similar "
    "name, set is_about_target false and stop there.\n"
    "- stage must reflect how far the matter has legally progressed: "
    "reported, alleged, investigating, charged, settled, dismissed, convicted. "
    "An accusation is not a conviction.\n"
    "- subject_role_in_event says what the subject DID in this event:\n"
    "    accused      an allegation, case or action is against them\n"
    "    defendant    they are being sued or prosecuted\n"
    "    accuser      THEY are alleging wrongdoing by someone else\n"
    "    plaintiff    they brought the case\n"
    "    commentator  they are quoted about someone else's matter\n"
    "    unrelated    the event is not about them at all\n"
    "  Being named in an article is not the same as being the subject of the "
    "allegation. 'X says Y committed fraud' makes X the accuser, not the "
    "accused, and must not be reported as an adverse event about X.\n"
    "- actor_is_subject is true ONLY when subject_role_in_event is accused or "
    "defendant. Never for accuser, plaintiff, commentator or unrelated.\n"
    "- severity 1-5: 1 routine, 2 minor, 3 material, 4 serious, "
    "5 severe (fraud, conviction, sanctions).\n"
    "- confidence 0-1: how clearly the article supports what you reported.\n"
    "- event_date: ISO yyyy-mm-dd or yyyy-mm or yyyy, from the text; null if "
    "the text does not date the event.\n"
    "- If the article reports no adverse event at all, set event_type null.\n\n"
    'Reply as JSON: {"articles": [{"id": int, "is_about_target": bool, '
    '"is_journalism": bool, "is_promotional": bool, "event_type": str|null, '
    '"event_summary": str|null, "event_date": str|null, "stage": str|null, '
    '"actors": [str], "subject_role_in_event": str, "actor_is_subject": bool, '
    '"severity": int, "confidence": number, '
    '"supersedes_earlier_report": bool, "reason": str}]}'
)

# Roles that make an event adverse *for the subject*. Anything else means the
# article names them without accusing them.
ADVERSE_ROLES = ("accused", "defendant")

# Categories the rest of the pipeline understands. The model is free to name
# an event type in its own words; this maps it back onto the taxonomy so the
# UI and the risk rubric see a closed vocabulary.
CATEGORY_ALIASES = {
    "fraud": "fraud", "scam": "fraud", "embezzlement": "fraud",
    "misappropriation": "fraud", "cheating": "fraud", "forgery": "fraud",
    "money laundering": "fraud", "bribery": "corruption", "corruption": "corruption",
    "lawsuit": "litigation", "litigation": "litigation", "court": "litigation",
    "legal dispute": "litigation", "arbitration": "litigation",
    "regulatory": "regulatory", "regulatory action": "regulatory",
    "penalty": "regulatory", "fine": "regulatory", "compliance": "regulatory",
    "investigation": "investigation", "probe": "investigation",
    "enforcement": "investigation", "raid": "investigation",
    "arrest": "arrest", "charges": "arrest", "criminal": "arrest",
    "conviction": "arrest", "indictment": "arrest",
    "insolvency": "insolvency", "bankruptcy": "insolvency", "default": "insolvency",
    "tax": "regulatory", "tax evasion": "regulatory",
    "governance": "governance", "resignation": "governance",
    "accounting": "governance", "audit": "governance",
    "sanctions": "sanctions", "debarment": "sanctions",
}

VALID_STAGES = ("reported", "alleged", "investigating", "charged",
                "settled", "dismissed", "convicted")


def normalise_category(event_type: str | None) -> str | None:
    """Map a free-text event type onto the closed risk taxonomy."""
    if not event_type:
        return None
    lowered = event_type.strip().lower()
    if lowered in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[lowered]
    for phrase, category in CATEGORY_ALIASES.items():
        if phrase in lowered:
            return category
    return "other"


def _payload_for(article, index: int) -> dict:
    """What the model sees for one article.

    Body text when it was fetched, headline and snippet otherwise. The
    publisher is included because "as the company said in a statement" reads
    differently from a wire report, and the date because an article's own
    date bounds when the event could have happened.
    """
    return {
        "id": index,
        "headline": article.headline,
        "publisher": getattr(article, "publisher_name", None) or article.publisher,
        "published": article.published,
        "text": (article.body_text or article.snippet or "")[:4000],
        "has_full_text": bool(article.body_text),
    }


def _apply(article, answer: dict) -> None:
    """Store one validated answer on its article."""
    stage = (answer.get("stage") or "").strip().lower()
    category = normalise_category(answer.get("event_type"))
    role = (answer.get("subject_role_in_event") or "").strip().lower()

    # The model's own actor_is_subject is not trusted on its own: it set it
    # true for "Grover claimed Koladiya committed data theft", which is him
    # accusing someone else. The role has to agree.
    actor_is_subject = bool(answer.get("actor_is_subject", False))
    if role:
        actor_is_subject = role in ADVERSE_ROLES

    article.extracted = {
        "subject_role_in_event": role or None,
        "is_about_target": bool(answer.get("is_about_target", True)),
        "is_journalism": bool(answer.get("is_journalism", True)),
        "is_promotional": bool(answer.get("is_promotional", False)),
        "category": category,
        "event_summary": (answer.get("event_summary") or "").strip() or None,
        "event_date": (answer.get("event_date") or "").strip() or None,
        "stage": stage if stage in VALID_STAGES else None,
        "actors": [a for a in (answer.get("actors") or []) if isinstance(a, str)],
        "actor_is_subject": actor_is_subject,
        "severity": max(1, min(5, int(answer.get("severity") or 1))),
        "confidence": max(0.0, min(1.0, float(answer.get("confidence") or 0))),
        "supersedes_earlier_report": bool(answer.get("supersedes_earlier_report")),
        "reason": (answer.get("reason") or "").strip() or None,
    }

    # The model's reading overrides the keyword guess where it is confident.
    # Where it is not, the keyword annotation stands -- which is the whole
    # point of keeping the deterministic path alive underneath.
    if article.extracted["confidence"] >= 0.5:
        if article.extracted["stage"]:
            article.risk_stage = article.extracted["stage"]
        if category and category != "other":
            article.risk_categories = [category]
        elif article.extracted["is_about_target"] and not category:
            article.risk_categories = []
        if article.extracted["is_promotional"]:
            article.is_promotional = True


def _batch(client, target: str, subject: str, role_period, batch: list,
           offset: int) -> bool:
    """One call for one batch of articles. True if anything was applied."""
    user = json.dumps({
        "target_entity": target,
        "subject_person": subject,
        "subject_role_period": role_period,
        "articles": [_payload_for(a, offset + i) for i, a in enumerate(batch)],
    }, ensure_ascii=False)

    answer = client.complete_json(SYSTEM, user, max_tokens=2400)
    if not answer or not isinstance(answer.get("articles"), list):
        return False

    by_index = {offset + i: a for i, a in enumerate(batch)}
    applied = False
    for row in answer["articles"]:
        if not isinstance(row, dict):
            continue
        article = by_index.get(row.get("id"))
        if article is None:
            # An id the batch did not contain. Discarded rather than guessed
            # at: a misaligned answer would attach one article's event to
            # another's URL.
            continue
        try:
            _apply(article, row)
            applied = True
        except (TypeError, ValueError):
            continue
    return applied


def analyse(fetcher, articles: list, target: str, subject: str,
            role_period=(None, None), say=None) -> dict:
    """Extract what each article says. Mutates articles in place.

    Returns a small tally so the report can state how much of the evidence
    was model-read and how much fell back to keywords.
    """
    tally = {"sent": 0, "read": 0, "dropped_not_about_target": 0,
             "dropped_promotional": 0, "backend": "keyword"}

    client = openai_client.OpenAIClient(
        fetcher, budget=config.OPENAI_MAX_EXTRACT_CALLS, purpose="evidence analysis"
    )
    if not client.available or not articles:
        return tally

    tally["backend"] = f"openai:{config.OPENAI_MODEL}"
    size = max(1, config.OPENAI_EXTRACT_BATCH)

    for start in range(0, len(articles), size):
        batch = articles[start:start + size]
        tally["sent"] += len(batch)
        if not client.available:
            if say:
                say("    extraction budget reached; remaining articles keep "
                    "their keyword annotation")
            break
        _batch(client, target, subject, list(role_period), batch, start)

    for article in articles:
        extracted = article.extracted
        if not extracted:
            continue
        tally["read"] += 1
        if not extracted["is_about_target"]:
            tally["dropped_not_about_target"] += 1
        if extracted["is_promotional"]:
            tally["dropped_promotional"] += 1

    if say and tally["read"]:
        say(f"    read {tally['read']} article(s)"
            f"; dropped {tally['dropped_not_about_target']} about a different entity")
    return tally


def keep_relevant(articles: list) -> list:
    """Articles the model confirmed are about the target and not promotional.

    Articles it never saw are kept: an extraction that did not run is not a
    judgement that the article is irrelevant.
    """
    kept = []
    for article in articles:
        extracted = article.extracted
        if extracted and extracted["confidence"] >= 0.5:
            if not extracted["is_about_target"] or extracted["is_promotional"]:
                continue
        kept.append(article)
    return kept
