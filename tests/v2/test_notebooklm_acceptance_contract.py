from pathlib import Path


def test_rollout_documents_connection_source_and_output_contracts():
    root = Path(__file__).parents[2]
    rollout = (root / "docs" / "native-quizzes-nuc-rollout.md").read_text(
        encoding="utf-8"
    )

    assert "Client file saved" in rollout
    assert "Lecture N Quiz" in rollout
    assert "canonical" in rollout.casefold()
    assert "exactly two source IDs" in rollout
    assert "@lmunet.edu" in rollout
    assert "Allow" in rollout
    assert "Bypass policy that applies to everyone" not in rollout
