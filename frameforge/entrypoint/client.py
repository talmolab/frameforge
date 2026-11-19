"""HTTP clients for a node's management server; the hub imports these.

``NodeClient`` has one method per registry verb, generated at import, so the list
of calls is ``VERBS`` and nothing else. ``SetupClient`` drives the interactive setup
flow, which is not a verb: fetch the questions, post the answers.
"""

import dataclasses
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

from . import node as _registered  # noqa: F401
from .types import ActionResult, decode
from .verbs import SETUP_PATH, SETUP_QUESTIONS_PATH, TOKEN_HEADER, VERBS, Verb, route


class NodeError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class _Transport:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None, stream: bool = False):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if method == "POST" and self.token:
            headers[TOKEN_HEADER] = self.token

        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=None if stream else self.timeout)
        except urllib.error.HTTPError as error:
            raise NodeError(error.code, error.read().decode(errors="replace")) from None
        except urllib.error.URLError as error:
            raise NodeError(0, str(error.reason)) from None
        if stream:
            return _lines(response)
        with response:
            payload = response.read()
        return json.loads(payload) if payload else None


def _lines(response) -> Iterator[str]:
    with response:
        for raw in response:
            line = raw.decode(errors="replace").rstrip("\n")
            if line.startswith("data: "):
                yield line[6:]


class NodeClient(_Transport):
    """One method per verb in ``entrypoint.verbs.VERBS``, each returning the verb's type."""

    def _call(self, spec: Verb, **kwargs):
        method, path = route(spec)
        if method == "GET":
            payload = self.request(method, path, params=kwargs, stream=spec.streams)
        else:
            payload = self.request(method, path, body=kwargs)
        return decode(spec.returns, payload)


def _method(spec: Verb):
    def call(self, **kwargs):
        return self._call(spec, **kwargs)
    call.__name__ = spec.name
    call.__doc__ = spec.help + "".join(f"\n  {arg.name}: {arg.help}" for arg in spec.args)
    return call


for _spec in VERBS.values():
    setattr(NodeClient, _spec.name, _method(_spec))


class SetupClient(_Transport):
    """The setup flow over HTTP: render ``questions`` as a form, post the answers to ``run``."""

    def questions(self, mode: str) -> list[dict]:
        return self.request("GET", SETUP_QUESTIONS_PATH.format(mode=mode))

    def run(self, mode: str, answers: dict) -> ActionResult:
        payload = self.request("POST", SETUP_PATH.format(mode=mode), body=answers)
        return decode("ActionResult", payload)


def to_json(value):
    """A verb result as a JSON-ready structure, for templates or a FastAPI response."""
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [to_json(item) for item in value]
    return value
