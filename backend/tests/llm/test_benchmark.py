"""The match-quality benchmark (#2): a model's verdicts against labels."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.core.management.base import CommandError
from pydantic_ai.models.function import FunctionModel

from apps.llm import benchmark, registry
from apps.llm.benchmark import BANDS, agreement, band_of
from tests.factories import make_job
from tests.llm.test_commands import ledger, run, score_by_hash


@pytest.mark.parametrize(
    ("score", "band"),
    [(100, "strong"), (85, "strong"), (84, "apply"), (70, "apply"), (69, "maybe"), (50, "maybe"),
     (49, "skip"), (0, "skip")],
)  # fmt: skip
def test_a_score_is_the_rubrics_band(score: int, band: str) -> None:
    assert band_of(score) == band


def test_agreement_counts_what_the_product_acts_on() -> None:
    labels = {"a": "strong", "b": "apply", "c": "maybe", "d": "skip", "e": "skip", "f": "apply"}
    scores = {
        "a": 90,  # strong, as labelled
        "b": 88,  # apply read as strong: one band off, and kept
        "c": 30,  # maybe read as skip: one band off
        "d": 20,  # skip, as labelled, and archived
        "e": 72,  # skip read as apply: two bands off, and kept when it should not be
        "f": 40,  # apply read as skip: two bands off, and archived when it is worth applying to
        "unlabelled": 99,
    }
    found = agreement(labels, scores)
    assert (found.judged, found.exact, found.within_one) == (6, 2, 4)
    assert (found.worth, found.kept) == (3, 2)  # a, b and f are worth applying to; f was lost
    assert (found.skips, found.archived) == (2, 1)
    assert found.confusion["skip"] == {"skip": 1, "apply": 1}
    assert found.confusion["apply"] == {"strong": 1, "skip": 1}
    assert set(found.confusion) == set(BANDS)
    assert found.share(2, 3) == "2/3" and found.share(0, 0) == "none"


def _files(tmp_path: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([{"id": row["id"], "label": row["label"]} for row in rows]))
    postings = tmp_path / "postings.jsonl"
    postings.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "company": "Acme",
                    "locations": ["Oakland, CA"],
                    "remote": "hybrid",
                    "description": "Python and Postgres. " * 50,
                }
            )
            for row in rows
            if row.get("text", True)
        )
    )
    return labels, postings


@pytest.mark.django_db
def test_labelled_postings_take_their_text_from_the_file_then_the_mirror(tmp_path: Path) -> None:
    in_mirror = make_job(title="Still in the mirror", description_text="Go and Kafka. " * 50)
    labels, postings = _files(
        tmp_path,
        [
            {"id": "greenhouse:acme:1", "label": "apply", "title": "Backend Engineer"},
            {"id": in_mirror.id, "label": "skip", "title": "(from the mirror)", "text": False},
            {"id": "greenhouse:gone:9", "label": "maybe", "title": "Nowhere", "text": False},
            {"id": "greenhouse:acme:2", "label": "excellent", "title": "Not a label"},
        ],
    )
    loaded = benchmark.load(postings, labels)
    assert [(item.id, item.label) for item in loaded] == [
        ("greenhouse:acme:1", "apply"),
        (in_mirror.id, "skip"),
    ]
    assert loaded[1].posting["title"] == "Still in the mirror"
    scored = benchmark.score_sample(loaded)
    assert (scored[0].title, scored[0].company) == ("Backend Engineer", "Acme")
    evaluated = benchmark.eval_sample(loaded)
    assert evaluated[0][0] == "greenhouse:acme:1" and evaluated[0][1].posted_at is None


@pytest.mark.django_db(transaction=True)
def test_the_command_scores_the_labelled_postings_and_says_how_often_it_agreed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {"id": f"greenhouse:acme:{n}", "label": BANDS[n % 4], "title": f"Role {n}"}
        for n in range(8)
    ]
    labels, postings = _files(tmp_path, rows)
    monkeypatch.setattr(benchmark, "LABELS_PATH", labels)
    model = FunctionModel(score_by_hash(0))
    monkeypatch.setattr(registry, "base_model", lambda ref: model)
    out_file = tmp_path / "answers.json"

    code, out, err = run(
        "match_benchmark",
        "--models",
        "openai:gpt-5.6-luna",
        "--postings",
        str(postings),
        "--out",
        str(out_file),
    )
    assert (code, err) == (0, "")
    assert "8 labelled postings: 2 skip, 2 maybe, 2 apply, 2 strong" in out
    assert "the label's band exactly:" in out and "/8" in out
    assert "kept (at 55+) of those worth applying to:" in out
    answers = json.loads(out_file.read_text())["openai:gpt-5.6-luna"]
    assert len(answers) == 8 and {a["label"] for a in answers} == set(BANDS)
    # Real calls would be ledgered as a bake-off's, with no user.
    assert {(row["label"], row["user_id"], row["purpose"]) for row in ledger()} == {
        ("bakeoff", None, "score")
    }
    # A model that is not one is refused before anything is called.
    with pytest.raises(CommandError, match="Not a model: luna"):
        run("match_benchmark", "--models", "luna", "--postings", str(postings))


@pytest.mark.django_db
def test_a_rehearsal_never_calls_a_real_model(
    tmp_path: Path, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model named on the command line is called for real whatever `LLM_FAKE` says, which
    once cost three cents on a rehearsal. With `LLM_FAKE` on, only a fake may be named."""
    labels, postings = _files(
        tmp_path, [{"id": "greenhouse:acme:1", "label": "apply", "title": "Backend Engineer"}]
    )
    monkeypatch.setattr(benchmark, "LABELS_PATH", labels)
    called: list[str] = []
    monkeypatch.setattr(registry, "base_model", lambda ref: called.append(ref))
    env(llm_fake=True, debug=True)
    with pytest.raises(CommandError, match="LLM_FAKE is on, and openai:gpt-5.6-luna would be"):
        run("match_benchmark", "--models", "openai:gpt-5.6-luna", "--postings", str(postings))
    assert called == []
