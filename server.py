import asyncio
import os
from datetime import date, datetime
from typing import Annotated
from urllib.parse import unquote

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

TRACKER_URL = "https://api.simplify.jobs/v2/candidate/me/tracker/"
TRACKER_PAGE_SIZE = 500
SAVED = 1
APPLIED = 2
STATUS_NAMES = {
    APPLIED: "APPLIED",
    11: "SCREEN",
    12: "INTERVIEWING",
    13: "OFFER",
    21: "WITHDRAWN",
    22: "GHOSTED",
    23: "REJECTED",
    24: "ACCEPTED",
}

mcp = MCPServer("Simplify")


class Application(BaseModel):
    id: str
    title: str | None
    company: str | None
    location: str | None
    job_url: str | None
    status: str
    applied_on: date | None
    status_updated_on: date | None
    archived: bool


class ApplicationPage(BaseModel):
    applications: list[Application]
    total: int
    next_offset: int | None


class TrackerStatus(BaseModel):
    status: int
    timestamp: datetime | None


class TrackerCompany(BaseModel):
    name: str | None


class TrackerRecord(BaseModel):
    id: str
    company: TrackerCompany | None
    job_posting_title: str | None
    job_posting_location: str | None
    job_posting_url: str | None
    date_archived: datetime | None
    status_events: list[TrackerStatus] | None

    def to_application(self) -> Application | None:
        # Simplify's board uses the highest status code in the record's events.
        events = self.status_events or []
        current = max(events, key=lambda event: event.status, default=None)
        if current is None or current.status <= SAVED:
            return None

        applied_on = min(
            (
                event.timestamp.date()
                for event in events
                if event.status == APPLIED and event.timestamp is not None
            ),
            default=None,
        )
        return Application(
            id=self.id,
            title=self.job_posting_title,
            company=self.company.name if self.company else None,
            location=self.job_posting_location,
            job_url=self.job_posting_url,
            status=STATUS_NAMES.get(current.status, f"UNKNOWN_{current.status}"),
            applied_on=applied_on,
            status_updated_on=current.timestamp.date() if current.timestamp else None,
            archived=self.date_archived is not None,
        )


class TrackerPage(BaseModel):
    items: list[TrackerRecord]
    page: int
    pages: int
    total: int


class TrackerSession:
    def __init__(self) -> None:
        self._access: tuple[str, str] | None = None
        self._refresh_lock = asyncio.Lock()

    @staticmethod
    async def _get(
        client: httpx.AsyncClient,
        params: dict[str, int | str],
        cookie_name: str,
        token: str,
        csrf: str,
    ) -> httpx.Response:
        # Build each request explicitly so the client's cookie jar cannot send
        # the long-lived refresh cookie alongside a cached access cookie.
        request = httpx.Request(
            "GET",
            TRACKER_URL,
            params=params,
            cookies={cookie_name: token, "csrf": csrf},
            headers={"Accept": "application/json", "X-CSRF-Token": unquote(csrf)},
        )
        return await client.send(request)

    async def get(
        self, client: httpx.AsyncClient, params: dict[str, int | str]
    ) -> httpx.Response:
        access = self._access
        if access is not None:
            response = await self._get(client, params, "authorization", *access)
            if response.status_code != 401:
                return response

        async with self._refresh_lock:
            # Another tool call may have refreshed the access cookie while
            # this request was in flight or waiting for the lock.
            latest = self._access
            if latest is not None and latest != access:
                response = await self._get(client, params, "authorization", *latest)
                if response.status_code != 401:
                    return response

            self._access = None
            refresh = os.getenv("SIMPLIFY_REFRESH")
            csrf = os.getenv("SIMPLIFY_CSRF")
            if not refresh or not csrf:
                raise ValueError("Set SIMPLIFY_REFRESH and SIMPLIFY_CSRF using the README.")

            # Simplify issues a fresh access cookie on an authenticated tracker
            # read that uses the refresh cookie.
            response = await self._get(client, params, "refresh", refresh, csrf)
            if response.status_code == 200:
                token = response.cookies.get("authorization")
                if token:
                    self._access = (token, csrf)
            return response


tracker_session = TrackerSession()


async def fetch_tracker_page(client: httpx.AsyncClient, page: int) -> TrackerPage:
    try:
        response = await tracker_session.get(
            client,
            {
                "page": page,
                "size": TRACKER_PAGE_SIZE,
                "sort_by": "tracked_date",
                "sort_direction": "desc",
            },
        )
    except httpx.RequestError:
        raise RuntimeError("Could not reach the Simplify tracker.") from None

    if response.status_code == 401:
        raise RuntimeError(
            "Simplify rejected the saved session. Sign in to Simplify, replace both "
            "cookies in .env, and restart the MCP server. See the README."
        )
    if response.status_code == 429:
        raise RuntimeError("Simplify rate-limited the request. Try again later.")
    if response.status_code != 200:
        raise RuntimeError(f"Simplify returned HTTP {response.status_code}.")
    try:
        result = TrackerPage.model_validate_json(response.content)
    except ValidationError:
        raise RuntimeError("Simplify returned an unexpected tracker response.") from None
    if result.page != page:
        raise RuntimeError("Simplify returned the wrong tracker page.")
    return result


async def fetch_applications() -> list[Application]:
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        first = await fetch_tracker_page(client, 0)
        records = {record.id: record for record in first.items}
        for page in range(1, first.pages):
            result = await fetch_tracker_page(client, page)
            if result.total != first.total:
                raise RuntimeError(
                    "The Simplify tracker changed during pagination. Try again."
                )
            records.update((record.id, record) for record in result.items)

    if len(records) != first.total:
        raise RuntimeError("Simplify returned an incomplete tracker list. Try again.")
    applications = []
    for record in records.values():
        application = record.to_application()
        if application is not None:
            applications.append(application)
    return applications


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_applications(
    query: str | None = None,
    status: str | None = None,
    archived: bool | None = None,
    limit: Annotated[int, Field(ge=1)] | None = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> ApplicationPage:
    """List Simplify applications and their current tracker statuses.

    Includes active and archived applications by default and excludes saved-only
    jobs. Results are ordered by when the jobs were added to the tracker, newest
    first. Query matches company or title by substring. Status matches a
    Simplify status such as APPLIED or INTERVIEWING. Both are case-insensitive.
    Set archived to true or false to restrict the scope. Pass next_offset as
    offset for another page, or set limit to null for all matching results.
    """
    applications = await fetch_applications()
    search = (query or "").strip().casefold()
    status_filter = (status or "").strip().casefold()
    matches = [
        application
        for application in applications
        if (archived is None or application.archived == archived)
        and (not status_filter or application.status.casefold() == status_filter)
        and (
            not search
            or search in (application.title or "").casefold()
            or search in (application.company or "").casefold()
        )
    ]
    end = offset + limit if limit is not None else len(matches)
    return ApplicationPage(
        applications=matches[offset:end],
        total=len(matches),
        next_offset=end if end < len(matches) else None,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
