import asyncio
import os
import unittest
from http.cookies import SimpleCookie
from unittest.mock import patch

import httpx

import server


class TrackerSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.session = server.TrackerSession()
        self.enterContext(
            patch.dict(
                os.environ,
                {"SIMPLIFY_REFRESH": "saved-refresh", "SIMPLIFY_CSRF": "csrf%2Etoken"},
            )
        )

    def assert_cookies(self, request: httpx.Request, name: str, token: str) -> None:
        cookies = SimpleCookie(request.headers["Cookie"])
        self.assertEqual(
            {key: value.value for key, value in cookies.items()},
            {name: token, "csrf": "csrf%2Etoken"},
        )
        self.assertEqual(request.headers["X-CSRF-Token"], "csrf.token")

    async def test_reuses_access_and_renews_only_on_401(self) -> None:
        exchanges = iter(
            [
                ("refresh", "saved-refresh", 200, "access-1"),
                ("authorization", "access-1", 200, None),
                ("authorization", "access-1", 403, None),
                ("authorization", "access-1", 401, None),
                ("refresh", "saved-refresh", 200, "access-2"),
                ("authorization", "access-2", 200, None),
            ]
        )

        def reply(request: httpx.Request) -> httpx.Response:
            name, value, status, new_access = next(exchanges)
            self.assert_cookies(request, name, value)
            headers = {"Set-Cookie": f"authorization={new_access}"} if new_access else {}
            return httpx.Response(status, headers=headers)

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            for expected in (200, 200, 403, 200, 200):
                response = await self.session.get(client, {"page": 0})
                self.assertEqual(response.status_code, expected)
        self.assertIsNone(next(exchanges, None))

    async def test_rejected_refresh_reports_sign_in(self) -> None:
        cookies = iter(("authorization", "refresh"))

        def reply(request: httpx.Request) -> httpx.Response:
            name = next(cookies)
            self.assert_cookies(
                request, name, "expired-access" if name == "authorization" else "saved-refresh"
            )
            return httpx.Response(401)

        self.session._access = ("expired-access", "csrf%2Etoken")
        with patch.object(server, "tracker_session", self.session):
            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                with self.assertRaisesRegex(RuntimeError, "Sign in to Simplify"):
                    await server.fetch_tracker_page(client, 0)
        self.assertIsNone(next(cookies, None))

    async def test_concurrent_401s_share_one_renewal(self) -> None:
        both_rejected = asyncio.Barrier(2)
        refresh_count = 0

        async def reply(request: httpx.Request) -> httpx.Response:
            nonlocal refresh_count
            cookies = SimpleCookie(request.headers["Cookie"])
            if "refresh" in cookies:
                self.assert_cookies(request, "refresh", "saved-refresh")
                refresh_count += 1
                return httpx.Response(
                    200, headers={"Set-Cookie": f"authorization=access-{refresh_count}"}
                )
            if cookies["authorization"].value == "access-1":
                await both_rejected.wait()
                return httpx.Response(401)
            self.assert_cookies(request, "authorization", "access-2")
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            await self.session.get(client, {"page": 0})
            responses = await asyncio.wait_for(
                asyncio.gather(
                    self.session.get(client, {"page": 0}),
                    self.session.get(client, {"page": 1}),
                ),
                timeout=5,
            )
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(refresh_count, 2)
