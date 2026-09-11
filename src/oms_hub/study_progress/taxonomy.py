"""Versioned top-level official categories; no inferred question mappings or crosswalks.

NBOME's source identifiers are dimension + number. USMLE publishes headings rather
than category codes: preserve the heading as source_id and use a local ordinal only
inside the versioned canonical ID. These catalogs do not import nested topic lists.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    canonical_id: str
    source_id: str
    label: str


@dataclass(frozen=True)
class Taxonomy:
    namespace: str
    version: str
    title: str
    source_url: str
    specifications_url: str
    verified_on: str
    applicability: str
    categories: tuple[Category, ...]


TAXONOMIES = (
    Taxonomy(
        "nbome_comlex_level1",
        "2025-02",
        "NBOME COMLEX-USA Level 1",
        "https://www.nbome.org/app/uploads/2025/05/COMLEX-USA-Blueprint-2025.pdf",
        "https://www.nbome.org/assessments/comlex-usa/comlex-usa-blueprint/",
        "2026-09-11",
        "February 2025 blueprint; Level 1 through April 8, 2028. "
        "Blueprint 2.0 applies from May 2028 and is a separate future version.",
        tuple(
            Category(
                f"nbome_comlex_level1:2025-02:{dimension}:{number}", f"{dimension}:{number}", label
            )
            for dimension, labels in (
                (
                    "dimension1",
                    (
                        "Osteopathic Principles, Practice, and Manipulative Treatment",
                        "Osteopathic Patient Care and Procedural Skills",
                        "Application of Knowledge for Osteopathic Medical Practice",
                        "Practice-Based Learning and Improvement in Osteopathic Medical Practice",
                        "Interpersonal and Communication Skills in the Practice "
                        "of Osteopathic Medicine",
                        "Professionalism in the Practice of Osteopathic Medicine",
                        "Systems-Based Practice in Osteopathic Medicine",
                    ),
                ),
                (
                    "dimension2",
                    (
                        "Community Health and Patient Presentations Related to Wellness",
                        "Human Development, Reproduction, and Sexuality",
                        "Endocrine System and Metabolism",
                        "Nervous System and Mental Health",
                        "Musculoskeletal System",
                        "Genitourinary/Renal System and Breasts",
                        "Gastrointestinal System and Nutritional Health",
                        "Circulatory and Hematologic Systems",
                        "Respiratory System",
                        "Integumentary System",
                    ),
                ),
            )
            for number, label in enumerate(labels, 1)
        ),
    ),
    Taxonomy(
        "usmle_step1",
        "2026",
        "USMLE Step 1 (shared USMLE content outline)",
        "https://www.usmle.org/sites/default/files/2022-01/USMLE_Content_Outline_0.pdf",
        "https://www.usmle.org/exam-resources/step-1-materials/"
        "step-1-content-outline-and-specifications",
        "2026-09-11",
        "2026 shared outline; Step 1 specifications determine emphasis. "
        "The URL's 2022-01 directory is not the document version.",
        tuple(
            Category(f"usmle_step1:2026:content:{number}", label, label)
            for number, label in enumerate(
                (
                    "Human Development",
                    "Immune System",
                    "Blood & Lymphoreticular System",
                    "Behavioral Health",
                    "Nervous System & Special Senses",
                    "Skin & Subcutaneous Tissue",
                    "Musculoskeletal System",
                    "Cardiovascular System",
                    "Respiratory System",
                    "Gastrointestinal System",
                    "Renal & Urinary System",
                    "Pregnancy, Childbirth, & the Puerperium",
                    "Female and Transgender Reproductive System & Breast",
                    "Male and Transgender Reproductive System",
                    "Endocrine System",
                    "Multisystem Processes & Disorders",
                    "Biostatistics, Epidemiology/Population Health, & Interpretation "
                    "of the Medical Literature",
                    "Social Sciences",
                ),
                1,
            )
        ),
    ),
)
