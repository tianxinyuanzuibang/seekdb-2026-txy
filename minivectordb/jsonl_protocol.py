"""The MiniVectorDB standard input/output protocol (JSON Lines).

The protocol is defined by the organisers in ``STDIO_PROTOCOL.md`` on the
``2026-competition-round2`` branch of ``oceanbase/seekdb`` and implemented
by their ``protocol_check`` driver.  It is JSON Lines: one JSON object per
physical line, in both directions.

    judge  -- {"id":1,"op":"execute","sql":"...","search_mode":"exact"} -->  database
    judge  <-- {"id":1,"ok":true,"columns":["id"],"rows":[[1]]}           --  database

The rules that matter for the implementation:

* The program opens ``/data``, recovers, and then writes exactly
  ``{"type":"ready"}`` and flushes - before the first request is read, and
  again after every restart.
* Every request gets exactly one response, echoing the request ``id``
  with its JSON type preserved (a number must not become a string).
* ``ok`` is a JSON boolean, never ``0``/``1``/``"true"``.
* A successful statement that has no result set answers with just
  ``{"id":N,"ok":true}``; a modification adds ``affected_rows``; a query
  adds ``columns`` and ``rows``.
* A failed statement answers ``{"id":N,"ok":false,"error":{"code":...,
  "message":...}}`` where ``code`` is a non-empty string, and the process
  keeps running.
* ``op":"shutdown"`` rolls back, flushes, answers ``{"id":N,"ok":true}``
  and exits with status 0.  Nothing may be written to stdout after that
  response - not even a blank line.
* stdout carries protocol messages only; everything else goes to stderr.

The legacy plain-text framing (SQL in, tab separated rows out) is still
supported for local use and for the mysqltest-style harness, and is
selected with ``MVD_PROTOCOL=line``; the JSONL protocol is the default
because it is what the judge speaks.
"""

import json
import os
import sys

from .results import Result, error_name


class JSONLProtocol:
    """The judge-facing protocol: one JSON object per line."""

    def __init__(self, in_stream, out_stream, runner, set_mode=None,
                 close_hook=None, data_dir=None):
        self.in_stream = in_stream
        self.out_stream = out_stream
        self.runner = runner
        self.set_mode = set_mode
        self.close_hook = close_hook
        self.data_dir = data_dir

    # -- output --------------------------------------------------------
    def _write(self, payload):
        """Write one JSON message and flush it immediately."""
        data = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        self.out_stream.write(data.encode("utf-8") + b"\n")
        self.out_stream.flush()

    def emit_ready(self):
        """The startup handshake - exactly this object, nothing more."""
        self._write({"type": "ready"})

    # -- input ---------------------------------------------------------
    def serve(self):
        self.emit_ready()
        while True:
            line = self.in_stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                request = json.loads(text)
            except ValueError as exc:
                sys.stderr.write(
                    "minivectordb: invalid request JSON: {}\n".format(exc)
                )
                sys.stderr.flush()
                continue
            if not isinstance(request, dict):
                sys.stderr.write(
                    "minivectordb: request is not a JSON object\n"
                )
                sys.stderr.flush()
                continue
            if not self._handle(request):
                break

    def _handle(self, request):
        """Handle one request.  Returns False when the program must exit."""
        ident = request.get("id")
        op = (request.get("op") or "execute").lower()

        if op == "shutdown":
            self._write({"id": ident, "ok": True})
            if self.close_hook is not None:
                try:
                    self.close_hook()
                except Exception:                  # pragma: no cover
                    pass
            try:
                self.out_stream.flush()
            except Exception:                      # pragma: no cover
                pass
            os._exit(0)

        if op != "execute":
            self._write({
                "id": ident,
                "ok": False,
                "error": {
                    "code": "unsupported_op",
                    "message": "unsupported operation {!r}".format(op),
                },
            })
            return True

        sql = request.get("sql")
        if not isinstance(sql, str):
            self._write({
                "id": ident,
                "ok": False,
                "error": {
                    "code": "invalid_request",
                    "message": "the request has no sql field",
                },
            })
            return True

        mode = request.get("search_mode")
        if mode not in ("exact", "ann"):
            mode = None

        try:
            result = self.runner(sql, mode)
        except Exception as exc:                   # pragma: no cover
            import traceback
            traceback.print_exc(file=sys.stderr)
            result = Result.error(1105, str(exc))

        if result is None:
            result = Result.ok()

        if getattr(result, "is_error", False):
            self._write({
                "id": ident,
                "ok": False,
                "error": {
                    "code": error_name(result.error_code),
                    "message": result.message or "statement failed",
                },
            })
            return True

        payload = {"id": ident, "ok": True}
        if result.columns:
            payload["columns"] = list(result.columns)
            payload["rows"] = [
                [_json_value(v) for v in row] for row in result.rows
            ]
        elif result.affected_rows is not None:
            payload["affected_rows"] = result.affected_rows
        self._write(payload)
        return True


def _json_value(value):
    """Convert a stored value into a JSON-safe one.

    BIGINT values must stay JSON integers: converting them through a float
    would lose precision beyond 2**53, and the judge compares the JSON
    type as well as the value.
    """
    if isinstance(value, tuple):
        return [_json_value(v) for v in value]
    if isinstance(value, list):
        return [_json_value(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # A non-finite float is not a legal JSON number.
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if value is None:
        return None
    return str(value)
