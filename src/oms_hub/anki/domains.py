from collections.abc import Sequence

_DOMAIN_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Cardio", ("cardio", "cardiology")),
    ("Derm", ("dermat",)),
    ("Endocrine", ("endocr", "diabetes")),
    ("GI", ("gastro", "::gi", "hepato")),
    ("Heme", ("hemat", "heme", "anemia")),
    ("MSK", ("musculoskel", "::msk", "rheumat")),
    ("Micro", ("micro", "bacter", "virus", "fung", "parasite")),
    ("Neuro", ("neuro",)),
    ("OBGYN", ("obgyn", "ob_gyn", "reproductive")),
    ("Pharm", ("pharm", "drug")),
    ("Psych", ("psych",)),
    ("Pulm", ("pulm", "respiratory")),
    ("Renal", ("renal", "nephro")),
)


def assign_domains(tags: Sequence[str]) -> tuple[str, ...]:
    """Assign zero or more deterministic soft domains from retained source tags."""
    searchable = "\n".join(tag.casefold() for tag in tags)
    return tuple(
        domain
        for domain, markers in _DOMAIN_RULES
        if any(marker in searchable for marker in markers)
    )
