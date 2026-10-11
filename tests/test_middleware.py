import asyncio

from app.middleware import BodyLimitMiddleware


def test_chunked_body_is_limited_before_application_runs():
    invoked = False
    messages = []

    async def app(scope, receive, send):
        nonlocal invoked
        invoked = True

    chunks = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ]
    )

    async def receive():
        return next(chunks)

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "method": "POST", "headers": [], "path": "/api/files/"}
    asyncio.run(BodyLimitMiddleware(app, 5)(scope, receive, send))
    assert not invoked
    assert messages[0]["status"] == 413
