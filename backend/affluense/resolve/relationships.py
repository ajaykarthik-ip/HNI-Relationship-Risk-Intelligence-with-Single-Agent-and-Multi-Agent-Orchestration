"""What kind of connection is this, exactly?

`relationship_type()` answered with five values, and five is not enough to run
a screening on. "Control" covered a founder-chairman and a CFO who left seven
years ago; a former Grofers CFO role therefore made that company's arrest
coverage the subject's exposure. A television hosting contract arrived as a
company. A minority investment looked the same as a board seat.

Fourteen classes, and only four of them carry exposure:

    current_company · executive_role · board_seat · investment
    former_employer · former_company
    subsidiary · parent · alias
    nonprofit · regulator · government_body · think_tank · trade_association
    media_role · other

Deterministic rules run first and settle almost everything -- they are free,
instant, and testable. A model is asked only about what the rules leave
genuinely unresolved, and its answer is stored with its reasoning so a reader
can see that a machine, not a rule, made that call.
"""

from __future__ import annotations

import json
import re

from .. import config
from ..sources import openai_client
from . import company as company_resolve

# Classes whose adverse coverage is the subject's own exposure.
#
# "Former" appears here on purpose. Whether an event happened while the
# subject was there is the *tenure* check's job, and gating it twice loses
# real findings: Ashneer Grover left BharatPe before the complaint was filed,
# and the complaint is still unambiguously about him.
#
# What separates these from the list below is responsibility, not tense. A
# founder, an executive officer or a director answers for the company's
# conduct. An analyst, an investor, a trustee or a television host does not.
CARRIES_EXPOSURE = (
    "control", "current_company", "former_company", "executive_role",
    "board_seat",
)

# Classes shown in full but never attributed to the person.
REPORTED_NOT_ATTRIBUTED = (
    "former_employer", "employment", "investment",
    "nonprofit", "regulator", "government_body", "think_tank",
    "trade_association", "media_role", "subsidiary", "parent",
)

MEDIA_ROLE = re.compile(
    r"\b(host|presenter|anchor|judge|panell?ist|jury|contestant|"
    r"brand ambassador|ambassador|endorser|face of|spokesperson|"
    r"commentator|columnist|guest)\b",
    re.IGNORECASE,
)

BOARD_ROLE = re.compile(
    r"\b(board member|non-?executive (director|chairman|chairperson)|"
    r"independent director|advisory board|trustee|board of directors)\b",
    re.IGNORECASE,
)

EXECUTIVE_ROLE = re.compile(
    r"\b(chief executive|ceo|managing director|chief financial|cfo|"
    r"chief operating|coo|chief technology|cto|president|"
    r"executive chairman|executive director)\b",
    re.IGNORECASE,
)

OWNER_ROLE = re.compile(
    r"\b(founder|co-?founder|promoter|owner|co-?owner|proprietor|"
    r"chairman|chairperson|chair|majority shareholder|controlling)\b",
    re.IGNORECASE,
)

INVESTOR_ROLE = re.compile(
    r"\b(investor|shareholder|stakeholder|backer|limited partner|"
    r"angel|seed investor)\b",
    re.IGNORECASE,
)

# A mixed role such as "Shareholder / Brand Ambassador" carries a real
# financial interest, and the ownership word outranks the endorsement word.
# Defined here rather than imported from the pipeline, which imports this.
CORPORATE_INTEREST = re.compile(
    r"\b(shareholder|stakeholder|owner|co-?owner|investor|partner|director|"
    r"founder|co-?founder|promoter|chairman|chairperson|proprietor|board)\b",
    re.IGNORECASE,
)


def classify(name: str, roles: list, status: str = "active",
             stake_percent: float | None = None) -> tuple:
    """(class, basis). Deterministic; returns "other" when genuinely unsure.

    Order matters. A public body hands its outside directors exactly the
    titles a controlling role is spelled with, so what the *organisation* is
    outranks what the role is called.
    """
    joined = " ".join(roles or [])
    former = status == "former"

    if company_resolve.PUBLIC_BODY_NAME.search(name or ""):
        if re.search(r"\b(regulat|reserve bank|central bank|securities and "
                     r"exchange|monetary authority)\b", name or "", re.IGNORECASE):
            return "regulator", "a regulator; a seat on it is a public appointment"
        if re.search(r"\b(chamber|confederation|federation|association)\b",
                     name or "", re.IGNORECASE):
            return "trade_association", "an industry body, not a holding"
        return "government_body", "a public body; a seat on it is not a holding"

    if company_resolve.is_philanthropic(name):
        if re.search(r"\b(institut|council on|centre for|center for|endowment|"
                     r"think tank)\b", name or "", re.IGNORECASE):
            return "think_tank", "a research institution, not corporate exposure"
        return "nonprofit", "a charitable or academic body, not corporate exposure"

    if MEDIA_ROLE.search(joined) and not CORPORATE_INTEREST.search(joined):
        return "media_role", f"an on-air or endorsement role ({joined[:60]})"

    if stake_percent is not None and stake_percent >= config.CONTROL_STAKE_PERCENT:
        return ("current_company" if not former else "former_company",
                f"a {stake_percent:g}% holding, at or above the control threshold")

    if stake_percent is not None and stake_percent > 0:
        return "investment", f"a {stake_percent:g}% minority holding"

    if OWNER_ROLE.search(joined):
        return ("former_company" if former else "current_company",
                f"a founding or controlling role ({joined[:60]})")

    if EXECUTIVE_ROLE.search(joined):
        return ("former_employer" if former else "executive_role",
                f"an executive office ({joined[:60]})")

    if BOARD_ROLE.search(joined):
        return "board_seat", f"a board seat ({joined[:60]})"

    if INVESTOR_ROLE.search(joined):
        return "investment", f"an investor relationship ({joined[:60]})"

    if company_resolve.EMPLOYMENT_ROLE.search(joined):
        return "former_employer" if former else "employment", "an employment role"

    return "other", ""


def carries_exposure(relationship_class: str) -> bool:
    """Whether this company's conduct is the subject's own exposure."""
    return relationship_class in CARRIES_EXPOSURE


def legacy_type(relationship_class: str) -> str:
    """Map back onto the five old values.

    The existing validator, report and UI speak the old vocabulary. Keeping a
    translation means this can ship without breaking them, and the old field
    can be dropped in one change later.
    """
    if relationship_class in CARRIES_EXPOSURE:
        return "control"
    if relationship_class in ("regulator", "government_body", "trade_association"):
        return "public_office"
    if relationship_class in ("nonprofit", "think_tank"):
        return "philanthropy"
    if relationship_class in ("former_employer", "employment"):
        return "employment"
    if relationship_class in ("board_seat", "investment", "subsidiary",
                              "parent", "former_company"):
        return "control" if relationship_class == "former_company" else "unknown"
    if relationship_class == "media_role":
        return "employment"
    return "unknown"


def resolve_unknowns(fetcher, records: list, subject: str, say=None) -> int:
    """Ask a model about the records the rules could not place.

    Only the residue, and only ever one call. Anything it returns outside the
    known vocabulary is discarded — a made-up class would flow straight into
    the risk rubric.
    """
    unresolved = [r for r in records if r.get("relationship_class") == "other"]
    if not unresolved:
        return 0

    client = openai_client.OpenAIClient(
        fetcher, budget=1, purpose="relationship classification"
    )
    if not client.available:
        return 0

    valid = set(CARRIES_EXPOSURE) | set(REPORTED_NOT_ATTRIBUTED) | {"other"}

    answer = client.complete_json(
        "Classify how a person is connected to each organisation, using only "
        "the role text supplied. Choose exactly one class from: "
        + ", ".join(sorted(valid)) + ".\n"
        "current_company and executive_role mean the person's conduct and the "
        "organisation's are linked. board_seat, investment, former_employer, "
        "nonprofit, regulator, government_body, think_tank, "
        "trade_association and media_role do not.\n"
        'Reply as JSON: {"records": [{"index": int, "class": str, '
        '"reason": str}]}',
        json.dumps({
            "subject": subject,
            "records": [
                {"index": i, "organisation": r.get("name"),
                 "roles": r.get("relationships", []),
                 "status": r.get("status", "active")}
                for i, r in enumerate(unresolved)
            ],
        }, ensure_ascii=False),
        max_tokens=800,
    )
    if not answer or not isinstance(answer.get("records"), list):
        return 0

    resolved = 0
    for row in answer["records"]:
        if not isinstance(row, dict):
            continue
        index = row.get("index")
        chosen = (row.get("class") or "").strip()
        if not isinstance(index, int) or not 0 <= index < len(unresolved):
            continue
        if chosen not in valid or chosen == "other":
            continue
        record = unresolved[index]
        record["relationship_class"] = chosen
        record["relationship_basis"] = (
            (row.get("reason") or "").strip()
            + f" [classified by {config.OPENAI_MODEL}]"
        ).strip()
        resolved += 1

    if say and resolved:
        say(f"    classified {resolved} ambiguous relationship(s)")
    return resolved
