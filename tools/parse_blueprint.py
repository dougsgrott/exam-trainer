#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Transcribed blueprint outlines -> `kb/blueprints/**.json`.

Blueprints are not machine-readable. Zero of seven vendors surveyed publishes
JSON, an API or a feed; the Anthropic and Databricks PDFs use subset CID fonts
that need `/ToUnicode` CMap parsing, and there is no poppler or pypdf on this box.
So there is no scraper here and there never will be: a person reads the guide and
types the outline out, and this tool's whole job is to catch their typos and turn
the result into something the projection can load.

That shapes the format. It is an indented text outline because a person writes it
and reviews it in a diff -- not JSON, which is miserable to type and worse to
review, and not YAML or TOML, which would either need a dependency or turn a tree
into brackets. A label is the rest of the line, byte for byte: whatever punctuation
the vendor used survives, including the smart quotes and the inconsistent commas
that 018's join depends on matching exactly.

    # a comment
    certification: az-104
    name: Microsoft Azure Administrator
    vendor: microsoft
    version: Version 1.0
    effective: Effective 2026-07-01
    regime: range
    levels: Domain, Task
    source: az-104.html sha256=<64 hex> kind=html retrieved=2026-09-20

    - [20-25%] Manage Azure identities and governance
      - Manage Microsoft Entra users and groups

Four weight regimes, because vendors disagree about arithmetic:

    exact        `[21%]`      AWS, Anthropic, Databricks -- sums to 100
    range        `[20-25%]`   AZ-104 bounds to 80/105; SnowPro COF-C02 to 80/110
    absent       no weights   dbt publishes an outline and no percentages
    unpublished  no weights   COF-C03's guide is behind a Marketo form

Nothing here enforces that weights sum to 100, and nothing downstream does either:
a blueprint that bounds to 80/105 is a correct transcription of a correct document,
and the schema that rejects it is the schema that is wrong. `weights_sum` and
`weights_sum_to_100` are recorded so a person can see the arithmetic and decide.

Weights appear only at the top level in all six vendors surveyed, so one below the
top level is rejected as a transcription error -- here, with a line number, where
the person who typed it can fix it. The database has no such CHECK, on purpose: a
future vendor with nested weights should cost an edit to this file, not a migration.

Usage:
    uv run tools/parse_blueprint.py [--src DIR] [--kb DIR] [--check] [--quiet]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

BLUEPRINT_SUFFIX = ".blueprint"
BLUEPRINTS_DIR = "blueprints"
MAX_DEPTH = 4
INDENT = 2

REGIMES = ("exact", "range", "absent", "unpublished")
WEIGHTED_REGIMES = ("exact", "range")

# `- [21%] Label`, `- [20-25%] Label`, `- Label`. The label is group 3 and is taken
# verbatim -- no strip beyond the single space after the marker, no unicode fixes.
_BULLET = re.compile(r"^(?P<indent>[ ]*)- (?:\[(?P<weight>[^\]]+)\]\s*)?(?P<label>.*)$")
_HEADER = re.compile(r"^(?P<key>[a-z_]+):[ ]?(?P<value>.*)$")
_EXACT = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*%?\s*$")
_RANGE = re.compile(r"^\s*(?P<low>\d+(?:\.\d+)?)\s*[-–]\s*(?P<high>\d+(?:\.\d+)?)\s*%?\s*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_HEADERS = ("certification", "vendor", "regime")


class BlueprintError(ValueError):
    """A transcription mistake, with the line the person should look at."""

    def __init__(self, path: Path | str, line: int, message: str) -> None:
        self.path = str(path)
        self.line = line
        self.message = message
        super().__init__(f"{self.path}:{line}: {message}")


@dataclass
class Node:
    label: str
    depth: int
    ordinal: int
    line: int
    kind: str | None = None
    weight_pct: float | None = None
    weight_min: float | None = None
    weight_max: float | None = None
    children: list["Node"] = field(default_factory=list)

    def walk(self, prefix: tuple[int, ...] = ()):
        """Every node with its 1-based path, depth first, in document order."""
        path = (*prefix, self.ordinal)
        yield path, self
        for child in self.children:
            yield from child.walk(path)


@dataclass
class Source:
    path: str
    sha256: str
    kind: str | None = None
    retrieved_on: str | None = None
    note: str | None = None


@dataclass
class Blueprint:
    certification: str
    vendor: str
    regime: str
    nodes: list[Node]
    name: str | None = None
    """The vendor's own name for the certification.

    Only used when the corpus has no questions for it -- phase 8's whole point is a
    certification that arrives as a blueprint and nothing else, and it needs a name
    to show on a page. When questions exist, theirs wins.
    """

    version_label: str | None = None
    effective_from: str | None = None
    levels: tuple[str, ...] = ()
    sources: list[Source] = field(default_factory=list)
    path: str = ""

    @property
    def id(self) -> str:
        """`microsoft/az-104/Version 1.0` -- vendor, certification, the version it is."""
        return f"{self.vendor}/{self.certification}/{self.version_label or 'unversioned'}"

    @property
    def max_depth(self) -> int:
        return max((node.depth for _path, node in self.every()), default=1)

    def every(self):
        for node in self.nodes:
            yield from node.walk()

    @property
    def top(self) -> list[Node]:
        return self.nodes

    @property
    def weights_sum(self) -> float | None:
        """Exact weights sum themselves; ranges sum their *lower* bounds.

        Recorded, never enforced. AZ-104 bounds to 80/105 and SnowPro to 80/110,
        and both are correct transcriptions of correct documents.
        """
        if self.regime not in WEIGHTED_REGIMES:
            return None
        if self.regime == "exact":
            return round(sum(node.weight_pct or 0.0 for node in self.top), 4)
        return round(sum(node.weight_min or 0.0 for node in self.top), 4)

    @property
    def weights_sum_max(self) -> float | None:
        if self.regime != "range":
            return None
        return round(sum(node.weight_max or 0.0 for node in self.top), 4)

    @property
    def weights_sum_to_100(self) -> bool | None:
        """True, False, or **None for "the question does not apply"**.

        Only an `exact` blueprint has a single sum to compare against 100, so only
        an exact one gets a True or a False -- and False is recorded rather than
        rejected, because a vendor is allowed to publish 99. For a range regime the
        honest answer is None: AZ-104's lows sum to 80 and its highs to 105, and
        saying `True` because that brackets 100 would read as "the weights sum to
        100", which they do not. `weights_sum` and `weights_sum_max` carry the
        arithmetic instead.
        """
        if self.regime != "exact":
            return None
        return abs((self.weights_sum or 0.0) - 100.0) < 1e-6

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "certification": self.certification,
            "name": self.name,
            "vendor": self.vendor,
            "version_label": self.version_label,
            "effective_from": self.effective_from,
            "weight_regime": self.regime,
            "weights_sum": self.weights_sum,
            "weights_sum_max": self.weights_sum_max,
            "weights_sum_to_100": self.weights_sum_to_100,
            "max_depth": self.max_depth,
            "levels": list(self.levels),
            "sources": [
                {
                    "path": source.path,
                    "sha256": source.sha256,
                    "kind": source.kind,
                    "retrieved_on": source.retrieved_on,
                    "note": source.note,
                }
                for source in self.sources
            ],
            "nodes": [
                {
                    "path": ".".join(str(part) for part in path),
                    "depth": node.depth,
                    "ordinal": node.ordinal,
                    "kind": node.kind,
                    "label": node.label,
                    "weight_pct": node.weight_pct,
                    "weight_min": node.weight_min,
                    "weight_max": node.weight_max,
                }
                for path, node in self.every()
            ],
        }


# ------------------------------------------------------------------------- parsing


def _weight(raw: str, line: int, path: Path | str) -> tuple[float | None, float | None, float | None]:
    exact = _EXACT.match(raw)
    if exact:
        return float(exact.group("value")), None, None
    spread = _RANGE.match(raw)
    if spread:
        low, high = float(spread.group("low")), float(spread.group("high"))
        if low > high:
            raise BlueprintError(path, line, f"weight range {raw!r} runs backwards")
        return None, low, high
    raise BlueprintError(
        path, line, f"{raw!r} is not a weight; expected `21%` or `20-25%`"
    )


def _source(value: str, line: int, path: Path | str) -> Source:
    # `note=` swallows the rest of the line, because the useful notes are sentences
    # -- "behind a Marketo form; transcribed from the public exam page" is exactly
    # the kind of thing worth recording and it has spaces in it. Every other field
    # is a single token.
    note = None
    head = value
    marker = value.find("note=")
    if marker != -1:
        head, note = value[:marker].rstrip(), value[marker + len("note=") :].strip()

    parts = head.split()
    if not parts:
        raise BlueprintError(path, line, "source needs a path")
    fields = {}
    for part in parts[1:]:
        if "=" not in part:
            raise BlueprintError(
                path, line,
                f"{part!r} is not key=value (only `note=` may contain spaces, and it must be last)",
            )
        key, _, val = part.partition("=")
        fields[key] = val
    if note is not None:
        fields["note"] = note
    sha = fields.get("sha256", "")
    if not _SHA256.match(sha):
        raise BlueprintError(
            path, line,
            "source needs sha256=<64 hex chars>; it is how a manual re-fetch detects drift",
        )
    return Source(
        path=parts[0],
        sha256=sha,
        kind=fields.get("kind"),
        retrieved_on=fields.get("retrieved"),
        note=fields.get("note"),
    )


def parse_text(text: str, path: Path | str = "<string>") -> Blueprint:
    """One artifact -> a `Blueprint`, or `BlueprintError` naming the line."""
    headers: dict[str, str] = {}
    sources: list[Source] = []
    roots: list[Node] = []
    stack: list[Node] = []
    seen_bullet = False

    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        bullet = _BULLET.match(raw)
        if bullet is None:
            if seen_bullet:
                raise BlueprintError(
                    path, number, f"expected an outline bullet (`- `), got {raw.strip()[:40]!r}"
                )
            header = _HEADER.match(raw)
            if header is None:
                raise BlueprintError(
                    path, number, f"expected `key: value` or `- item`, got {raw.strip()[:40]!r}"
                )
            key, value = header.group("key"), header.group("value").rstrip()
            if key == "source":
                sources.append(_source(value, number, path))
            elif key in headers:
                raise BlueprintError(path, number, f"{key!r} is set twice")
            else:
                headers[key] = value
            continue

        seen_bullet = True
        spaces = len(bullet.group("indent"))
        if spaces % INDENT:
            raise BlueprintError(
                path, number, f"indent of {spaces} is not a multiple of {INDENT}"
            )
        depth = spaces // INDENT + 1
        if depth > MAX_DEPTH:
            raise BlueprintError(
                path, number, f"depth {depth} is deeper than {MAX_DEPTH}; AWS is the deepest at 4"
            )
        if depth > len(stack) + 1:
            raise BlueprintError(
                path, number, f"depth {depth} has no parent at depth {depth - 1}"
            )

        label = bullet.group("label")
        if not label.strip():
            raise BlueprintError(path, number, "a node needs a label")

        weight_pct = weight_min = weight_max = None
        if bullet.group("weight") is not None:
            if depth > 1:
                raise BlueprintError(
                    path, number,
                    "a weight below the top level is a transcription error -- every vendor "
                    "surveyed puts weights only on the top level",
                )
            weight_pct, weight_min, weight_max = _weight(bullet.group("weight"), number, path)

        del stack[depth - 1 :]
        siblings = stack[-1].children if stack else roots
        node = Node(
            label=label,
            depth=depth,
            ordinal=len(siblings) + 1,
            line=number,
            weight_pct=weight_pct,
            weight_min=weight_min,
            weight_max=weight_max,
        )
        siblings.append(node)
        stack.append(node)

    missing = [key for key in REQUIRED_HEADERS if key not in headers]
    if missing:
        raise BlueprintError(path, 1, f"missing header(s): {', '.join(missing)}")
    regime = headers["regime"]
    if regime not in REGIMES:
        raise BlueprintError(path, 1, f"regime {regime!r} is not one of {', '.join(REGIMES)}")
    if not roots:
        raise BlueprintError(path, 1, "a blueprint with no nodes is not a blueprint")

    levels = tuple(part.strip() for part in headers.get("levels", "").split(",") if part.strip())
    blueprint = Blueprint(
        certification=headers["certification"],
        name=headers.get("name") or None,
        vendor=headers["vendor"],
        regime=regime,
        nodes=roots,
        version_label=headers.get("version") or None,
        effective_from=headers.get("effective") or None,
        levels=levels,
        sources=sources,
        path=str(path),
    )
    _check_weights(blueprint, path)
    _apply_levels(blueprint, path)
    return blueprint


def _check_weights(blueprint: Blueprint, path: Path | str) -> None:
    weighted = [node for node in blueprint.top if node.weight_pct is not None or node.weight_min is not None]
    if blueprint.regime in WEIGHTED_REGIMES:
        if not weighted:
            raise BlueprintError(
                path, blueprint.top[0].line,
                f"regime is {blueprint.regime!r} but no top-level node carries a weight",
            )
        if len(weighted) != len(blueprint.top):
            bare = next(node for node in blueprint.top if node not in weighted)
            raise BlueprintError(
                path, bare.line,
                f"regime is {blueprint.regime!r} so every top-level node needs a weight",
            )
        wrong_shape = [
            node
            for node in blueprint.top
            if (blueprint.regime == "exact") != (node.weight_pct is not None)
        ]
        if wrong_shape:
            raise BlueprintError(
                path, wrong_shape[0].line,
                f"regime is {blueprint.regime!r}; use "
                + ("`21%`" if blueprint.regime == "exact" else "`20-25%`"),
            )
    elif weighted:
        raise BlueprintError(
            path, weighted[0].line,
            f"regime is {blueprint.regime!r} but this node carries a weight",
        )


def _apply_levels(blueprint: Blueprint, path: Path | str) -> None:
    """`levels:` names each depth the way the vendor does. That is `node.kind`.

    AWS's typed `Knowledge of:` bucket is not a special case in the parser; it is
    the vendor's name for depth 3, declared once in the header.
    """
    if not blueprint.levels:
        return
    if len(blueprint.levels) < blueprint.max_depth:
        raise BlueprintError(
            path, 1,
            f"levels names {len(blueprint.levels)} level(s) but the tree is "
            f"{blueprint.max_depth} deep",
        )
    for _path, node in blueprint.every():
        node.kind = blueprint.levels[node.depth - 1]


def parse_file(path: Path) -> Blueprint:
    return parse_text(path.read_text(encoding="utf-8"), path)


def discover(root: Path) -> list[Path]:
    return sorted(root.rglob(f"*{BLUEPRINT_SUFFIX}"))


def verify_sources(blueprint: Blueprint, root: Path) -> list[str]:
    """Re-hash any source document that is actually present. Absence is not an error.

    The hash is a record of what was transcribed, not a dependency: fixtures have
    no documents beside them, and a guide behind a Marketo form may never be saved
    at all. When the file *is* there, drift is worth shouting about.
    """
    problems = []
    for source in blueprint.sources:
        candidate = (Path(blueprint.path).parent / source.path).resolve()
        if not candidate.is_file():
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != source.sha256:
            problems.append(
                f"{blueprint.path}: {source.path} has changed since it was transcribed "
                f"(recorded {source.sha256[:12]}, found {digest[:12]})"
            )
    return problems


def serialise(blueprint: Blueprint) -> str:
    return json.dumps(blueprint.to_json(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def out_path(kb: Path, blueprint: Blueprint) -> Path:
    return kb / BLUEPRINTS_DIR / blueprint.vendor / f"{blueprint.certification}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribed blueprints -> kb/blueprints/.")
    parser.add_argument("src", nargs="?", default="data/blueprints", help="artifact directory")
    parser.add_argument("--kb", default="kb", help="output knowledge base directory")
    parser.add_argument("--check", action="store_true", help="validate and write nothing")
    parser.add_argument("--quiet", action="store_true", help="print nothing unless it fails")
    args = parser.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        if args.check:
            print(f"parse_blueprint: no such directory: {src}", file=sys.stderr)
            return 1
        if not args.quiet:
            print(f"parse_blueprint: nothing to do; {src} does not exist")
        return 0

    artifacts = discover(src)
    if not artifacts:
        if not args.quiet:
            print(f"parse_blueprint: no *{BLUEPRINT_SUFFIX} files under {src}")
        return 0

    failures: list[str] = []
    parsed: list[Blueprint] = []
    for artifact in artifacts:
        try:
            blueprint = parse_file(artifact)
        except BlueprintError as error:
            failures.append(str(error))
            continue
        failures.extend(verify_sources(blueprint, src))
        parsed.append(blueprint)

    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        return 1

    if not args.check:
        kb = Path(args.kb)
        for blueprint in parsed:
            target = out_path(kb, blueprint)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(serialise(blueprint), encoding="utf-8")

    if not args.quiet:
        verb = "checked" if args.check else "wrote"
        print(f"parse_blueprint: {verb} {len(parsed)} blueprint(s) from {src}")
        for blueprint in parsed:
            low, high = blueprint.weights_sum, blueprint.weights_sum_max
            if low is None:
                sums = ""
            elif high is None:
                sums = f", weights sum to {low:g}"
            else:
                sums = f", weights bound to {low:g}/{high:g}"
            flag = blueprint.weights_sum_to_100
            note = "" if flag is not False else " (not 100 -- recorded, not an error)"
            print(
                f"  {blueprint.id:<44} {len(list(blueprint.every())):>3} nodes, "
                f"depth {blueprint.max_depth}, {blueprint.regime}{sums}{note}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
