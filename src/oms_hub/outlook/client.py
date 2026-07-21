from datetime import datetime
from typing import Protocol

import httpx

from oms_hub.outlook.sync import OutlookEvent


class TokenProvider(Protocol):
    def access_token(self) -> str: ...


class GraphCalendarClient:
    def __init__(
        self,
        tokens: TokenProvider,
        http: httpx.Client | None = None,
    ):
        self.tokens = tokens
        self.http = http or httpx.Client(timeout=30.0)

    def list_events(
        self,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[OutlookEvent]:
        events: list[OutlookEvent] = []
        url: str | None = (
            "https://graph.microsoft.com/v1.0/me/calendarView"
        )
        params: dict[str, str] | None = {
            "startDateTime": start_utc.isoformat(),
            "endDateTime": end_utc.isoformat(),
            "$select": (
                "id,subject,start,lastModifiedDateTime,isCancelled"
            ),
            "$orderby": "start/dateTime",
            "$top": "200",
        }
        headers = {
            "Authorization": f"Bearer {self.tokens.access_token()}",
            "Prefer": 'outlook.timezone="UTC"',
        }
        while url:
            response = self.http.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
            for raw in payload.get("value", []):
                if raw.get("isCancelled"):
                    continue
                events.append(OutlookEvent.from_graph(raw))
            url = payload.get("@odata.nextLink")
            params = None
        return events
