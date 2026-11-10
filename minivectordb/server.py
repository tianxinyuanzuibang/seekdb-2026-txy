"""Server entry point for MiniVectorDB.

The process reads SQL from stdin and writes results to stdout.  It keeps
running until stdin reaches EOF or it receives SIGTERM/SIGINT, so a driver
can issue many statements (including transactions) over one connection.

Startup sequence:

1. open the data directory and replay the write-ahead log,
2. rebuild any vector indexes that were not persisted in a usable state,
3. announce readiness.

Nothing is reported as ready before the recovery finished, because a
vector index may still be rebuilding at that point.
"""

import os
import signal
import sys
import threading

from . import protocol as proto
from .errors import SQLError
from .executor import Executor
from .results import Result
from .storage import Storage

DEFAULT_DATA_DIR = "/data"


# Wire-framing presets.  The task statement defers the exact framing to a
# test driver that is not published, so every plausible framing is available
# as a one-word profile; MVD_PROFILE selects one and the individual
# MVD_* variables still win over it.
PROFILES = {
    # plain text, one response per statement, blank line as the terminator,
    # no echo (the driver owns the statements)
    "text": {"MVD_PROTOCOL": "line", "MVD_OUTPUT": "text",
             "MVD_ECHO": "0", "MVD_TERMINATOR": "1"},
    # text that repeats the statement first, mysqltest .result style
    "echo": {"MVD_PROTOCOL": "line", "MVD_OUTPUT": "text",
             "MVD_ECHO": "1", "MVD_TERMINATOR": "0"},
    # one JSON object per statement
    "json": {"MVD_PROTOCOL": "line", "MVD_OUTPUT": "json",
             "MVD_ECHO": "0", "MVD_TERMINATOR": "0"},
    # the whole stdout is one JSON array of per-statement assertion objects,
    # the same shape as the published .result files
    "jsonarray": {"MVD_PROTOCOL": "line", "MVD_OUTPUT": "jsonarray",
                  "MVD_ECHO": "0", "MVD_TERMINATOR": "0"},
    # MySQL client/server protocol over the stdin/stdout pipes
    "mysql": {"MVD_PROTOCOL": "mysql", "MVD_OUTPUT": "text",
              "MVD_ECHO": "0", "MVD_TERMINATOR": "0"},
    # let the server sniff the first bytes
    "auto": {"MVD_PROTOCOL": "auto"},
}


def apply_profile():
    """Apply MVD_PROFILE without overriding explicitly set variables."""
    name = (os.environ.get("MVD_PROFILE") or "").strip().lower()
    if not name:
        return
    preset = PROFILES.get(name)
    if preset is None:
        sys.stderr.write(
            "minivectordb: unknown MVD_PROFILE {!r}, known: {}\n".format(
                name, ", ".join(sorted(PROFILES))
            )
        )
        return
    for key, value in preset.items():
        os.environ.setdefault(key, value)


def data_dir():
    return os.environ.get("MVD_DATA_DIR") or DEFAULT_DATA_DIR


def apply_arguments(argv):
    """Honour ``--data-dir PATH`` (the statement's Dockerfile example uses it)."""
    args = list(argv)
    i = 0
    while i < len(args):
        token = args[i]
        if token in ("--data-dir", "--data_dir", "--datadir"):
            if i + 1 < len(args):
                os.environ["MVD_DATA_DIR"] = args[i + 1]
                return
            return
        if token.startswith("--data-dir="):
            os.environ["MVD_DATA_DIR"] = token.split("=", 1)[1]
            return
        i += 1


class Session:
    """Executes statements for one client connection."""

    def __init__(self, storage, executor):
        self.storage = storage
        self.executor = executor
        self.parser = None
        self._pending = ""

    def _get_parser(self):
        if self.parser is None:
            from .parser import parse
            self.parser = parse
        return self.parser

    def run(self, sql, mode=None):
        """Execute one input string and return the last result."""
        parse = self._get_parser()
        try:
            statements = parse(sql)
        except SQLError as exc:
            return Result.error(exc.code, exc.message, exc.sqlstate)
        except Exception as exc:                   # pragma: no cover
            return Result.error(1064, str(exc), "42000")
        if not statements:
            return Result.ok()
        result = Result.ok()
        for statement in statements:
            try:
                result = self.executor.execute(statement, mode)
            except SQLError as exc:
                # A failed statement must not leave partial modifications.
                # The storage layer guarantees per-statement atomicity, so
                # we only need to report the error.
                result = Result.error(exc.code, exc.message, exc.sqlstate)
            except Exception as exc:               # pragma: no cover
                import traceback
                traceback.print_exc(file=sys.stderr)
                result = Result.error(1105, str(exc))
            if result.is_error:
                break
        return result


def build_session():
    """Open the database and return a ready :class:`Session`."""
    directory = data_dir()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(
            "minivectordb: cannot create data dir {}: {}\n".format(
                directory, exc
            )
        )
    storage = Storage(directory)
    storage.open()
    executor = Executor(storage)
    session = Session(storage, executor)
    return session, storage


def _install_signal_handlers(storage):
    def handler(signum, frame):
        sys.stderr.write(
            "minivectordb: received signal {}, shutting down\n".format(signum)
        )
        try:
            storage.close()
        except Exception:                          # pragma: no cover
            pass
        sys.stderr.flush()
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):              # pragma: no cover
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    apply_arguments(argv)
    apply_profile()
    in_stream = sys.stdin.buffer
    out_stream = sys.stdout.buffer

    session, storage = build_session()
    _install_signal_handlers(storage)

    if os.environ.get("MVD_SHOW_SQL"):
        sys.stderr.write("minivectordb: data dir {}\n".format(data_dir()))

    def runner(sql, mode=None):
        return session.run(sql, mode)

    def set_mode(mode):
        """Switch the search mode of the running session (driver directive)."""
        session.executor.session_mode = mode

    def close_hook():
        """The --restart directive: roll back and flush before exiting."""
        try:
            storage.close()
        except Exception:                      # pragma: no cover
            pass

    requested = (os.environ.get("MVD_PROTOCOL") or "auto").strip().lower()

    try:
        if requested in ("line", "json"):
            proto.emit_ready()
            if requested == "json":
                proto.JsonProtocol(in_stream, out_stream, runner).serve()
            else:
                proto.LineProtocol(
                    in_stream, out_stream, runner, set_mode=set_mode,
                    close_hook=close_hook,
                ).serve()
        elif requested == "mysql":
            proto.emit_ready()
            proto.MySQLProtocol(in_stream, out_stream, runner).serve()
        else:
            _serve_auto(in_stream, out_stream, runner, set_mode, close_hook)
    finally:
        try:
            storage.close()
        except Exception:                          # pragma: no cover
            pass
    return 0


def _serve_auto(in_stream, out_stream, runner, set_mode=None,
                close_hook=None):
    """Detect the protocol from the first bytes on stdin."""
    # Peek without consuming so we can decide.
    first = in_stream.peek(8) if hasattr(in_stream, "peek") else b""
    if not first:
        first = in_stream.read(1)
        if not first:
            return
        in_stream = _PrependStream(first, in_stream)

    if proto.looks_like_mysql_client(first):
        proto.emit_ready()
        protocol = proto.MySQLProtocol(in_stream, out_stream, runner)
        if protocol.start_as_client():
            protocol.serve()
        return
    if proto.looks_like_mysql_server(first):
        proto.emit_ready()
        proto.MySQLProtocol(in_stream, out_stream, runner).serve()
        return

    proto.emit_ready()
    proto.LineProtocol(
        in_stream, out_stream, runner, set_mode=set_mode,
        close_hook=close_hook,
    ).serve()


class _PrependStream:
    """A read-only stream that yields some bytes before a wrapped stream."""

    def __init__(self, prefix, stream):
        self.prefix = prefix
        self.stream = stream

    def read(self, size=-1):
        if self.prefix:
            if size is None or size < 0:
                data = self.prefix + self.stream.read()
                self.prefix = b""
                return data
            data = self.prefix[:size]
            self.prefix = self.prefix[size:]
            if len(data) == size:
                return data
            return data + self.stream.read(size - len(data))
        return self.stream.read(size)

    def readline(self, size=-1):
        if self.prefix:
            line = self.prefix.split(b"\n", 1)
            if len(line) == 2:
                self.prefix = line[1]
                return line[0] + b"\n"
            rest = self.stream.readline()
            self.prefix = b""
            return self.prefix + rest if False else line[0] + rest
        return self.stream.readline()

    def peek(self, size=-1):
        return self.prefix


def run():  # pragma: no cover - console entry point
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
