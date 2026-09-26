"""The two Anthropic blueprints, and the corpus cross-check they make possible.

The shape of this issue was decided by something outside it: **the exam guides are
not public.** They are issued through the Claude Partner Network portal, and
nobody working on this box can read them. So what ships here is the half that can
be sourced and verified -- the seven domains per certification and their published
weights -- and the objective bullets below them wait for 052 and partner access.

That makes two tests here load-bearing in a way the arithmetic ones are not:

- `test_no_anthropic_blueprint_claims_a_source_document_it_does_not_have`
- `test_a_blueprint_with_no_source_document_may_not_go_below_depth_1`

The failure they exist to prevent is somebody "finishing" 018 by typing plausible
bullets from a third-party exam summary. Domain weights are corroborable -- the
corpus agrees to 0.67pp, which is what the rest of this file measures -- but an
objective bullet is corroborable against nothing, so it may only come from a
document with a sha256 beside it. Invented vendor language would land in the
thing 031 classifies against and there would be no way to tell afterwards.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import REPO_ROOT
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from examkb.ingest import ingest
from examkb.models import corpus
from examkb.services import blueprint as blueprint_service

sys.path.insert(0, str(REPO_ROOT / "tools"))
import parse_blueprint as bp  # noqa: E402

TRANSCRIPTIONS = REPO_ROOT / "data" / "blueprints"
ANTHROPIC = TRANSCRIPTIONS / "anthropic"

# The published figures, from the plan's probing. Written out here rather than
# read from the file under test, because a test that reads its own answer from the
# artifact it is checking would pass after any edit at all.
PUBLISHED = {
    "ccao-f": (
        ("Output Evaluation and Validation", 21.0),
        ("Workflow Integration and Solution Design", 16.0),
        ("Governance, Risk, and Responsible Use", 15.0),
        ("Prompting and Task Execution", 14.0),
        ("Product and Model Selection", 12.0),
        ("Configuration and Knowledge Management", 12.0),
        ("Troubleshooting and Optimization", 10.0),
    ),
    "ccar-p": (
        ("Integration", 19.0),
        ("Solution Design & Architecture", 17.0),
        ("Evaluation, Testing & Optimization", 16.0),
        ("Stakeholder Communication & Lifecycle Management", 14.0),
        ("Governance, Safety & Risk Management", 14.0),
        ("Claude Models, Prompting & Context Engineering", 13.0),
        ("Developer Productivity & Operational Enablement", 7.0),
    ),
}

pytestmark = pytest.mark.skipif(
    not ANTHROPIC.is_dir(),
    reason="data/ is gitignored; the transcriptions are not in a fresh checkout",
)


def anthropic() -> dict[str, bp.Blueprint]:
    return {
        blueprint.certification: blueprint
        for blueprint in (bp.parse_file(path) for path in bp.discover(ANTHROPIC))
    }


# --------------------------------------------------------------- the published weights


@pytest.mark.parametrize("certification", sorted(PUBLISHED))
def test_the_published_weights_are_transcribed_exactly(certification: str) -> None:
    """The criterion: 21/16/15/14/12/12/10 and 19/17/16/14/14/13/7."""
    blueprint = anthropic()[certification]

    assert tuple(
        (node.label, node.weight_pct) for node in blueprint.top
    ) == PUBLISHED[certification]


@pytest.mark.parametrize("certification", sorted(PUBLISHED))
def test_the_weights_sum_to_100_and_are_flagged_as_doing_so(certification: str) -> None:
    blueprint = anthropic()[certification]

    assert blueprint.regime == "exact"
    assert blueprint.weights_sum == 100.0
    assert blueprint.weights_sum_to_100 is True


def test_both_certifications_are_transcribed() -> None:
    assert sorted(anthropic()) == ["ccao-f", "ccar-p"]


def test_they_check_clean() -> None:
    """The issue's own verification line."""
    result = subprocess.run(
        [sys.executable, "tools/parse_blueprint.py", "--check", str(TRANSCRIPTIONS)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------------------ provenance


def test_no_anthropic_blueprint_claims_a_source_document_it_does_not_have() -> None:
    """No sha256 is invented for a guide nobody on this box can read.

    017 made `source:` mean "this came from a file, and here is its hash so a
    re-fetch detects drift". A hash of a document that was never saved would make
    `verify_sources` permanently silent and the drift mechanism a decoration.
    """
    for certification, blueprint in anthropic().items():
        for source in blueprint.sources:
            document = (Path(blueprint.path).parent / source.path)
            assert document.is_file(), (
                f"{certification} declares source {source.path!r} with a sha256, "
                f"but no such file is beside it"
            )


def test_the_transcriptions_say_why_there_is_no_source_document() -> None:
    """A reader who finds these later must not think the guide was simply lost."""
    for path in bp.discover(ANTHROPIC):
        text = path.read_text(encoding="utf-8")
        assert "Partner Network" in text, path
        assert "052" in text or "not public" in text, path


def test_a_blueprint_with_no_source_document_may_not_go_below_depth_1() -> None:
    """The rule that keeps invented vendor text out of the corpus.

    A domain weight can be corroborated -- the corpus does it, to 0.67pp. An
    objective bullet cannot be corroborated against anything, so it has to come
    from a document. This fails the day somebody adds a second level without
    adding the guide it was read from, which is exactly the day it should fail.
    """
    for path in bp.discover(TRANSCRIPTIONS):
        blueprint = bp.parse_file(path)
        present = [
            source
            for source in blueprint.sources
            if (path.parent / source.path).is_file()
        ]
        if not present:
            assert blueprint.max_depth == 1, (
                f"{path} goes {blueprint.max_depth} deep with no source document "
                f"beside it; objective bullets need a guide, not a recollection"
            )


# ------------------------------------------------------------------------ the join


def test_all_14_corpus_domains_join_to_a_blueprint_node(real_session: Session) -> None:
    """The criterion, both directions, with zero fuzzy matching."""
    joined = 0
    for certification in ("ccao-f", "ccar-p"):
        check = blueprint_service.cross_check(real_session, certification)
        assert check.unjoined_domains == (), check.unjoined_domains
        assert check.unjoined_nodes == (), check.unjoined_nodes
        joined += len(check.rows)

    assert joined == 14


def test_every_join_is_recorded_as_exact(real_session: Session) -> None:
    methods = set(
        real_session.scalars(sa.select(corpus.BlueprintDomainMap.join_method))
    )

    assert methods == {"exact"}


def test_an_unjoined_domain_is_reported_rather_than_matched_to_its_nearest(
    real_session: Session,
) -> None:
    """Change one label by one character and it stops joining. No near-match."""
    node = real_session.scalars(
        sa.select(corpus.BlueprintNode)
        .join(corpus.Blueprint, corpus.Blueprint.id == corpus.BlueprintNode.blueprint_id)
        .where(corpus.Blueprint.certification_id == "ccao-f")
        .where(corpus.BlueprintNode.label == "Troubleshooting and Optimization")
    ).one()
    real_session.execute(
        sa.delete(corpus.BlueprintDomainMap).where(
            corpus.BlueprintDomainMap.node_id == node.id
        )
    )
    node.label = "Troubleshooting & Optimization"  # `&` for `and` -- one character
    real_session.flush()

    check = blueprint_service.cross_check(real_session, "ccao-f")

    assert check.unjoined_domains == ("Troubleshooting and Optimization",)
    assert check.unjoined_nodes == ("Troubleshooting & Optimization",)
    assert not check.joined_cleanly


def test_the_vendors_own_punctuation_is_what_makes_the_join_work(
    real_session: Session,
) -> None:
    """`and` in one certification, `&` in the other. Both reproduced, both join."""
    labels = set(
        real_session.scalars(
            sa.select(corpus.BlueprintNode.label).where(corpus.BlueprintNode.depth == 1)
        )
    )

    assert "Governance, Risk, and Responsible Use" in labels
    assert "Governance, Safety & Risk Management" in labels


def test_the_join_is_scoped_by_certification(real_session: Session) -> None:
    """Two vendors may both call a domain `Integration`; the map must not cross."""
    rows = real_session.execute(
        sa.select(corpus.Domain.certification_id, corpus.Blueprint.certification_id)
        .join(corpus.BlueprintDomainMap, corpus.BlueprintDomainMap.domain_id == corpus.Domain.id)
        .join(corpus.BlueprintNode, corpus.BlueprintNode.id == corpus.BlueprintDomainMap.node_id)
        .join(corpus.Blueprint, corpus.Blueprint.id == corpus.BlueprintNode.blueprint_id)
    ).all()

    assert rows, "no join rows at all"
    assert all(domain_cert == blueprint_cert for domain_cert, blueprint_cert in rows)


# -------------------------------------------------------------------- the cross-check


@pytest.mark.parametrize("certification", sorted(PUBLISHED))
def test_the_corpus_agrees_with_the_published_weights(
    real_session: Session, certification: str
) -> None:
    """The criterion: ≤ 0.7pp on both certifications."""
    check = blueprint_service.cross_check(real_session, certification)

    assert check.worst_deviation_pp is not None
    assert check.worst_deviation_pp <= blueprint_service.TOLERANCE_PP
    assert check.within_tolerance


def test_the_cross_check_is_reported_never_enforced(real_session: Session) -> None:
    """A corpus that drifts flags. It does not raise, and it does not refuse."""
    domain = real_session.scalars(
        sa.select(corpus.Domain).where(
            corpus.Domain.id == "ccao-f/troubleshooting-and-optimization"
        )
    ).one()
    domain.question_count = 200  # nothing like the published 10%
    real_session.flush()

    check = blueprint_service.cross_check(real_session, "ccao-f")  # no raise

    assert not check.within_tolerance
    assert check.worst_deviation_pp > blueprint_service.TOLERANCE_PP
    assert check.joined_cleanly, "drift is not a join failure"


def test_a_deviation_is_in_percentage_points_not_percent(real_session: Session) -> None:
    check = blueprint_service.cross_check(real_session, "ccao-f")
    row = next(row for row in check.rows if row.label == "Output Evaluation and Validation")

    assert row.published_pct == 21.0
    assert row.corpus_pct == pytest.approx(21.67, abs=0.01)
    assert row.deviation_pp == pytest.approx(0.67, abs=0.01)


def test_the_cross_check_knows_there_is_no_source_document(real_session: Session) -> None:
    check = blueprint_service.cross_check(real_session, "ccao-f")

    assert check.has_source_document is False


def test_asking_about_a_certification_with_no_blueprint_says_so(
    real_session: Session,
) -> None:
    with pytest.raises(blueprint_service.BlueprintError, match="no current blueprint"):
        blueprint_service.cross_check(real_session, "nothing-like-this")


# ---------------------------------------------------------------------- weights_for


def test_weights_for_is_keyed_by_domain_id(real_session: Session) -> None:
    """019 filters on domains, so it must not have to redo the join."""
    weights = blueprint_service.weights_for(real_session, "ccao-f")

    assert sum(weights.values()) == 100.0
    assert weights["ccao-f/output-evaluation-and-validation"] == 21.0
    assert len(weights) == 7


def test_weights_for_is_empty_when_there_is_no_blueprint(real_session: Session) -> None:
    assert blueprint_service.weights_for(real_session, "nothing-like-this") == {}


# ------------------------------------------------------- the weights are editable data


def test_changing_a_weight_in_the_artifact_reaches_the_database(
    tmp_db: Engine, tmp_path: Path
) -> None:
    """The whole point of the format: one number, one file, no code change.

    This is the round trip a person actually performs -- edit the bracket, run
    `make blueprints`, run `examkb ingest` -- and it is asserted end to end so
    that "the percentages live in the artifact" stays true rather than becoming
    true-when-it-was-written.
    """
    source = tmp_path / "blueprints"
    shutil.copytree(ANTHROPIC, source / "anthropic")
    artifact = source / "anthropic" / "ccao-f.blueprint"
    artifact.write_text(
        artifact.read_text(encoding="utf-8")
            .replace("- [21%] Output Evaluation", "- [24%] Output Evaluation")
            .replace("- [10%] Troubleshooting", "- [7%] Troubleshooting"),
        encoding="utf-8",
    )

    kb = tmp_path / "kb"
    shutil.copytree(REPO_ROOT / "kb", kb)
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "parse_blueprint.py"),
         str(source), "--kb", str(kb), "--quiet"],
        check=True, cwd=REPO_ROOT,
    )

    with Session(tmp_db) as session:
        ingest(session, kb)
        session.commit()

        weights = blueprint_service.weights_for(session, "ccao-f")
        assert weights["ccao-f/output-evaluation-and-validation"] == 24.0
        assert weights["ccao-f/troubleshooting-and-optimization"] == 7.0
        assert sum(weights.values()) == 100.0

        # And the cross-check notices, which is what makes it worth having.
        check = blueprint_service.cross_check(session, "ccao-f")
        assert not check.within_tolerance


def test_no_module_hard_codes_the_published_weights() -> None:
    """The artifact is the only place they live.

    A constant in `examkb/` holding 21/16/15/... would be a second copy, and the
    way anybody finds out is by editing the file and watching nothing change.
    """
    offenders = [
        path
        for path in [*(REPO_ROOT / "examkb").rglob("*.py"), *(REPO_ROOT / "tools").rglob("*.py")]
        if "Output Evaluation and Validation" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


# --------------------------------------------------------------------------- the CLI


@pytest.fixture
def cli(run_cli, real_db: Engine):
    """`examkb ...` against a copy of the real projection, never the repo's own."""
    url = str(real_db.url)

    def run(*arguments: str):
        return run_cli(*arguments, env={"DATABASE_URL": url})

    return run


def test_blueprint_check_reports_both_certifications(cli) -> None:
    result = cli("blueprint", "check")

    assert result.returncode == 0, result.stderr
    assert "ccao-f" in result.stdout
    assert "ccar-p" in result.stdout
    assert "within the 0.7pp tolerance" in result.stdout


def test_blueprint_check_says_there_is_no_source_document(cli) -> None:
    result = cli("blueprint", "check", "--cert", "ccao-f")

    assert result.returncode == 0, result.stderr
    assert "no source document recorded" in result.stdout
    assert "ccar-p" not in result.stdout, "--cert should narrow it"


def test_blueprint_check_writes_nothing(cli, real_db: Engine) -> None:
    def rows() -> list:
        with Session(real_db) as session:
            return list(session.execute(sa.select(corpus.BlueprintNode).order_by(
                corpus.BlueprintNode.id)).scalars().all())

    before = [(node.id, node.weight_pct) for node in rows()]
    assert cli("blueprint", "check").returncode == 0
    after = [(node.id, node.weight_pct) for node in rows()]

    assert before == after


# ------------------------------------------------- the fingerprint 018 found in two halves


def test_the_status_fingerprint_is_the_one_ingest_stores() -> None:
    """One definition of "the corpus changed", not two that agree by luck.

    `status.corpus_fingerprint` hashed `shard_rows` while ingest stored
    `corpus_rows`. Those are the same list only while `kb/blueprints/` is empty,
    which it was until this issue -- so the first real blueprint would have made
    every page in the app report "this projection is older than kb/" permanently,
    with `examkb ingest` writing nothing to contradict it. Found by
    `test_every_filter_parameter_is_accepted_by_the_route`, which reads the count
    off the page and got the staleness banner instead.
    """
    from examkb import ingest as ingest_module
    from examkb import status

    status.forget_corpus_fingerprint()

    assert status.corpus_fingerprint(REPO_ROOT / "kb") == ingest_module.kb_fingerprint(
        REPO_ROOT / "kb"
    )


def test_a_blueprint_edit_is_noticed_by_the_cached_fingerprint(tmp_path: Path) -> None:
    """The cache keys on `shards.json`, so blueprints had to be added to the key.

    Otherwise the fingerprint is right and stale at the same time: correct when
    computed, never recomputed after an outline changes.
    """
    from examkb import status

    kb = tmp_path / "kb"
    shutil.copytree(REPO_ROOT / "kb", kb)
    status.forget_corpus_fingerprint()
    before = status.corpus_fingerprint(kb)

    artifact = kb / "blueprints" / "anthropic" / "ccao-f.json"
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace('"weight_pct": 21.0', '"weight_pct": 22.0'),
        encoding="utf-8",
    )

    assert status.corpus_fingerprint(kb) != before, "the cache did not notice"
