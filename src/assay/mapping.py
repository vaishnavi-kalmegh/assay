"""Offline requirement-to-test mapping, traceability matrix and gap report.

For each acceptance criterion this module decides which generated tests (if any)
plausibly verify it. The default mapper is deliberately simple and inspectable:

1. Eligibility. A test can only verify a criterion if it exercises the same
   operation (method and path), expects the same status, checks what the
   criterion talks about (a criterion about response-body content needs a test
   that checks the body; the baseline tests only check the status code), and
   targets the same field (a test about one field cannot verify a criterion about
   another; whole-request tests such as "valid input" are eligible for any field).
2. Ranking. Among eligible tests, rank by TF-IDF cosine similarity between a text
   built from the criterion and a text built from the test.
3. Threshold. Eligible tests scoring at or above the threshold are mapped, at most
   ``top_k`` per criterion. The default threshold is provisional: it is not
   validated until the labelled evaluation (milestone 5).

Criteria that end up with no mapped test are reported as gaps, with the reason.
A mapping means "textually and structurally plausible", not "proven to verify the
requirement". Everything here is deterministic and runs offline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sklearn.feature_extraction.text import TfidfVectorizer

from assay.baseline import generate_cases
from assay.runner import expected_status, load_spec

# Provisional, and chosen after looking at the demo score table (see
# docs/MAPPING.md). Milestone 5 re-chooses it on a labelled development subset and
# freezes it before the final evaluation.
DEFAULT_THRESHOLD = 0.4
DEFAULT_TOP_K = 3

GapReason = Literal[
    "no_test_for_operation",
    "no_test_with_expected_status",
    "needs_response_body_check",
    "no_test_for_field",
    "no_test_for_constraint",
    "below_threshold",
]


class MappingError(ValueError):
    """Raised for invalid criteria, tests, or mapper settings."""


class LLMDisabledError(RuntimeError):
    """Raised when the optional LLM mapper is used. It is disabled in the MVP."""


class Criterion(BaseModel):
    """One structured acceptance criterion from criteria.yaml."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    method: str
    path: str
    field: str
    constraint: str
    value: Any
    expected_status: int
    description: str


class MappableTest(BaseModel):
    """What the mapper needs to know about one generated test."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    name: str
    method: str
    path: str
    field: str
    kind: str
    expected_valid: bool
    expected_status: int
    # What the generated test asserts. Baseline tests assert only the status code.
    checks: tuple[str, ...] = ("status",)


class TraceMatch(BaseModel):
    """One test mapped to a criterion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    name: str
    score: float
    rank: int


class TraceRow(BaseModel):
    """Everything known about one criterion: its matches or why it has none."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    description: str
    method: str
    path: str
    expected_status: int
    eligible_tests: int
    matches: list[TraceMatch] = Field(default_factory=list)
    gap_reason: GapReason | None = None
    gap_detail: str | None = None
    # Every eligible test, best first. `matches` is the part at or above threshold.
    candidates: list[TraceMatch] = Field(default_factory=list)


class MappingResult(BaseModel):
    """The outcome of mapping every criterion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mapper: str
    threshold: float
    top_k: int
    tests: int
    rows: list[TraceRow]

    @property
    def mapped(self) -> list[TraceRow]:
        return [row for row in self.rows if row.matches]

    @property
    def gaps(self) -> list[TraceRow]:
        return [row for row in self.rows if not row.matches]

    def case_mapping(self) -> dict[str, list[str]]:
        """Case ID -> requirement IDs, the format `assay run --mapping` reads."""
        by_case: dict[str, set[str]] = {}
        for row in self.rows:
            for match in row.matches:
                by_case.setdefault(match.case_id, set()).add(row.requirement_id)
        return {case: sorted(by_case[case]) for case in sorted(by_case)}


class RequirementMapper(Protocol):
    """Anything that can map criteria to tests."""

    name: str

    def map(
        self, criteria: Sequence[Criterion], tests: Sequence[MappableTest]
    ) -> MappingResult: ...


# --- text preparation --------------------------------------------------------

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "twenty": "20", "forty": "40", "hundred": "100", "thousand": "1000",
}
_STOP_WORDS = frozenset(
    {"a", "an", "the", "is", "are", "be", "of", "than", "its", "by", "in", "for", "and"}
)
# Words that only say where a field lives; they carry no meaning for matching.
_LOCATION_WORDS = frozenset({"request", "response", "body", "path"})
_TOKEN = re.compile(r"[a-z]+|\d+(?:\.\d+)?")

# How each criterion constraint is worded in the generated test names.
_CONSTRAINT_PHRASES = {
    "min_length": "minimum length",
    "max_length": "maximum length",
    "greater_than": "exclusive minimum",
    "greater_than_or_equal": "minimum",
    "less_than_or_equal": "maximum",
    "enum": "enum",
    "required": "missing required",
    "equals_path_parameter": "path parameter",
    "not_found": "unknown not found",
}


def tokenize(text: str) -> list[str]:
    """Lowercase words and numbers; number words become digits; 10.0 becomes 10."""
    tokens: list[str] = []
    for raw in _TOKEN.findall(text.lower()):
        if raw[0].isdigit():
            number = float(raw)
            tokens.append(str(int(number)) if number.is_integer() else raw)
            continue
        word = _NUMBER_WORDS.get(raw, raw)
        if word not in _STOP_WORDS:
            tokens.append(word)
    return tokens


def _field_words(field: str) -> list[str]:
    return [word for word in tokenize(field) if word not in _LOCATION_WORDS]


def _flatten(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(item) for item in value)
    return str(value)


def _outcome_word(success: bool) -> str:
    return "valid" if success else "invalid"


def criterion_text(criterion: Criterion) -> str:
    """The words that describe a criterion. Method, path and status are not
    included: eligibility already requires them to match."""
    phrase = _CONSTRAINT_PHRASES.get(
        criterion.constraint, criterion.constraint.replace("_", " ")
    )
    success = 200 <= criterion.expected_status < 300
    parts = [
        " ".join(_field_words(criterion.field)),
        phrase,
        _flatten(criterion.value),
        _outcome_word(success),
        criterion.description,
    ]
    return " ".join(" ".join(tokenize(part)) for part in parts if part)


def case_text(test: MappableTest) -> str:
    """The words that describe a test: its field and what the case does."""
    pieces = test.name.split(" — ")
    tail = pieces[-1] if len(pieces) >= 3 else test.name
    parts = [
        " ".join(_field_words(test.field)),
        tail,
        _outcome_word(test.expected_valid),
    ]
    return " ".join(" ".join(tokenize(part)) for part in parts if part)


# --- inputs ------------------------------------------------------------------


def load_criteria(path: Path) -> list[Criterion]:
    """Read and validate criteria.yaml."""
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise MappingError(f"{path} is not valid YAML: {error}") from error
    if not isinstance(document, dict) or not isinstance(
        document.get("criteria"), list
    ):
        raise MappingError(f"{path} must contain a top-level 'criteria' list")
    criteria: list[Criterion] = []
    for index, item in enumerate(document["criteria"], start=1):
        try:
            criteria.append(Criterion.model_validate(item))
        except ValueError as error:
            raise MappingError(
                f"criterion #{index} in {path} is invalid: {error}"
            ) from error
    return criteria


def baseline_tests_from_spec(spec: dict[str, Any]) -> list[MappableTest]:
    """The baseline tests for a spec, described for the mapper."""
    return [
        MappableTest(
            case_id=case.case_id,
            name=case.name,
            method=case.method,
            path=case.path,
            field=case.field,
            kind=case.kind,
            expected_valid=case.expected_valid,
            expected_status=expected_status(spec, case),
            checks=("status",),
        )
        for case in generate_cases(spec)
    ]


# --- mapping -----------------------------------------------------------------


def _constraint_compatible(criterion: Criterion, test: MappableTest) -> bool:
    """Reject tests whose boundary direction does not exercise the criterion.

    Operation, status, and field are checked separately. For recognized schema
    constraints, the generated case kind/name must also match the relevant
    boundary and outcome. Unknown constraints retain the previous text-ranking
    behavior rather than being guessed.
    """
    constraint = criterion.constraint
    success = 200 <= criterion.expected_status < 300
    value_tokens = tokenize(_flatten(criterion.value))
    value = value_tokens[0] if value_tokens else ""

    if constraint == "min_length":
        phrase = (\n            f"minimum length {value}"\n            if success\n            else f"just below minimum length {value}"\n        )
        return phrase in test.name.lower()
    if constraint == "max_length":
        phrase = (\n            f"maximum length {value}"\n            if success\n            else f"just above maximum length {value}"\n        )
        return phrase in test.name.lower()
    if constraint == "greater_than":
        phrase = (
            f"just inside exclusive minimum {value}"
            if success else f"just outside exclusive minimum {value}"
        )
        return phrase in test.name.lower()
    if constraint == "greater_than_or_equal":
        phrase = (\n            f"minimum boundary {value}"\n            if success\n            else f"just outside minimum {value}"\n        )
        return phrase in test.name.lower()
    if constraint == "less_than_or_equal":
        phrase = (\n            f"maximum boundary {value}"\n            if success\n            else f"just outside maximum {value}"\n        )
        return phrase in test.name.lower()
    if constraint == "enum":
        if success:
            return test.kind == "valid" and test.expected_valid
        return test.kind == "invalid_enum" and "invalid enum value" in test.name.lower()
    if constraint == "required":
        if success:
            return test.kind == "valid" and test.expected_valid
        return (\n            test.kind == "missing_required"\n            and "missing required field" in test.name.lower()\n        )
    if constraint == "not_found":
        return "not found" in test.name.lower() and test.expected_status == 404
    return True


def _gap_row(
    criterion: Criterion, reason: GapReason, detail: str, eligible: int = 0
) -> TraceRow:
    return TraceRow(
        requirement_id=criterion.id,
        description=criterion.description,
        method=criterion.method.upper(),
        path=criterion.path,
        expected_status=criterion.expected_status,
        eligible_tests=eligible,
        gap_reason=reason,
        gap_detail=detail,
    )


def map_requirements(
    criteria: Sequence[Criterion],
    tests: Sequence[MappableTest],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    top_k: int = DEFAULT_TOP_K,
    mapper: str = "tfidf",
) -> MappingResult:
    """Map criteria to tests with the eligibility + TF-IDF + threshold method."""
    if not 0.0 <= threshold <= 1.0:
        raise MappingError(f"threshold must be between 0 and 1, got {threshold}")
    if top_k < 1:
        raise MappingError(f"top_k must be at least 1, got {top_k}")
    criteria = sorted(criteria, key=lambda item: item.id)
    tests = sorted(tests, key=lambda item: item.case_id)
    if len({item.id for item in criteria}) != len(criteria):
        raise MappingError("criteria IDs must be unique")
    if len({item.case_id for item in tests}) != len(tests):
        raise MappingError("test case IDs must be unique")

    scores = _similarity_matrix(criteria, tests)
    position = {test.case_id: index for index, test in enumerate(tests)}
    rows: list[TraceRow] = []
    for row_index, criterion in enumerate(criteria):
        method = criterion.method.upper()
        same_operation = [
            test for test in tests
            if (test.method.upper(), test.path) == (method, criterion.path)
        ]
        if not same_operation:
            rows.append(_gap_row(
                criterion, "no_test_for_operation",
                f"no test exercises {method} {criterion.path}",
            ))
            continue
        same_status = [
            test for test in same_operation
            if test.expected_status == criterion.expected_status
        ]
        if not same_status:
            codes = sorted({test.expected_status for test in same_operation})
            available = ", ".join(str(code) for code in codes)
            rows.append(_gap_row(
                criterion, "no_test_with_expected_status",
                f"no test expects status {criterion.expected_status}; tests for "
                f"this operation expect: {available}",
            ))
            continue
        needs_body = criterion.field.startswith("response.body")
        body_ready = [
            test for test in same_status if not needs_body or "body" in test.checks
        ]
        if not body_ready:
            rows.append(_gap_row(
                criterion, "needs_response_body_check",
                f"the criterion is about {criterion.field}, but the "
                f"{len(same_status)} candidate test(s) check only "
                + "/".join(sorted({c for t in same_status for c in t.checks})),
            ))
            continue
        wanted = frozenset(_field_words(criterion.field))
        eligible = [
            test for test in body_ready
            if not wanted
            or not _field_words(test.field)
            or frozenset(_field_words(test.field)) == wanted
        ]
        if not eligible:
            targeted = sorted({test.field for test in body_ready})
            rows.append(_gap_row(
                criterion, "no_test_for_field",
                f"no test targets {criterion.field}; candidate tests target: "
                + ", ".join(targeted),
            ))
            continue
        constraint_eligible = [
            test for test in eligible if _constraint_compatible(criterion, test)
        ]
        if not constraint_eligible:
            rows.append(_gap_row(
                criterion,
                "no_test_for_constraint",
                f"eligible tests do not exercise constraint {criterion.constraint} "
                f"with value {criterion.value!r} and expected status "
                f"{criterion.expected_status}",
                eligible=len(eligible),
            ))
            continue
        eligible = constraint_eligible
        ranked = sorted(
            (
                (round(float(scores[row_index, position[test.case_id]]), 4), test)
                for test in eligible
            ),
            key=lambda pair: (-pair[0], pair[1].case_id),
        )
        candidates = [
            TraceMatch(case_id=test.case_id, name=test.name, score=score, rank=rank)
            for rank, (score, test) in enumerate(ranked, start=1)
        ]
        matches = [match for match in candidates if match.score >= threshold][:top_k]
        row = TraceRow(
            requirement_id=criterion.id,
            description=criterion.description,
            method=method,
            path=criterion.path,
            expected_status=criterion.expected_status,
            eligible_tests=len(eligible),
            matches=matches,
            candidates=candidates,
        )
        if not matches:
            row = row.model_copy(update={
                "gap_reason": "below_threshold",
                "gap_detail": (
                    f"best eligible test scored {candidates[0].score:.4f}, below "
                    f"the threshold {threshold}: {candidates[0].name}"
                ),
            })
        rows.append(row)
    return MappingResult(
        mapper=mapper, threshold=threshold, top_k=top_k, tests=len(tests), rows=rows
    )


def _similarity_matrix(
    criteria: Sequence[Criterion], tests: Sequence[MappableTest]
) -> Any:
    """Cosine similarity of TF-IDF vectors; rows are criteria, columns are tests."""
    import numpy as np

    if not criteria or not tests:
        return np.zeros((len(criteria), len(tests)))
    documents = [criterion_text(item) for item in criteria] + [
        case_text(item) for item in tests
    ]
    if not any(documents):
        return np.zeros((len(criteria), len(tests)))
    vectorizer = TfidfVectorizer(
        analyzer=str.split, sublinear_tf=True, smooth_idf=True, norm="l2"
    )
    matrix = vectorizer.fit_transform(documents)
    criteria_vectors = matrix[: len(criteria)]
    test_vectors = matrix[len(criteria):]
    return (criteria_vectors @ test_vectors.T).toarray()


class TfidfMapper:
    """The default offline mapper."""

    name = "tfidf"

    def __init__(
        self, threshold: float = DEFAULT_THRESHOLD, top_k: int = DEFAULT_TOP_K
    ) -> None:
        self.threshold = threshold
        self.top_k = top_k

    def map(
        self, criteria: Sequence[Criterion], tests: Sequence[MappableTest]
    ) -> MappingResult:
        return map_requirements(
            criteria, tests, threshold=self.threshold, top_k=self.top_k
        )


class DisabledLLMMapper:
    """Placeholder for a future LLM-assisted mapper. It never runs.

    The MVP is fully offline: this class exists only to mark where an optional,
    pluggable mapper would go. It makes no network calls and imports no provider.
    """

    name = "llm"

    def map(
        self, criteria: Sequence[Criterion], tests: Sequence[MappableTest]
    ) -> MappingResult:
        raise LLMDisabledError(
            "The LLM mapper is disabled. assay's MVP maps requirements offline; "
            "use --mapper tfidf."
        )


def get_mapper(
    name: str, threshold: float = DEFAULT_THRESHOLD, top_k: int = DEFAULT_TOP_K
) -> RequirementMapper:
    """Return a mapper by name."""
    if name == "tfidf":
        return TfidfMapper(threshold, top_k)
    if name == "llm":
        return DisabledLLMMapper()
    raise MappingError(f"unknown mapper {name!r}; choose 'tfidf' or 'llm'")


# --- reports -----------------------------------------------------------------


def _markdown(result: MappingResult) -> str:
    lines = [
        "# Traceability and gap report",
        "",
        f"Mapper `{result.mapper}`, threshold {result.threshold}, "
        f"top-k {result.top_k}. {len(result.rows)} criteria, {result.tests} tests: "
        f"{len(result.mapped)} mapped, {len(result.gaps)} with no mapped test.",
        "",
        "A mapped test is textually and structurally plausible for the criterion. "
        "It is not proof that the test verifies it: mapping quality is measured "
        "separately against hand-labelled pairs.",
        "",
        "## Traceability",
        "",
        "| Requirement | Mapped tests (score) |",
        "| --- | --- |",
    ]
    for row in result.rows:
        if row.matches:
            cell = "<br>".join(
                f"{m.case_id} ({m.score:.4f}) {m.name}" for m in row.matches
            )
        else:
            cell = "—"
        lines.append(f"| {row.requirement_id} | {cell} |")
    lines += ["", "## Gaps: criteria with no mapped test", ""]
    if not result.gaps:
        lines.append("None.")
    else:
        lines += ["| Requirement | Reason | Detail |", "| --- | --- | --- |"]
        for row in result.gaps:
            lines.append(
                f"| {row.requirement_id} | {row.gap_reason} | {row.gap_detail} |"
            )
    return "\n".join(lines)


def write_reports(result: MappingResult, output_dir: Path) -> dict[str, Path]:
    """Write mapping.json, traceability.json and report.md, deterministically."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    traceability = {
        "mapper": result.mapper,
        "threshold": result.threshold,
        "top_k": result.top_k,
        "summary": {
            "criteria": len(result.rows),
            "tests": result.tests,
            "mapped": len(result.mapped),
            "gaps": len(result.gaps),
        },
        "requirements": [row.model_dump(mode="json") for row in result.rows],
    }
    files = {
        "mapping.json": json.dumps(result.case_mapping(), indent=2, sort_keys=True),
        "traceability.json": json.dumps(traceability, indent=2, sort_keys=True),
        "report.md": _markdown(result),
    }
    paths: dict[str, Path] = {}
    for name, text in files.items():
        paths[name] = output_dir / name
        paths[name].write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    return paths


# --- command line ------------------------------------------------------------


def register_commands(commands: Any) -> None:
    """Add the `map` subcommand to the assay CLI."""
    command = commands.add_parser(
        "map", help="map acceptance criteria to generated tests and report gaps"
    )
    command.add_argument("--spec", type=Path, default=Path("specs/openapi.json"))
    command.add_argument("--criteria", type=Path, default=Path("criteria.yaml"))
    command.add_argument("--output", type=Path, default=Path("reports/mapping"))
    command.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    command.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    command.add_argument("--mapper", choices=("tfidf", "llm"), default="tfidf")


def handle(arguments: argparse.Namespace) -> int:
    """Run `assay map`. Returns 0 on success and 2 on error."""
    try:
        mapper = get_mapper(arguments.mapper, arguments.threshold, arguments.top_k)
        criteria = load_criteria(arguments.criteria)
        tests = baseline_tests_from_spec(load_spec(arguments.spec))
        result = mapper.map(criteria, tests)
        write_reports(result, arguments.output)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        f"Mapped {len(result.mapped)} of {len(result.rows)} criteria "
        f"({len(result.gaps)} gaps) to {result.tests} tests"
    )
    print(f"Reports written to {arguments.output}")
    return 0
