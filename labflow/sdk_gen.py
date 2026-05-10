"""Generate a tiny stdlib Python client from a LabFlow OpenAPI spec (v0.15).

``labflow gen-sdk --spec openapi.json --out client.py`` produces a
single-file, dependency-free Python module exposing one ``Client`` class
with one method per ``operationId`` (or per ``METHOD path`` if the
operation has no id). Methods accept path parameters as positional args
and the request body / query as kwargs.

This is *intentionally* minimal — it's a starter file teams can extend,
not a 100%-coverage generator. We don't try to model the response
schemas; everything returns ``dict | list | None``.
"""
from __future__ import annotations

import json
import re
import textwrap
from typing import Any

_PATH_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_HEADER = '''"""Auto-generated LabFlow client — DO NOT EDIT.

Re-run ``labflow gen-sdk --spec <openapi.json> --out <this file>`` to
regenerate. Pure stdlib (urllib + json) — no third-party deps.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Optional


class LabflowError(Exception):
    """Raised when the server returns a non-2xx response."""

    def __init__(self, status: int, body: Any):
        super().__init__(f"HTTP {status}: {body!r}")
        self.status = status
        self.body = body


class Client:
    def __init__(self, base_url: str, *, api_key: Optional[str] = None,
                 timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # ----- transport -------------------------------------------------
    def _request(self, method: str, path: str, *,
                 query: Optional[dict[str, Any]] = None,
                 body: Any = None) -> Any:
        url = self.base_url + path
        if query:
            qs = urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}
            )
            if qs:
                url += ("&" if "?" in url else "?") + qs
        headers = {"Accept": "application/json"}
        data: Optional[bytes] = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct:
                    return json.loads(raw.decode("utf-8"))
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:  # pragma: no cover (network)
            body_text = e.read().decode("utf-8", errors="replace")
            try:
                body_obj = json.loads(body_text)
            except json.JSONDecodeError:
                body_obj = body_text
            raise LabflowError(e.code, body_obj) from None
'''


def _sanitize_method_name(name: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    if not out:
        out = "op"
    if out[0].isdigit():
        out = "op_" + out
    return out


def _operation_id(method: str, path: str, op: dict[str, Any]) -> str:
    if isinstance(op.get("operationId"), str) and op["operationId"]:
        return _sanitize_method_name(op["operationId"])
    return _sanitize_method_name(f"{method}_{path}")


def _path_params(path: str) -> list[str]:
    return _PATH_PARAM.findall(path)


def _render_method(method: str, path: str, op: dict[str, Any]) -> str:
    name = _operation_id(method, path, op)
    path_params = _path_params(path)
    has_body = method.upper() in {"POST", "PUT", "PATCH"}
    params = ["self"]
    params.extend(path_params)
    if has_body:
        params.append("body: Any = None")
    params.append("query: Optional[dict[str, Any]] = None")
    sig = ", ".join(params)
    body_arg = "body=body" if has_body else "body=None"
    summary = (op.get("summary") or "").strip()
    doc = f'        """{summary or method.upper() + " " + path}"""\n' \
        if summary or path else ""
    # Build the path interpolation safely.
    if path_params:
        fmt = path
        for p in path_params:
            fmt = fmt.replace("{" + p + "}", "{" + p + "}")
        path_expr = f'f{fmt!r}'
    else:
        path_expr = repr(path)
    return (
        f"    def {name}({sig}) -> Any:\n"
        f"{doc}"
        f"        return self._request({method.upper()!r}, {path_expr},"
        f" query=query, {body_arg})\n"
    )


def render(spec: dict[str, Any]) -> str:
    """Return the source of a stdlib Python client for ``spec``."""
    if not isinstance(spec, dict):
        raise ValueError("spec must be an OpenAPI dict")
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        raise ValueError("spec.paths must be an object")
    methods_src: list[str] = []
    seen: set[str] = set()
    for path, item in sorted(paths.items()):
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in {"get", "post", "put",
                                       "patch", "delete"}:
                continue
            if not isinstance(op, dict):
                continue
            base = _operation_id(method, path, op)
            name = base
            n = 2
            while name in seen:
                name = f"{base}_{n}"
                n += 1
            seen.add(name)
            # Inject the (possibly disambiguated) name back into the op
            # so the rendered method picks it up.
            op_with_name = dict(op)
            op_with_name["operationId"] = name
            methods_src.append(_render_method(method, path, op_with_name))
    title = (spec.get("info") or {}).get("title", "LabFlow")
    version = (spec.get("info") or {}).get("version", "")
    banner = textwrap.dedent(f'''
        # {title} client
        # OpenAPI version: {version}
        # Operations: {len(methods_src)}
    ''').strip()
    body = _HEADER + "\n    # ----- generated operations --------------------------------\n"
    body += "\n".join(methods_src) if methods_src else "    pass\n"
    return f"{banner}\n{body}\n"


def render_from_json(spec_text: str) -> str:
    return render(json.loads(spec_text))
