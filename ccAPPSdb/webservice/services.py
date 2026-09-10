from channels.generic.http import AsyncHttpConsumer
from .commands import WebService


class StopService(AsyncHttpConsumer):
    msgtemplate = """
        <!doctype html>
        <html lang="en">
            <head>
            <meta http-equiv="content-type" content="text/html; charset=utf-8">
            </head>
            <body>%s</body>
        </html>
        """

    async def handle(self, body):
        self.scope["response_headers"].append((b"Content-Type", b"text/html"))
        if self.scope["method"] != "POST":
            return await self.send_response(
                401,
                (self.msgtemplate % "Only POST requests allowed").encode("utf-8"),
                headers=self.scope["response_headers"],
            )
        else:
            # TODO wait for lock?
            await self.send_response(
                404,
                (self.msgtemplate % "Shutting down").encode("utf-8"),
                headers=self.scope["response_headers"],
            )
            WebService.stop()


class PingService(AsyncHttpConsumer):
    async def handle(self, body):
        self.scope["response_headers"].append((b"Content-Type", b"text/plain"))
        await self.send_response(
            200,
            b"OK",
            headers=self.scope["response_headers"],
        )
