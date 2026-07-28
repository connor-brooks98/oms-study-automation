from pathlib import Path


def test_rollout_documents_connection_source_and_output_contracts():
    root = Path(__file__).parents[2]
    rollout = (root / "docs" / "native-quizzes-nuc-rollout.md").read_text(
        encoding="utf-8"
    )

    assert "Connect Notebook" in rollout
    assert "no OAuth JSON upload" in rollout
    assert "/public/quizzes" in rollout
    assert "Lecture Outline" in rollout
    assert "Not started" in rollout
    assert "Completed" in rollout
    assert "canonical" in rollout.casefold()
    assert "exactly two source IDs" in rollout
    assert "@lmunet.edu" in rollout
    assert "Allow" in rollout
    assert "Bypass policy that applies to everyone" not in rollout
