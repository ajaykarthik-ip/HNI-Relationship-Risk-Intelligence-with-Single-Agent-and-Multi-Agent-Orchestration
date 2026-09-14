"""Identity Agent — who did you mean?

Wholly V1's logic, reached through the bridge. `resolve/person.py` already does
the right thing: a confirmed candidate short-circuits the guess entirely, and
without one the deterministic scorer decides unless a key is set. There is no
version of "faster" that justifies re-implementing identity resolution, because
a wrong subject makes every downstream second wasted.

    in    the typed name, optional company, optional confirmed candidate
    out   Subject, the candidates considered, the biography
    LLM   only for candidate ranking, and only inside V1's own budget
    fails no Wikidata entity resolves to a Subject with wikidata_id=None, and
          the pipeline degrades exactly as V1 does
"""

from __future__ import annotations

from affluense.resolve import person as person_resolve


async def resolve(bridge, reporter, name: str, company: str | None,
                  confirmed: dict | None) -> tuple:
    """Returns (Subject, candidates, biography)."""
    # The run has begun, so the bootstrap stage is over. Without this it sits
    # at 0/1 for the whole run and holds back its share of the progress bar.
    reporter.finish("starting")
    reporter.stage("identity", total=1)
    reporter.say("  Resolving identity ...")

    subject, candidates, bio = await bridge.run(
        person_resolve.resolve, name, company, confirmed=confirmed,
    )

    reporter.advance("identity")
    reporter.finish("identity")
    return subject, candidates, bio
