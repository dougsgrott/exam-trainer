"""PROJECTION tables: everything that is a pure function of `kb/` bytes.

Every table here can be dropped and rebuilt by `examkb ingest --rebuild` (006),
which is only true because **every primary key is derived from content** --
`ccao-f/exam-01/q001`, `ccao-f/exam-01/q001#A`, `docs/build-with-claude/prompt-caching`
-- and never from an autoincrementing counter. A rebuilt row with a new integer key
would break every journal row pointing at it and every diff comparing two rebuilds.
`tests/test_schema.py` asserts no table in this module uses one.

`ingest_run` is the single exception, and says so: `content_keyed = False`. It
records when a projection was built, which is not derivable from what was built.

Two constraint rules the plan is explicit about:

- **No CHECK rejects a blueprint whose weights do not sum to 100.** Microsoft's
  AZ-104 bounds to 80/105 and Snowflake's COF-C02 to 80/110 by design;
  `weights_sum_to_100` is a *recorded* boolean, and a corpus that drifts flags
  rather than fails.
- **Vendor strings are stored byte-identical.** `domain.label`, `blueprint_node.label`
  and `blueprint_node.kind` carry the vendor's own punctuation, including its
  inconsistencies across certifications, so the join to the corpus needs no fuzzy
  matching. The slug columns beside them are for URLs, never for matching.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from examkb.models.base import Projection, UTCDateTime

# ---------------------------------------------------------------------- vocabularies
#
# Only values this project owns are constrained. A vendor's own vocabulary --
# blueprint node kinds, taxonomy scheme names -- is never CHECKed, because a
# constraint on it is a migration the day a new vendor spells it differently.

ORIGINS = ("vendor_dump", "official_sample", "local_generation", "hand_written")
QUESTION_TYPES = ("single_select", "multi_select")
ANNOTATION_SOURCES = ("seed", "model", "human")
WEIGHT_REGIMES = ("exact", "range", "absent", "unpublished")
CARDINALITIES = ("one", "many")
INGEST_MODES = ("bootstrap", "incremental", "rebuild")
INGEST_STATUSES = ("bootstrap", "running", "ok", "failed")


def enum_check(column: str, values: tuple[str, ...], name: str, *, nullable: bool = False):
    """A CHECK pinning `column` to `values`, spelled once from the tuple above."""
    allowed = ", ".join(f"'{value}'" for value in values)
    clause = f"{column} IN ({allowed})"
    if nullable:
        clause = f"{column} IS NULL OR {clause}"
    return CheckConstraint(clause, name=name)


# ------------------------------------------------------------------------ provenance


class IngestRun(Projection):
    """A corpus state this projection has been built from.

    **The id is the `kb` fingerprint** -- sha256 over the shard hashes (006) --
    which makes this a record of corpus states rather than a log of every time
    someone typed `ingest`. Two consequences worth knowing:

    - "Is the projection stale?" is one comparison: every projection row carries
      this id, so 008's banner and 047's doctor ask whether it still matches the
      bytes on disk.
    - Re-projecting an unchanged corpus writes nothing at all, timestamps
      included, so `ingest --rebuild` twice leaves a byte-identical database.
      `started_at`, `mode` and `status` therefore describe the run that *first*
      projected this state.
    """

    __tablename__ = "ingest_run"
    __table_args__ = (
        enum_check("mode", INGEST_MODES, "mode_known"),
        enum_check("status", INGEST_STATUSES, "status_known"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    kb_fingerprint: Mapped[str | None] = mapped_column(Text)
    shard_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    examkb_version: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class Shard(Projection):
    """One `kb/` question shard, with the hash the fingerprint is computed from.

    Keyed on the path inside `kb/` because that is what `kb/shards.json` keys on,
    and the whole point of the shard layout (003) is that one producer's file is
    not another producer's to rewrite.
    """

    __tablename__ = "shard"
    __table_args__ = (Index("ix_shard_ingest_run_id", "ingest_run_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # e.g. "questions.jsonl"
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )


# ----------------------------------------------------------------------------- corpus


class Certification(Projection):
    __tablename__ = "certification"
    __table_args__ = (Index("ix_certification_vendor", "vendor"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "ccao-f"
    name: Mapped[str] = mapped_column(Text, nullable=False)
    vendor: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str | None] = mapped_column(Text)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )


class Exam(Projection):
    __tablename__ = "exam"
    __table_args__ = (
        UniqueConstraint("certification_id", "slug", name="uq_exam_certification_id_slug"),
        Index("ix_exam_certification_id", "certification_id"),
        Index("ix_exam_mode", "mode"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "ccao-f/exam-01"
    certification_id: Mapped[str] = mapped_column(
        Text, ForeignKey("certification.id"), nullable=False
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    number: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str | None] = mapped_column(Text)  # "realistic" / "hard": vendor's word
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )


class Domain(Projection):
    """A certification's own domain string, kept byte-identical.

    `label` is what the vendor wrote (`Governance, Risk, and Responsible Use` in
    one certification, `Governance, Safety & Risk Management` in the other). The
    blueprint join in 018 matches on this column exactly, with zero fuzzy matching;
    `slug` exists only so a domain can appear in a URL.
    """

    __tablename__ = "domain"
    __table_args__ = (
        UniqueConstraint("certification_id", "label", name="uq_domain_certification_id_label"),
        Index("ix_domain_certification_id", "certification_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "ccao-f/prompting-and-..."
    certification_id: Mapped[str] = mapped_column(
        Text, ForeignKey("certification.id"), nullable=False
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )


class Question(Projection):
    """One question, exactly as `kb/` holds it.

    `domain_label` is stored beside `domain_id` on purpose: a question whose domain
    string does not resolve to a `domain` row still keeps the vendor's text, so the
    failure is visible in the data rather than silently dropped at ingest.

    The three hash columns are the dedup gate's cheap layer (033): exact prompt,
    normalised prompt, and prompt-plus-options. The expensive layer -- FTS5 BM25
    top-20 then Jaccard -- only runs when these miss.
    """

    __tablename__ = "question"
    __table_args__ = (
        enum_check("type", QUESTION_TYPES, "type_known"),
        enum_check("origin", ORIGINS, "origin_known"),
        CheckConstraint("select_count >= 1", name="select_count_positive"),
        Index("ix_question_certification_id", "certification_id"),
        Index("ix_question_exam_id", "exam_id"),
        Index("ix_question_domain_id", "domain_id"),
        Index("ix_question_type", "type"),
        Index("ix_question_origin", "origin"),
        Index("ix_question_content_hash", "content_hash"),
        Index("ix_question_prompt_sha256", "prompt_sha256"),
        Index("ix_question_normalized_sha256", "normalized_sha256"),
        Index("ix_question_item_sha256", "item_sha256"),
        Index("ix_question_ingest_run_id", "ingest_run_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "ccao-f/exam-01/q001"
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    certification_id: Mapped[str] = mapped_column(
        Text, ForeignKey("certification.id"), nullable=False
    )
    exam_id: Mapped[str | None] = mapped_column(Text, ForeignKey("exam.id"))
    domain_id: Mapped[str | None] = mapped_column(Text, ForeignKey("domain.id"))
    domain_label: Mapped[str | None] = mapped_column(Text)
    question_number: Mapped[int | None] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    select_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prompt_md: Mapped[str] = mapped_column(Text, nullable=False)
    overall_explanation_md: Mapped[str | None] = mapped_column(Text)
    correct_labels: Mapped[list] = mapped_column(JSON, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="vendor_dump")
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_file: Mapped[str | None] = mapped_column(Text)
    source_course_url: Mapped[str | None] = mapped_column(Text)
    shard_id: Mapped[str | None] = mapped_column(Text, ForeignKey("shard.id"))
    prompt_sha256: Mapped[str | None] = mapped_column(Text)
    normalized_sha256: Mapped[str | None] = mapped_column(Text)
    item_sha256: Mapped[str | None] = mapped_column(Text)
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )


class QuestionOption(Projection):
    """`<question id>#<label>` -- the label is the vendor's, the position is ours.

    `position` is the order the option was authored in. It is never the order it is
    displayed in: the runner shuffles and records its own permutation on
    `attempt_item.option_order` (013), which is safe because no explanation in the
    corpus references an option letter (0 hits across all 5490 fields).
    """

    __tablename__ = "question_option"
    __table_args__ = (
        UniqueConstraint("question_id", "label", name="uq_question_option_question_id_label"),
        Index("ix_question_option_question_id", "question_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("question.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text_md: Mapped[str] = mapped_column(Text, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    explanation_md: Mapped[str | None] = mapped_column(Text)


class Reference(Projection):
    """A cited page, keyed on its *normalised* form.

    `normalize_reference` (002) collapses the nine mirror hosts, so the same page
    is one row and one reading-list entry (023) rather than two under two hostnames.
    """

    __tablename__ = "reference"
    __table_args__ = (Index("ix_reference_host", "host"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "docs/build-with-claude/..."
    display_url: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str | None] = mapped_column(Text)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )


class QuestionReference(Projection):
    __tablename__ = "question_reference"
    __table_args__ = (
        PrimaryKeyConstraint("question_id", "reference_id", name="pk_question_reference"),
        Index("ix_question_reference_reference_id", "reference_id"),
    )

    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("question.id", ondelete="CASCADE"), nullable=False
    )
    reference_id: Mapped[str] = mapped_column(Text, ForeignKey("reference.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_url: Mapped[str] = mapped_column(Text, nullable=False)


# --------------------------------------------------------------------------- blueprint


class Blueprint(Projection):
    """A vendor's published outline for one certification.

    `weights_sum_to_100` is recorded, never enforced. So is `weights_sum`: an
    AZ-104 blueprint whose ranges bound to 80/105 is a correct transcription of a
    correct document, and the schema that rejects it is the schema that is wrong.
    """

    __tablename__ = "blueprint"
    __table_args__ = (
        enum_check("weight_regime", WEIGHT_REGIMES, "weight_regime_known"),
        CheckConstraint("max_depth BETWEEN 1 AND 4", name="max_depth_in_range"),
        Index("ix_blueprint_certification_id", "certification_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "anthropic/ccao-f/1.0"
    certification_id: Mapped[str] = mapped_column(
        Text, ForeignKey("certification.id"), nullable=False
    )
    vendor: Mapped[str] = mapped_column(Text, nullable=False)
    version_label: Mapped[str | None] = mapped_column(Text)  # "Version 1.0"
    effective_from: Mapped[str | None] = mapped_column(Text)  # vendor's own wording
    weight_regime: Mapped[str] = mapped_column(Text, nullable=False)
    weights_sum: Mapped[float | None] = mapped_column(Float)
    weights_sum_to_100: Mapped[bool | None] = mapped_column(Boolean)
    max_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )


class BlueprintSource(Projection):
    """One artifact a blueprint was transcribed from, with its sha256.

    Plural because Databricks needs two: weights from the guide's HTML, objectives
    from a PDF in a subset CID font. Lineage and drift machinery was cut; a hash
    plus a manual re-fetch is the whole mechanism.
    """

    __tablename__ = "blueprint_source"
    __table_args__ = (Index("ix_blueprint_source_blueprint_id", "blueprint_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    blueprint_id: Mapped[str] = mapped_column(
        Text, ForeignKey("blueprint.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_on: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class BlueprintNode(Projection):
    """One line of the outline, up to depth 4 (AWS: domain → task → bucket → bullet).

    `kind` holds the vendor's name for the level and is deliberately unconstrained.
    Weights appear only at the top level in all six vendors surveyed, so 017 rejects
    a weight below depth 1 as a transcription error -- in the parser, with a line
    number, where the person who typed it can fix it. Not here: a CHECK would turn
    a future vendor's nested weights into a migration.
    """

    __tablename__ = "blueprint_node"
    __table_args__ = (
        CheckConstraint("depth BETWEEN 1 AND 4", name="depth_in_range"),
        UniqueConstraint("blueprint_id", "path", name="uq_blueprint_node_blueprint_id_path"),
        Index("ix_blueprint_node_blueprint_id", "blueprint_id"),
        Index("ix_blueprint_node_parent_id", "parent_id"),
        Index("ix_blueprint_node_blueprint_id_depth", "blueprint_id", "depth"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "anthropic/ccao-f/1.0#1.2"
    blueprint_id: Mapped[str] = mapped_column(
        Text, ForeignKey("blueprint.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(Text, ForeignKey("blueprint_node.id"))
    path: Mapped[str] = mapped_column(Text, nullable=False)  # "1.2.3"
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text, nullable=False)  # byte-identical
    weight_pct: Mapped[float | None] = mapped_column(Float)
    weight_min: Mapped[float | None] = mapped_column(Float)
    weight_max: Mapped[float | None] = mapped_column(Float)


class BlueprintDomainMap(Projection):
    """The corpus domain ↔ blueprint node join, recorded rather than recomputed.

    `join_method` exists so the zero-fuzzy-matching rule is inspectable in the data.
    018 fails its test if a domain string does not join exactly; nothing here may
    fall back to a nearest match.
    """

    __tablename__ = "blueprint_domain_map"
    __table_args__ = (
        PrimaryKeyConstraint("domain_id", "node_id", name="pk_blueprint_domain_map"),
        enum_check("join_method", ("exact",), "join_method_known"),
        Index("ix_blueprint_domain_map_node_id", "node_id"),
    )

    domain_id: Mapped[str] = mapped_column(Text, ForeignKey("domain.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(
        Text, ForeignKey("blueprint_node.id", ondelete="CASCADE"), nullable=False
    )
    join_method: Mapped[str] = mapped_column(Text, nullable=False, default="exact")


class FormatProfile(Projection):
    """What one certification's exam actually looks like, as data.

    The 720 cut and the 100–1000 scale live here rather than in the grader (014),
    and `allowed_formats` is what stops 2-of-4 being hard-coded (044): a list of
    `{"options": n, "select": k}` shapes the sampler, runner and generator read.
    """

    __tablename__ = "format_profile"
    __table_args__ = (
        UniqueConstraint("certification_id", name="uq_format_profile_certification_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # the certification slug
    certification_id: Mapped[str] = mapped_column(
        Text, ForeignKey("certification.id"), nullable=False
    )
    item_count: Mapped[int | None] = mapped_column(Integer)
    time_limit_minutes: Mapped[int | None] = mapped_column(Integer)
    scale_min: Mapped[int | None] = mapped_column(Integer)
    scale_max: Mapped[int | None] = mapped_column(Integer)
    cut_score: Mapped[int | None] = mapped_column(Integer)
    validity_months: Mapped[int | None] = mapped_column(Integer)
    allowed_formats: Mapped[list | None] = mapped_column(JSON)
    source: Mapped[str | None] = mapped_column(Text)
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )


# ---------------------------------------------------------------------------- taxonomy


class TaxonScheme(Projection):
    """One classification axis. Schemes are data, not an enum in code.

    Tags (026) are one scheme with `cardinality = 'many'` and no certification.
    Category (027) is a scheme per certification with `cardinality = 'one'` --
    which is the whole reason it is a table: "code" means prompt text for Anthropic
    and Spark SQL for Databricks, and a hard-coded enum would be wrong on day one.
    """

    __tablename__ = "taxon_scheme"
    __table_args__ = (
        enum_check("cardinality", CARDINALITIES, "cardinality_known"),
        Index("ix_taxon_scheme_certification_id", "certification_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "tag", "ccao-f/category"
    certification_id: Mapped[str | None] = mapped_column(Text, ForeignKey("certification.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    cardinality: Mapped[str] = mapped_column(Text, nullable=False, default="many")
    source: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )


class Taxon(Projection):
    __tablename__ = "taxon"
    __table_args__ = (
        UniqueConstraint("scheme_id", "slug", name="uq_taxon_scheme_id_slug"),
        Index("ix_taxon_scheme_id", "scheme_id"),
        Index("ix_taxon_parent_id", "parent_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "tag/prompt-caching"
    scheme_id: Mapped[str] = mapped_column(
        Text, ForeignKey("taxon_scheme.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(Text, ForeignKey("taxon.id"))
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text, ForeignKey("blueprint_node.id"))
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )


class QuestionTaxon(Projection):
    """A question's classification on one axis, with where it came from.

    `source` is what 028 renders as provenance: a tag from the deterministic
    reference seed (026) and one a model proposed (031) are not the same claim, and
    the UI never presents them as if they were. `annotation_run_id` points at the
    `data/annotations/<run-id>/` directory the row was projected from -- the model
    call itself is journal, recorded on `llm_call`, because a projection row may
    never depend on one.
    """

    __tablename__ = "question_taxon"
    __table_args__ = (
        PrimaryKeyConstraint("question_id", "taxon_id", name="pk_question_taxon"),
        enum_check("source", ANNOTATION_SOURCES, "source_known"),
        Index("ix_question_taxon_taxon_id", "taxon_id"),
    )

    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("question.id", ondelete="CASCADE"), nullable=False
    )
    taxon_id: Mapped[str] = mapped_column(
        Text, ForeignKey("taxon.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    annotation_run_id: Mapped[str | None] = mapped_column(Text)


class QuestionAnnotation(Projection):
    """The per-question annotation fields that are not a taxonomy membership.

    Separate from `question` because it is projected from a different file:
    `kb/annotations.jsonl`, written by `parse_annotations.py` (027), not by the
    question parser. `difficulty_band` is one nullable 1–5 and stays NULL until
    something sets it -- the four estimates and the Elo were cut, and observed
    accuracy is computed on read beside the band rather than stored next to it.
    """

    __tablename__ = "question_annotation"
    __table_args__ = (
        CheckConstraint(
            "difficulty_band IS NULL OR difficulty_band BETWEEN 1 AND 5",
            name="difficulty_band_in_range",
        ),
        enum_check("source", ANNOTATION_SOURCES, "source_known", nullable=True),
    )

    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("question.id", ondelete="CASCADE"), primary_key=True
    )
    difficulty_band: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str | None] = mapped_column(Text)
    annotation_run_id: Mapped[str | None] = mapped_column(Text)
    notes_md: Mapped[str | None] = mapped_column(Text)
    ingest_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingest_run.id"), nullable=False
    )
