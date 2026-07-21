from collections.abc import Callable

from msal import (  # type: ignore[import-untyped]
    PublicClientApplication,
    SerializableTokenCache,
)

from oms_hub.security.secret_store import SecretStore

# MSAL automatically includes the reserved OIDC and offline-access scopes.
SCOPES = ["Calendars.Read"]


class OutlookAuthenticationRequired(RuntimeError):
    pass


class OutlookTokenProvider:
    def __init__(
        self,
        client_id: str,
        tenant: str,
        secrets: SecretStore,
        notify: Callable[[str], None] = print,
    ):
        self.secrets = secrets
        self.notify = notify
        self.cache = SerializableTokenCache()
        serialized = secrets.get("outlook-msal-cache")
        if serialized:
            self.cache.deserialize(serialized)
        self.application = PublicClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant}",
            token_cache=self.cache,
        )

    def _save(self) -> None:
        if self.cache.has_state_changed:
            self.secrets.set("outlook-msal-cache", self.cache.serialize())

    def login(self) -> None:
        flow = self.application.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise OutlookAuthenticationRequired(
                "Microsoft device login could not be started"
            )
        self.notify(str(flow["message"]))
        result = self.application.acquire_token_by_device_flow(flow)
        self._save()
        if "access_token" not in result:
            description = result.get(
                "error_description",
                result.get("error", "unknown error"),
            )
            raise OutlookAuthenticationRequired(str(description))

    def access_token(self) -> str:
        accounts = self.application.get_accounts()
        result = (
            self.application.acquire_token_silent(SCOPES, account=accounts[0])
            if accounts
            else None
        )
        self._save()
        if not result or "access_token" not in result:
            raise OutlookAuthenticationRequired(
                "Run 'oms-hub outlook-login' to authenticate"
            )
        return str(result["access_token"])
