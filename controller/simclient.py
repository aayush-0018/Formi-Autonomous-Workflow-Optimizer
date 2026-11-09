"""HTTP client for the simulator.

Transport errors and 5xx responses are retried with exponential backoff (the simulator is the outside world;
in production this is the platform API and it will blip). 4xx responses are *answers*, not failures: they are
returned to the caller as `ApiError` so governance code can branch on the error code (409 stale_base_version,
410 approval_lapsed, 402 out_of_credits, 403 auto_apply_not_permitted, ...).
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("controller.http")


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class SimClient:
    def __init__(self, base_url: str, timeout: float = 300.0, max_retries: int = 8):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.max_retries = max_retries

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.request(method, path, **kw)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise
                log.warning("transport error on %s %s (%s); retrying in %.1fs", method, path, e, delay)
            else:
                if r.status_code < 500:
                    return r
                if attempt == self.max_retries:
                    return r
                log.warning("%s on %s %s; retrying in %.1fs", r.status_code, method, path, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)
        raise RuntimeError("unreachable")

    def _json(self, method: str, path: str, **kw):
        r = self._req(method, path, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
                code = body.get("error") or str(body.get("detail"))
                detail = body.get("detail")
            except Exception:  # noqa: BLE001 - non-JSON error body
                code, detail = "http_error", r.text
            raise ApiError(r.status_code, code, detail)
        return r.json()

    # ---- read endpoints
    def state(self) -> dict:
        return self._json("GET", "/sim/state")

    def workflows(self) -> list[dict]:
        return self._json("GET", "/workflows")

    def workflow(self, wid: str) -> dict:
        return self._json("GET", f"/workflows/{wid}")

    def config(self, wid: str) -> dict:
        return self._json("GET", f"/workflows/{wid}/config")

    def config_history(self, wid: str) -> list[dict]:
        return self._json("GET", f"/workflows/{wid}/config/history")

    def chunks(self, kind: str, after: int = -1) -> list[dict]:
        return self._json("GET", "/events/chunks", params={"kind": kind, "after": after})

    def chunk_text(self, n: int) -> bytes:
        """Raw NDJSON bytes (no str decode: a 250k-event history chunk is ~130 MB)."""
        r = self._req("GET", f"/events/chunks/{n}")
        if r.status_code >= 400:
            raise ApiError(r.status_code, "chunk_error", r.text)
        return r.content

    def approval(self, aid: str) -> dict:
        return self._json("GET", f"/approvals/{aid}")

    def approvals(self, workflow_id: str | None = None, status: str | None = None) -> list[dict]:
        params = {k: v for k, v in (("workflow_id", workflow_id), ("status", status)) if v}
        return self._json("GET", "/approvals", params=params)

    # ---- write endpoints
    def advance(self, minutes: int) -> dict:
        return self._json("POST", "/sim/advance", json={"minutes": minutes})

    def request_approval(self, body: dict) -> dict:
        return self._json("POST", "/approvals", json=body)

    def apply(self, wid: str, approval_id: str) -> dict:
        return self._json("POST", f"/workflows/{wid}/apply", json={"approval_id": approval_id})

    def auto_apply(self, wid: str, body: dict) -> dict:
        return self._json("PUT", f"/workflows/{wid}/config", json=body)

    def suggest_fixes(self, wid: str, problem: str) -> dict:
        return self._json("POST", "/llm/suggest-fixes", json={"workflow_id": wid, "problem": problem})

    def validate(self, wid: str, prompt_version: str, n: int) -> dict:
        return self._json("POST", "/validation/runs", json={"workflow_id": wid, "prompt_version": prompt_version,
                                                             "n": n})
