"""Analyst Agent — the only LLM-native stage.

Regex decided what an article meant. It cannot tell "SEBI clears firm" from
"SEBI probes firm", cannot tell the target company from a namesake, and cannot
read a date out of prose. Those are comprehension problems, and they are the one
place in this pipeline where a model is clearly better than a rule.

Everything about *what* is asked is V1's, imported: the system prompt, the
per-article payload, the validation in `_apply` that overrides the model's own
`actor_is_subject` when the stated role disagrees with it, and the rule that an
id the batch did not contain is discarded rather than guessed at. The model is
never asked to assess overall risk -- that is arithmetic over findings in
`risk_score.py` -- and never asked to recall anything it was not handed.

What changes is that the batches go out together. V1 issues them one after
another inside a serial per-company loop, and each one waits a full round trip
before the next begins.

    in    one entity's articles, the entity and subject names, the role period
    out   article.extracted populated in place, plus V1's tally
    LLM   yes, at temperature 0, JSON mode, run-scoped budget
    fails no key -> keyword annotation and backend "keyword", exactly as V1.
          Budget exhausted -> the remaining articles keep their keyword
          annotation and the run says so. Malformed JSON -> discarded, and the
          deterministic order is left untouched.
"""

from __future__ import annotations

import json

from affluense import config as v1config
from affluense.enrich import extract as v1extract

from .. import config as v2config
from ..concurrency import map_bounded
from ..sources.openai_async import AsyncOpenAIClient


def batches_for(articles: list) -> int:
    """How many calls reading this evidence set would take.

    The orchestrator sums this across entities to size the run's extraction
    budget from the work actually queued, so every article V1 would have sent
    still gets sent. The ceiling bounds a loop bug; it does not trim coverage.
    """
    size = max(1, v1config.OPENAI_EXTRACT_BATCH)
    return (len(articles) + size - 1) // size


async def _run_batch(client, target: str, subject: str, role_period: list,
                     batch: list, offset: int) -> bool:
    """One call for one batch of articles. True if anything was applied."""
    user = json.dumps({
        "target_entity": target,
        "subject_person": subject,
        "subject_role_period": role_period,
        "articles": [v1extract._payload_for(a, offset + i)
                     for i, a in enumerate(batch)],
    }, ensure_ascii=False)

    answer = await client.complete_json(v1extract.SYSTEM, user, max_tokens=2400)
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
            v1extract._apply(article, row)
            applied = True
        except (TypeError, ValueError):
            continue
    return applied


async def analyse(transport, reporter, articles: list, target: str,
                  subject: str, budget, role_period=(None, None)) -> dict:
    """Extract what each article says. Mutates articles in place.

    Returns V1's tally, so the report can state how much of the evidence was
    model-read and how much fell back to keywords.
    """
    tally = {"sent": 0, "read": 0, "dropped_not_about_target": 0,
             "dropped_promotional": 0, "backend": "keyword"}

    client = AsyncOpenAIClient(transport, budget, purpose="evidence analysis")
    if not client.configured or not articles:
        # An entity with no evidence is still an entity this stage is done
        # with. Returning without advancing left the counter reading 5 of 9 on
        # a run where every entity had in fact been handled.
        reporter.advance("analysis")
        return tally

    tally["backend"] = f"openai:{v1config.OPENAI_MODEL}"
    size = max(1, v1config.OPENAI_EXTRACT_BATCH)

    # Batches are built up front, in article order, so the budget is spent on
    # the same evidence whatever order the API answers in.
    batches = [
        (start, articles[start:start + size])
        for start in range(0, len(articles), size)
    ]
    tally["sent"] = sum(len(batch) for _, batch in batches)

    async def one(entry):
        offset, batch = entry
        return await _run_batch(
            client, target, subject, list(role_period), batch, offset,
        )

    await map_bounded(
        batches, one, v2config.MAX_ANALYST_CONCURRENCY,
        on_error=lambda i, e: transport.note(
            f"Reading a batch of {target}'s evidence failed: {e}"
        ),
    )

    for article in articles:
        extracted = article.extracted
        if not extracted:
            continue
        tally["read"] += 1
        if not extracted["is_about_target"]:
            tally["dropped_not_about_target"] += 1
        if extracted["is_promotional"]:
            tally["dropped_promotional"] += 1

    if tally["sent"] and tally["read"] < tally["sent"]:
        reporter.say(
            "    extraction budget reached; remaining articles keep their "
            "keyword annotation"
        )
    if tally["read"]:
        reporter.say(
            f"    read {tally['read']} article(s)"
            f"; dropped {tally['dropped_not_about_target']} about a "
            "different entity"
        )
    reporter.advance("analysis")
    return tally
