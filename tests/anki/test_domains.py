from oms_hub.anki.domains import assign_domains


def test_domain_assignment_is_multi_valued_and_deterministic() -> None:
    tags = (
        "#AK_Step1_v12::Pharmacology::Hematology",
        "#Pathoma::Heme_Onc",
    )

    assert assign_domains(tags) == ("Heme", "Pharm")
    assert assign_domains(tuple(reversed(tags))) == ("Heme", "Pharm")


def test_unknown_tags_remain_searchable_without_a_false_domain() -> None:
    assert assign_domains(("AnkiHub_Optional::LMU_OMS_II::Foundations",)) == ()
