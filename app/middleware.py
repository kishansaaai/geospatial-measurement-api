"""Bound request bodies before multipart parsing, including chunked uploads."""

from tempfile import SpooledTemporaryFile

from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        try:
            too_large = length is not None and int(length) > self.max_bytes
        except ValueError:
            response = JSONResponse(
                {"error": {"code": "INVALID_CONTENT_LENGTH", "message": "Invalid Content-Length header."}},
                400,
            )
            return await response(scope, receive, send)
        if too_large:
            return await self.reject(scope, receive, send)
        # Spool to disk after 1 MiB; all resources close even if the client disconnects.
        with SpooledTemporaryFile(max_size=1024 * 1024) as body:
            total = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > self.max_bytes:
                    return await self.reject(scope, receive, send)
                body.write(chunk)
                if not message.get("more_body", False):
                    break
            body.seek(0)
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if delivered:
                    return await receive()
                chunk = body.read(64 * 1024)
                more = body.tell() < total
                if not more:
                    delivered = True
                return {"type": "http.request", "body": chunk, "more_body": more}

            await self.app(scope, bounded_receive, send)

    async def reject(self, scope, receive, send):
        response = JSONResponse(
            {"error": {"code": "UPLOAD_TOO_LARGE", "message": "The request exceeds the upload limit."}}, 413
        )
        await response(scope, receive, send)
