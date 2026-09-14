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

from . import people
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
    """Every *person* the extraction named across a group of articles.

    `actors` is not a list of people. The model puts whoever acted in it, and
    for a corporate matter that is the regulator and the company -- "FSSAI",
    "Dabur India Limited". Comparing those against the subject's name never
    matches, so treating them as named individuals made every company matter
    look like somebody else's act and suppressed it.

    Organisations are filtered out here, using the same structural test the
    network list uses: legal forms, single words and collective nouns are not
    people. What survives is the set of actual individuals, which is the only
    thing the question "did somebody else do this?" can be asked about.
    """
    found: list = []
    for article in group:
        extracted = getattr(article, "extracted", None) or {}
        for actor in extracted.get("actors") or []:
            if not isinstance(actor, str) or not actor.strip():
                continue
            name = actor.strip()
            if people.looks_like_person(name):
                found.append(name)
    return found


# Relationship classes where the pipeline established that nothing supports the
# link. Not a list of weak sources -- extraction-only links are still links --
# but of connections it actively could not corroborate.
UNCORROBORATED = ("uncorroborated_seed",)


def is_corroborated(relationship_type: str | None) -> bool:
    """Whether the subject's link to this entity is supported by anything."""
    return (relationship_type or "") not in UNCORROBORATED


def decide(group: list, carries_exposure: bool, subject_name: str | None,
           corroborated: bool = True) -> tuple:
    """(attributed_to_subject, reason). Never raises, never drops evidence.

    Two ways a finding becomes the subject's, and they mean different things:

      personal    the evidence names them as the party involved. Follows the
                  individual wherever it happened, whatever the relationship.

      exposure    the company's own adverse matter -- one where the evidence
                  names organisations and no individual -- carried because
                  they control it. NOT a claim that they did it.

    A named individual who is not the subject takes the finding out of both
    categories: that is the named person's conduct, and no relationship
    converts one person's act into another's.

    Everything else is contextual: reported, evidenced, visible in the JSON,
    and not counted toward a risk level.

    The invariant: exposure requires a relationship that is both real and
    corroborated. An asserted connection nothing supports can never carry an
    entity's matters to a person.
    """
    roles = {
        (getattr(a, "extracted", None) or {}).get("subject_role_in_event")
        for a in group
    }
    roles.discard(None)
    named = _named_individuals(group)
    flagged = any(
        (getattr(a, "extracted", None) or {}).get("actor_is_subject")
        for a in group
    )

    # The subject is in the story without being its subject. "X says Y
    # committed fraud" makes X the accuser, and reporting that as an adverse
    # finding about X is precisely backwards.
    if roles and roles <= set(BYSTANDER_ROLES):
        role = sorted(roles)[0]
        return False, (
            f"the subject appears as {role}, not as the party the matter is "
            "against"
        )

    # PERSONAL. Requires the evidence to name them -- the `actor_is_subject`
    # flag alone says *someone* is the party, never *who*, and against an
    # entity whose name contains a person's name the model reads that person
    # as the subject.
    if flagged and names_subject(named, subject_name):
        return True, "the evidence names the subject as the party involved"

    # WHOSE ACT IS IT. A named individual who is not the subject means the
    # evidence is describing that person's conduct, not the organisation's.
    # One person's act is never another's, however the two are connected --
    # and no relationship, however strong, converts it.
    #
    # An organisation-level matter names organisations: a regulator and the
    # body it acted against. That is why `_named_individuals` filters actors
    # down to people first; without it every corporate matter looked like
    # somebody else's act and was suppressed.
    if named and not names_subject(named, subject_name):
        others = ", ".join(sorted(set(named))[:3])
        return False, (
            f"the evidence names {others} as the party involved, not the "
            "subject; reported as context for the company"
        )

    # COMPANY EXPOSURE. No individual is named, so the matter is the
    # organisation's own -- and someone who controls it carries it, without
    # having done it. Both conditions are required, and the corroboration one
    # is the point of the whole function: an asserted link nothing supports
    # can never carry a company's matters to a person.
    if carries_exposure and corroborated:
        return True, "a matter against a company the subject controls"

    if not carries_exposure:
        return False, "the relationship to this company does not carry exposure"
    return False, (
        "the subject's link to this company is not corroborated, so its "
        "matters are reported as context and do not count toward their risk"
    )
