"""HTTP client for the sandbox executor (sandbox/executor.py). Needs EXECUTOR_URL and EXECUTOR_TOKEN."""

import httpx

from agentkit.config import env


class ExecutorError(RuntimeError):
    pass


class ExecutorClient:
    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self.url = (url or env("EXECUTOR_URL", "http://127.0.0.1:8765") or "").rstrip("/")
        self.token = token or env("EXECUTOR_TOKEN", required=True)

    async def _post(self, path: str, body: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(
                    self.url + path, json=body, headers={"Authorization": f"Bearer {self.token}"}
                )
        except httpx.HTTPError as exc:
            raise ExecutorError(f"executor unreachable: {type(exc).__name__}") from exc
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not data.get("ok"):
            raise ExecutorError(f"executor {r.status_code}: {data.get('error', 'failed')}")
        return data

    async def reset(self) -> None:
        await self._post("/reset", {})

    async def act(self, **body) -> dict:
        """One action; the result carries a fresh base64 PNG in 'screenshot'."""
        return await self._post("/action", body)

    async def clipboard_after(self, *keys: str) -> str:
        """Press key combos (e.g. ctrl+a, ctrl+c), then return the clipboard text."""
        for k in keys:
            await self.act(action="key", key=k)
        return (await self.act(action="read_clipboard")).get("text", "")
