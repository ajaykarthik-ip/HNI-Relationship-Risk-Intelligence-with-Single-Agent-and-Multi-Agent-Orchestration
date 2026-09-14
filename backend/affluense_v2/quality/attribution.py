"""Whose conduct is this?

`cluster.py:215` decides attribution like this:

    attributed_to_subject = bool(actor_is_subject or carries_exposure)

The `or` is the problem. Once a relationship carries exposure -- a directorship,
a controlling stake -- *every* adverse event at that company becomes the
person's, no matter who the evidence says did it. So a co-founder's alleged
forgery at a company the subject holds a minority stake in is reported as the
subject's finding, and so is a matter in which the subject is the one making the
accusation.

The extraction already has what is needed to tell the difference. It returns
`subject_role_in_event` -- accused, defendant, accuser, plaintiff, commentator,
unrelated -- and `actors`, the people the text actually names. V1 simply never
consults them at cluster level.

The rule here, in one sentence: a company matter is the subject's exposure when
they control the company **and the evidence does not name someone else as the
one who did it**.

Two directions of error are not equal, and this is deliberately asymmetric:

  never drop silently   an unattributed finding is still reported, as company
                        context. It is visible, it is in the JSON, it simply
                        does not drive the person's risk level. Dropping it
                        would be the false negative a due-diligence tool cannot
                        afford.

  absent actor attributes  when no individual is named at all, the matter is
                        the company's own -- a regulatory notice, a penalty --
                        and a person who controls the company does carry it.
"""

from __future__ import annotations

from .text import tokens

# Roles that make the subject the one answering for the event.
ADVERSE_ROLES = ("accused", "defendant")

# Roles that mean the subject appears in the story without being its subject.
# "X says Y committed fraud" makes X the accuser, and reporting that as an
# adverse finding about X is precisely backwards.
BYSTANDER_ROLES = ("accuser", "plaintiff", "commentator", "unrelated", "victim")


def _name_tokens(name: str | None) -> set:
    """Distinctive words in a person's name.

    Initials and one-letter fragments are dropped: matching on "K" would tie
    half a candidate list to the subject.
    """
    return {token for token in tokens(name) if len(token) > 2}


def names_subject(actors, subject_name: str | None) -> bool:
    """Whether any named actor plausibly is the subject.

    Deliberately generous. A miss here means attributing the subject's own
    conduct to nobody, which is the worse failure, so a shared distinctive name
    token counts as a match.
    """
    if not subject_name or not actors:
        return False
    wanted = _name_tokens(subject_name)
    if not wanted:
        return False
    for actor in actors:
        if not isinstance(actor, str):
            continue
        if _name_tokens(actor) & wanted:
            return True
    return False


def _named_individuals(group: list) -> list:
    """Every person the extraction named across a group of articles."""
    found: list = []
    for article in group:
        extracted = getattr(article, "extracted", None) or {}
        for actor in extracted.get("actors") or []:
            if isinstance(actor, str) and actor.strip():
                found.append(actor.strip())
    return found


def decide(group: list, carries_exposure: bool,
           subject_name: str | None) -> tuple:
    """(attributed_to_subject, reason). Never raises, never drops evidence."""

    roles = {
        (getattr(a, "extracted", None) or {}).get("subject_role_in_event")
        for a in group
    }
    roles.discard(None)

    # 1. The model read the text and put the subject in the dock. Theirs,
    #    whatever the relationship to the company is.
    if any(
        (getattr(a, "extracted", None) or {}).get("actor_is_subject")
        for a in group
    ):
        return True, "the evidence names the subject as the party involved"

    # 2. The model read the text and put them somewhere else in the story.
    #    Being named in an article is not being accused in it.
    if roles and roles <= set(BYSTANDER_ROLES):
        role = sorted(roles)[0]
        return False, (
            f"the subject appears as {role}, not as the party the matter is "
            "against"
        )

    if not carries_exposure:
        return False, "the relationship to this entity does not carry exposure"

    # 3. Exposure exists. The question is whether this particular event was
    #    somebody else's act.
    actors = _named_individuals(group)
    if actors and not names_subject(actors, subject_name):
        named = ", ".join(sorted({a for a in actors})[:3])
        return False, (
            f"the evidence names {named} as the party involved, not the "
            "subject; reported as context for the company"
        )

    # 4. No individual named: the matter is the organisation's own, and someone
    #    who controls the organisation carries it.
    return True, "a matter against the company, which the subject controls"
