"""The blueprint artifact format, its parser, and the projection it loads into.

Everything here follows from one finding: **blueprints are not machine-readable**.
Zero of seven vendors publishes JSON, an API or a feed, and the PDFs that exist use
subset CID fonts nothing on this box can read. So a person types the outline out,
and the tests that matter are the ones about not damaging what they typed and about
telling them exactly where they slipped.

Two of them are about refusing to be clever:

- **No CHECK rejects weights that do not sum to 100.** AZ-104's ranges bound to
  80/105 and SnowPro's to 80/110, and both are correct transcriptions of correct
  documents. The test inserts a blueprint summing to 99 straight into SQLite to
  prove the database really does accept it.
- **Labels are stored byte for byte.** 018's join matches the vendor's own
  punctuation with zero fuzzy matching, so a smart quote that becomes a straight
  one here is a domain that silently stops joining there.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import REPO_ROOT
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from examkb import ingest as ingest_module
from examkb.compat import blueprint_files, load_blueprints
from examkb.ingest import ingest, kb_fingerprint
from examkb.models import corpus

sys.path.insert(0, str(REPO_ROOT / "tools"))
import parse_blueprint as bp  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "blueprints"
SHA = "a" * 64


def artifact(body: str, **headers: str) -> str:
    """A minimal valid artifact, with `body` as its outline."""
    base = {"certification": "x-1", "vendor": "acme", "regime": "absent"}
    base.update(headers)
    head = "\n".join(f"{key}: {value}" for key, value in base.items())
    return f"{head}\nsource: guide.pdf sha256={SHA}\n\n{body}"


@pytest.fixture
def kb_with_blueprints(tmp_path: Path) -> Path:
    """The real corpus plus the four fixture blueprints, parsed into `kb/`.

    **The four, and only the four.** `kb/blueprints/` is emptied first: 018 put two
    real Anthropic transcriptions in there, and inheriting them would silently turn
    every count in this file from four into six.
    """
    kb = tmp_path / "kb"
    shutil.copytree(REPO_ROOT / "kb", kb)
    shutil.rmtree(kb / "blueprints", ignore_errors=True)
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "parse_blueprint.py"),
         str(FIXTURES), "--kb", str(kb), "--quiet"],
        check=True, cwd=REPO_ROOT,
    )
    return kb


# ------------------------------------------------------------------ the four regimes


def test_there_are_four_fixtures_one_per_regime() -> None:
    parsed = [bp.parse_file(path) for path in bp.discover(FIXTURES)]

    assert {blueprint.regime for blueprint in parsed} == set(bp.REGIMES)
    assert {blueprint.certification for blueprint in parsed} == {
        "az-104", "dbt-analytics-engineering", "cof-c03", "saa-c03",
    }


def test_all_four_check_clean() -> None:
    """The issue's own verification line."""
    result = subprocess.run(
        [sys.executable, "tools/parse_blueprint.py", "--check", str(FIXTURES)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "checked 4 blueprint(s)" in result.stdout


def test_all_four_project_without_a_constraint_violation(
    tmp_db: Engine, kb_with_blueprints: Path
) -> None:
    """The criterion. Ranges, absent weights and an unpublished guide all load."""
    with Session(tmp_db) as session:
        ingest(session, kb_with_blueprints)
        session.commit()

        rows = session.execute(
            sa.select(
                corpus.Blueprint.id,
                corpus.Blueprint.weight_regime,
                corpus.Blueprint.weights_sum,
                corpus.Blueprint.weights_sum_to_100,
                corpus.Blueprint.max_depth,
            ).order_by(corpus.Blueprint.id)
        ).all()

    assert [row[0] for row in rows] == [
        "aws/saa-c03/Version 1.1",
        "dbt/dbt-analytics-engineering/Version 1.0",
        "microsoft/az-104/Version 1.0",
        "snowflake/cof-c03/COF-C03",
    ]
    by_id = {row[0]: row for row in rows}
    assert by_id["aws/saa-c03/Version 1.1"][1:] == ("exact", 100.0, True, 4)
    assert by_id["microsoft/az-104/Version 1.0"][1:] == ("range", 80.0, None, 2)
    assert by_id["dbt/dbt-analytics-engineering/Version 1.0"][1:] == ("absent", None, None, 2)
    assert by_id["snowflake/cof-c03/COF-C03"][1:] == ("unpublished", None, None, 2)


def test_az_104_bounds_to_eighty_over_one_oh_five() -> None:
    """The plan's own number, and the reason there is no sum-to-100 CHECK."""
    az = next(b for b in (bp.parse_file(p) for p in bp.discover(FIXTURES)) if b.certification == "az-104")

    assert (az.weights_sum, az.weights_sum_max) == (80.0, 105.0)
    assert az.weights_sum_to_100 is None, "a range has no single sum to compare"


def test_the_database_accepts_weights_that_do_not_sum_to_100(tmp_db: Engine) -> None:
    """The criterion, asserted against SQLite rather than against the parser.

    A blueprint summing to 99 is inserted directly. If a CHECK ever appears, this
    is what catches it -- the parser could be bypassed, the schema cannot.
    """
    with Session(tmp_db) as session:
        run_id = session.scalar(sa.select(corpus.IngestRun.id).limit(1))
        session.add(
            corpus.Certification(
                id="acme", name="Acme", vendor="acme", question_count=0, ingest_run_id=run_id
            )
        )
        session.flush()
        session.add(
            corpus.Blueprint(
                id="acme/x/1",
                certification_id="acme",
                vendor="acme",
                weight_regime="exact",
                weights_sum=99.0,
                weights_sum_to_100=False,
                max_depth=1,
                ingest_run_id=run_id,
            )
        )
        session.commit()

        stored = session.get(corpus.Blueprint, "acme/x/1")
        assert stored.weights_sum == 99.0
        assert stored.weights_sum_to_100 is False


# ------------------------------------------------------------------------ depth 4


def test_the_depth_four_tree_survives_the_projection(
    tmp_db: Engine, kb_with_blueprints: Path
) -> None:
    """The criterion: AWS's typed bucket level is intact on the way out."""
    with Session(tmp_db) as session:
        ingest(session, kb_with_blueprints)
        session.commit()

        rows = session.execute(
            sa.select(corpus.BlueprintNode)
            .where(corpus.BlueprintNode.blueprint_id == "aws/saa-c03/Version 1.1")
            .order_by(corpus.BlueprintNode.path)
        ).scalars().all()

    by_depth = {}
    for node in rows:
        by_depth.setdefault(node.depth, []).append(node)

    assert set(by_depth) == {1, 2, 3, 4}
    assert [node.kind for node in by_depth[1]][:1] == ["Domain"]
    assert {node.kind for node in by_depth[2]} == {"Task Statement"}
    assert {node.kind for node in by_depth[3]} == {"Bucket"}
    assert {node.kind for node in by_depth[4]} == {"Bullet"}
    assert {node.label for node in by_depth[3]} == {"Knowledge of:", "Skills in:"}

    deepest = by_depth[4][0]
    assert deepest.parent_id.endswith("#1.1.1")
    assert deepest.path.count(".") == 3


def test_a_node_below_the_top_carries_no_weight(tmp_db: Engine, kb_with_blueprints: Path) -> None:
    with Session(tmp_db) as session:
        ingest(session, kb_with_blueprints)
        session.commit()

        weighted_depths = session.execute(
            sa.select(sa.distinct(corpus.BlueprintNode.depth)).where(
                sa.or_(
                    corpus.BlueprintNode.weight_pct.isnot(None),
                    corpus.BlueprintNode.weight_min.isnot(None),
                )
            )
        ).scalars().all()

    assert weighted_depths == [1]


# ------------------------------------------------------------- byte-identical labels


TRICKY = "Governance, Risk’s — and  Responsible Use"


def test_a_label_is_stored_byte_for_byte(tmp_session: Session, tmp_kb: Path, tmp_path: Path) -> None:
    """The criterion. 018 joins on the vendor's punctuation with zero fuzzy matching.

    A smart quote turned straight here is a domain that silently stops joining
    there, so the torture case carries a right single quote, an em dash, a
    non-breaking space and a double space.
    """
    kb = tmp_kb
    text = artifact(f"- {TRICKY}\n", certification="mini-a", vendor="acme")
    target = tmp_path / "bp" / "acme" / "x.blueprint"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

    parsed = bp.parse_file(target)
    assert parsed.nodes[0].label == TRICKY

    out = bp.out_path(kb, parsed)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(bp.serialise(parsed), encoding="utf-8")

    ingest(tmp_session, kb)
    tmp_session.commit()

    stored = tmp_session.scalar(
        sa.select(corpus.BlueprintNode.label).where(corpus.BlueprintNode.depth == 1)
    )
    assert stored == TRICKY
    assert stored.encode("utf-8") == TRICKY.encode("utf-8")


@pytest.mark.parametrize(
    "label",
    [
        "Trailing spaces preserved   ",
        "  leading spaces after the marker",
        "Knowledge of:",
        "API creation - for example, Amazon API Gateway",
        "Claude’s context window",
        "Governance, Safety & Risk Management",
    ],
)
def test_nothing_normalises_a_label(label: str) -> None:
    parsed = bp.parse_text(artifact(f"- {label}\n"))

    assert parsed.nodes[0].label == label


def test_the_fixtures_keep_the_vendors_own_punctuation() -> None:
    aws = next(b for b in (bp.parse_file(p) for p in bp.discover(FIXTURES)) if b.certification == "saa-c03")
    labels = [node.label for _path, node in aws.every()]

    assert "AWS federated access and identity services" in labels
    assert any(" - for example, " in label for label in labels), "AWS's own dash convention"
    assert "Knowledge of:" in labels and "Skills in:" in labels


# --------------------------------------------------------------- errors with a line


def test_a_weight_below_the_top_level_is_rejected_with_its_line() -> None:
    """The criterion. The person who typed it has to be able to find it."""
    text = artifact(
        "- [50%] Top\n  - [25%] Not allowed here\n", regime="exact"
    )

    with pytest.raises(bp.BlueprintError) as raised:
        bp.parse_text(text, "az.blueprint")

    assert raised.value.line == 7
    assert "below the top level" in raised.value.message
    assert "az.blueprint:7:" in str(raised.value)


@pytest.mark.parametrize(
    ("body", "line", "fragment"),
    [
        ("- Top\n   - Three spaces\n", 7, "not a multiple of 2"),
        ("- Top\n      - Orphan\n", 7, "no parent at depth"),
        ("- Top\n  - A\n    - B\n      - C\n        - D\n", 10, "deeper than 4"),
        ("- \n", 6, "needs a label"),
        ("- Top\nnot a bullet\n", 7, "expected an outline bullet"),
    ],
)
def test_every_mistake_names_its_line(body: str, line: int, fragment: str) -> None:
    with pytest.raises(bp.BlueprintError) as raised:
        bp.parse_text(artifact(body), "x.blueprint")

    assert raised.value.line == line
    assert fragment in raised.value.message


@pytest.mark.parametrize(
    ("headers", "fragment"),
    [
        ({"regime": "made-up"}, "is not one of"),
        ({"regime": "exact"}, "no top-level node carries a weight"),
    ],
)
def test_a_bad_header_is_refused(headers: dict, fragment: str) -> None:
    with pytest.raises(bp.BlueprintError, match=fragment):
        bp.parse_text(artifact("- Top\n", **headers))


def test_a_missing_header_is_named() -> None:
    with pytest.raises(bp.BlueprintError, match="missing header"):
        bp.parse_text("vendor: acme\n\n- Top\n")


def test_a_source_without_a_hash_is_refused() -> None:
    """The sha256 is the whole drift mechanism; a source without one records nothing."""
    text = "certification: x\nvendor: acme\nregime: absent\nsource: guide.pdf\n\n- Top\n"

    with pytest.raises(bp.BlueprintError, match="sha256"):
        bp.parse_text(text)


def test_a_wrong_shaped_weight_is_refused() -> None:
    with pytest.raises(bp.BlueprintError, match="use `20-25%`"):
        bp.parse_text(artifact("- [21%] Top\n", regime="range"))
    with pytest.raises(bp.BlueprintError, match="use `21%`"):
        bp.parse_text(artifact("- [20-25%] Top\n", regime="exact"))


def test_a_weight_on_an_unweighted_regime_is_refused() -> None:
    with pytest.raises(bp.BlueprintError, match="but this node carries a weight"):
        bp.parse_text(artifact("- [21%] Top\n", regime="absent"))


def test_a_backwards_range_is_refused() -> None:
    with pytest.raises(bp.BlueprintError, match="runs backwards"):
        bp.parse_text(artifact("- [25-20%] Top\n", regime="range"))


# ----------------------------------------------------------------- the source hash


def test_a_present_source_document_is_re_hashed(tmp_path: Path) -> None:
    """Absence is fine -- a Marketo-gated guide may never be saved. Drift is not."""
    document = tmp_path / "guide.pdf"
    document.write_bytes(b"the guide as transcribed")
    digest = hashlib.sha256(document.read_bytes()).hexdigest()

    good = tmp_path / "good.blueprint"
    good.write_text(
        f"certification: x\nvendor: acme\nregime: absent\nsource: guide.pdf sha256={digest}\n\n- Top\n",
        encoding="utf-8",
    )
    assert bp.verify_sources(bp.parse_file(good), tmp_path) == []

    document.write_bytes(b"the guide, quietly revised")
    problems = bp.verify_sources(bp.parse_file(good), tmp_path)

    assert len(problems) == 1
    assert "has changed since it was transcribed" in problems[0]


def test_a_missing_source_document_is_not_an_error(tmp_path: Path) -> None:
    artifact_path = tmp_path / "x.blueprint"
    artifact_path.write_text(artifact("- Top\n"), encoding="utf-8")

    assert bp.verify_sources(bp.parse_file(artifact_path), tmp_path) == []


# ------------------------------------------------------------------- the projection


def test_rebuild_reproduces_every_blueprint_node_key(
    tmp_db: Engine, kb_with_blueprints: Path
) -> None:
    """The criterion, and the reason the keys are derived from the outline's path."""
    def keys(session):
        return sorted(session.scalars(sa.select(corpus.BlueprintNode.id)).all())

    with Session(tmp_db) as session:
        ingest(session, kb_with_blueprints)
        session.commit()
        before = keys(session)

    with Session(tmp_db) as session:
        ingest(session, kb_with_blueprints, rebuild=True)
        session.commit()
        after = keys(session)

    assert after == before
    assert len(before) == 73
    assert "aws/saa-c03/Version 1.1#1.1.1.1" in before


def test_a_blueprint_only_certification_gets_a_row(
    tmp_db: Engine, kb_with_blueprints: Path
) -> None:
    """Phase 8's whole premise: a certification that arrives as an outline alone."""
    with Session(tmp_db) as session:
        ingest(session, kb_with_blueprints)
        session.commit()

        az = session.get(corpus.Certification, "az-104")
        ccao = session.get(corpus.Certification, "ccao-f")

    assert az is not None
    assert az.name == "Microsoft Azure Administrator"
    assert az.question_count == 0
    assert ccao.question_count == 360, "the corpus still owns its own count"


def test_the_sources_are_projected_with_their_hashes(
    tmp_db: Engine, kb_with_blueprints: Path
) -> None:
    with Session(tmp_db) as session:
        ingest(session, kb_with_blueprints)
        session.commit()
        sources = session.scalars(sa.select(corpus.BlueprintSource)).all()

        assert len(sources) == 4
        for source in sources:
            assert len(source.sha256) == 64
        note = next(s.note for s in sources if s.blueprint_id.startswith("snowflake/"))
        assert "Marketo" in note


# --------------------------------------------------------------- the fingerprint


def test_a_blueprint_change_makes_the_projection_stale(kb_with_blueprints: Path) -> None:
    """The criterion 017 added. 006 hashed question shards only."""
    before = kb_fingerprint(kb_with_blueprints)

    target = blueprint_files(kb_with_blueprints)[0]
    data = json.loads(target.read_text(encoding="utf-8"))
    data["nodes"][0]["label"] = data["nodes"][0]["label"] + " (revised)"
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    assert kb_fingerprint(kb_with_blueprints) != before


def test_the_fingerprint_covers_blueprints_explicitly(kb_with_blueprints: Path) -> None:
    rows = ingest_module.corpus_rows(kb_with_blueprints)
    providers = {row["provider"] for row in rows}

    assert "blueprint" in providers
    assert len([row for row in rows if row["provider"] == "blueprint"]) == 4


def test_removing_a_blueprint_removes_its_nodes(
    tmp_db: Engine, kb_with_blueprints: Path
) -> None:
    with Session(tmp_db) as session:
        ingest(session, kb_with_blueprints)
        session.commit()
        assert session.scalar(sa.select(sa.func.count()).select_from(corpus.Blueprint)) == 4

    blueprint_files(kb_with_blueprints)[0].unlink()

    with Session(tmp_db) as session:
        ingest(session, kb_with_blueprints)
        session.commit()

        assert session.scalar(sa.select(sa.func.count()).select_from(corpus.Blueprint)) == 3
        assert session.scalar(
            sa.select(sa.func.count()).select_from(corpus.BlueprintNode).where(
                corpus.BlueprintNode.blueprint_id.like("aws/%")
            )
        ) == 0


# ------------------------------------------------------------------- the tool itself


def test_the_parser_is_stdlib_only() -> None:
    """Every `tools/` script is a standalone PEP 723 program with no dependencies."""
    source = (REPO_ROOT / "tools" / "parse_blueprint.py").read_text(encoding="utf-8")

    assert "# dependencies = []" in source
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    }
    assert imports <= {"__future__", "argparse", "hashlib", "json", "re", "sys", "dataclasses", "pathlib"}


def test_check_mode_writes_nothing(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    result = subprocess.run(
        [sys.executable, "tools/parse_blueprint.py", "--check", str(FIXTURES), "--kb", str(kb)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert not kb.exists()


def test_a_bad_artifact_fails_the_check(tmp_path: Path) -> None:
    bad = tmp_path / "bad.blueprint"
    bad.write_text(artifact("- Top\n  - [25%] Nested weight\n", regime="exact"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "tools/parse_blueprint.py", "--check", str(tmp_path)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert "bad.blueprint:7:" in result.stderr


def test_an_empty_source_directory_is_not_a_failure(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "tools/parse_blueprint.py", str(tmp_path)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert "no *.blueprint files" in result.stdout


def test_the_written_json_round_trips(tmp_path: Path) -> None:
    parsed = [bp.parse_file(path) for path in bp.discover(FIXTURES)]
    kb = tmp_path / "kb"
    for blueprint in parsed:
        target = bp.out_path(kb, blueprint)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bp.serialise(blueprint), encoding="utf-8")

    loaded = load_blueprints(kb)

    assert len(loaded) == 4
    assert {item["id"] for item in loaded} == {blueprint.id for blueprint in parsed}
    for item in loaded:
        original = next(b for b in parsed if b.id == item["id"])
        assert [node["label"] for node in item["nodes"]] == [
            node.label for _path, node in original.every()
        ]
