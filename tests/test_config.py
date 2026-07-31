from oms_hub.config import Settings


def test_blank_public_hostname_disables_public_host() -> None:
    settings = Settings(public_hostname="")

    assert settings.public_hostname is None
