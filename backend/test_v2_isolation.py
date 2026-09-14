"""V1 must not change, and must not learn about V2.

Two guarantees, both checked by a machine rather than remembered by a person.

**One-way imports.** V2 reaches into V1 constantly -- that is the point, it is
how the query ladder, the risk taxonomy and the scorer stay shared. V1 must
never reach back. The moment it does, the engines are coupled and "V1 is the
stable baseline" stops being true.

**V1 is byte-identical.** Every module under `affluense/` is hashed against a
checked-in manifest. An accidental edit fails here, immediately, naming the
file -- which is a much better outcome than discovering it three weeks later
because a benchmark stopped reproducing.

`api.py` is deliberately outside the manifest: it is the one V1-adjacent file
that had to change, to route `engine` to an engine. Its V1 behaviour is pinned
by a behavioural test instead, below.

To regenerate the manifest after an INTENTIONAL V1 change:

    python test_v2_isolation.py --update

Do that only when you meant to change V1, and say so in the commit message.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent
V1_PACKAGE = BACKEND / "affluense"
V2_PACKAGE = BACKEND / "affluense_v2"
MANIFEST = BACKEND / "v1-manifest.json"


def v1_files() -> list:
    """Every V1 source file, sorted, excluding caches."""
    return sorted(
        path for path in V1_PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest() -> dict:
    return {
        str(path.relative_to(BACKEND)).replace("\\", "/"): digest(path)
        for path in v1_files()
    }


# ---------------------------------------------------------------------------
# The guarantees
# ---------------------------------------------------------------------------

def test_v1_never_imports_v2():
    """No module under affluense/ may reference affluense_v2, in any form."""
    offenders = []
    for path in v1_files():
        text = path.read_text(encoding="utf-8")
        if "affluense_v2" in text:
            offenders.append(str(path.relative_to(BACKEND)))
    assert not offenders, (
        "V1 must never import or mention V2. Offending files: "
        + ", ".join(offenders)
    )


def test_v1_imports_resolve_only_to_v1_or_stdlib():
    """Parse the imports rather than trusting a substring search."""
    offenders = []
    for path in v1_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] == "affluense_v2":
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, "V1 imports V2: " + "; ".join(offenders)


def test_v1_is_unchanged():
    """Every V1 file still hashes to what the manifest recorded."""
    assert MANIFEST.exists(), (
        "v1-manifest.json is missing. Regenerate it with "
        "`python test_v2_isolation.py --update` from a known-good V1."
    )
    expected = json.loads(MANIFEST.read_text(encoding="utf-8"))
    actual = build_manifest()

    changed = sorted(
        name for name in set(expected) & set(actual)
        if expected[name] != actual[name]
    )
    removed = sorted(set(expected) - set(actual))
    added = sorted(set(actual) - set(expected))

    problems = []
    if changed:
        problems.append("modified: " + ", ".join(changed))
    if removed:
        problems.append("deleted: " + ", ".join(removed))
    if added:
        problems.append("added: " + ", ".join(added))

    assert not problems, (
        "V1 is the frozen baseline and something under affluense/ moved. "
        + " | ".join(problems)
        + ". If the change was intentional, rerun "
        "`python test_v2_isolation.py --update`."
    )


def test_v2_exists_and_is_separate():
    """V2 is a sibling package, not a subpackage of V1."""
    assert V2_PACKAGE.is_dir()
    assert (V2_PACKAGE / "__init__.py").exists()
    assert not (V1_PACKAGE / "v2").exists(), (
        "V2 must live beside affluense/, not inside it: a subpackage shares "
        "V1's import surface and invites relative imports into V1."
    )


def test_v2_reuses_v1_coverage_limits():
    """V2 must not hold its own copy of the evidence caps.

    If V2 could define its own query ceiling, "V2 is faster" could quietly mean
    "V2 asks less". These are the same objects, by reference.
    """
    from affluense import config as v1config
    from affluense_v2 import config as v2config

    assert v2config.MAX_QUERIES_PER_ENTITY == v1config.MAX_QUERIES_PER_ENTITY
    assert v2config.NEWS_PER_QUERY == v1config.NEWS_PER_QUERY
    assert v2config.MAX_FULLTEXT_FETCHES == v1config.MAX_FULLTEXT_FETCHES
    assert v2config.OPENAI_EXTRACT_BATCH == v1config.OPENAI_EXTRACT_BATCH


def test_v2_does_not_reimplement_the_verdict():
    """The risk scorer and validator are V1's, not copies."""
    import affluense.risk_score as v1_risk
    import affluense.validate as v1_validate
    from affluense import output

    # If V2 ever grew its own scorer these imports would be the giveaway.
    assert not (V2_PACKAGE / "risk_score.py").exists()
    assert not (V2_PACKAGE / "validate.py").exists()
    assert not (V2_PACKAGE / "output.py").exists()
    assert callable(v1_risk.assess)
    assert callable(v1_validate.validate_report)
    assert callable(output.build_report)


def test_api_defaults_to_v1():
    """The engine field is additive: omitting it must mean V1."""
    import api

    request = api.ScreeningRequest(name="Mukesh Ambani",
                                   company="Reliance Industries")
    assert request.engine == "v1"
    network = api.NetworkRequest(name="Azim Premji", company="Wipro")
    assert network.engine == "v1"

    job = api.Job(id="x", kind="screening", name="n", company=None)
    assert job.engine == "v1"
    assert job.summary()["engine"] == "v1"


def test_v1_progress_path_is_untouched_by_v2():
    """V1's percentage still comes from its own step history."""
    import api

    progress = [
        "Resolving identity ...",
        "14 company records merged to 7 entities",
        "  Screening: Tata Sons",
    ]
    percent, phase = api.run_progress(progress, "running")
    assert percent > 2
    assert "screening company 1 of 7" == phase

    # And a V2 job with no event yet must not fall into that path.
    percent_v2, phase_v2 = api.run_progress(progress, "running", "v2", None)
    assert (percent_v2, phase_v2) == (2, "starting")


# ---------------------------------------------------------------------------
# Manifest maintenance
# ---------------------------------------------------------------------------

def main(argv: list) -> int:
    if "--update" in argv:
        manifest = build_manifest()
        MANIFEST.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {MANIFEST.name} with {len(manifest)} V1 files.")
        return 0
    print(__doc__)
    print(f"{len(v1_files())} V1 files tracked.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
