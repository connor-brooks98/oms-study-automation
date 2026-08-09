"""Human-facing lecture labels for web presentation surfaces."""


def lecture_label(subject: str, lecture_number: int) -> str:
    """Return the compact course-relative label without exposing database IDs."""
    return f"{subject} Lecture {lecture_number:02d}"
