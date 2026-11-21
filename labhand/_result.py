# -*- coding: utf-8 -*-
"""VMResult, the plain-data result every labhand tool returns, and set_matcher."""
import functools
import inspect
import re
import traceback
from dataclasses import dataclass, field

SLICE = 4000
MATCH_LABEL = "MATCH"

# By default nothing matches; a caller injects its own matcher with set_matcher().
_MATCHER = [lambda text: None]


def set_matcher(fn):
    """Teach labhand once what a caller-defined "win string" looks like: `fn(text)` returns
    the matched string or None; tools then put it in VMResult.match. The library itself
    has no notion of one."""
    _MATCHER[0] = fn


def _find_match(text):
    return _MATCHER[0](text or "")


@dataclass
class VMResult:
    """What every labhand tool returns. Field names are a stable contract: callers read them
    directly.

    ok       did the call succeed
    summary  one short line (headline)
    detail   the raw output (large output spills to a file on the host)
    known    measured facts worth keeping
    match    what the matcher injected with set_matcher() found in the raw output, else None
             (the library itself has no notion of what to look for)
    info     the text actually shown
    image    {media_type, data} for picture tools, else None
    waiting  the step is legitimately waiting on a running job

    `flag` is the deprecated, pre-rename name of `match`: VMResult(..., flag=x) and r.flag
    still work (beacon and the research harness read it) and mean exactly `match`.
    """
    ok: bool
    summary: str
    detail: str = ""
    known: list = field(default_factory=list)
    match: str = None
    info: str = None
    image: dict = None
    waiting: bool = False


_dataclass_init = VMResult.__init__


def _init(self, *args, flag=None, **kwargs):
    _dataclass_init(self, *args, **kwargs)
    if flag is not None and self.match is None:
        self.match = flag


_init.__doc__ = _dataclass_init.__doc__
VMResult.__init__ = _init
VMResult.flag = property(lambda self: self.match,
                         lambda self, value: setattr(self, "match", value),
                         doc="Deprecated alias of `match`.")


# --- the public boundary: a failure is a VMResult, never an exception ----------------
_TOOL_MARK = "__labhand_tool__"
_DETAIL_TAIL = 4000
_TB_FRAMES = 8


def _host_label(name, sig, args, kwargs):
    """The host a failed call was aimed at, for its summary: the `host` argument, else the
    default host the tool would have picked (win_*: first windows host, linux_*: first linux
    host, others: first host). Never raises: this runs while reporting a failure."""
    try:
        host = sig.bind_partial(*args, **kwargs).arguments.get("host")
    except Exception:  # noqa: BLE001
        host = None
    if host:
        return str(host)
    os_name = "windows" if name.startswith("win_") else "linux" if name.startswith("linux_") else None
    try:
        from . import hosts
        return hosts.default(os_name)
    except Exception:  # noqa: BLE001
        return "the default host"


def _traceback_tail(e):
    """The innermost frames of `e`'s traceback (where it was raised: connect, sftp, ...),
    header kept, at most _DETAIL_TAIL chars cut at a line boundary."""
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__, limit=-_TB_FRAMES))
    if len(tb) > _DETAIL_TAIL:
        tb = "...\n" + tb[-_DETAIL_TAIL:].split("\n", 1)[-1]
    return tb


def public_tool(fn):
    """Decorator for every public labhand tool (what the MCP server publishes, and the same
    function called as a library). An exception that escapes the tool - the host is
    unreachable, the SSH/SFTP session broke (OSError incl. socket timeouts, paramiko errors,
    EOFError), or any other unexpected `Exception` - comes back as
    VMResult(ok=False, summary="<tool> on <host>: <ExcClass>: <message>",
    detail=<traceback tail>) instead of raising. `BaseException`s that are not `Exception`
    (KeyboardInterrupt, SystemExit, GeneratorExit) still propagate. Whatever the tool
    returns (a VMResult with ok=False, `waiting` or `image` included) is passed through
    untouched. The pooled-SSH retry ladder in remote.py runs first, inside the tool: a blip
    is still retried before it becomes a failure here. Idempotent: an already wrapped
    function is returned as is."""
    if getattr(fn, _TOOL_MARK, False):
        return fn
    name = fn.__name__
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - the public boundary: a failure is a result
            msg = " ".join(str(e).split())[:300]
            host = _host_label(name, sig, args, kwargs)
            return VMResult(False, f"{name} on {host}: {e.__class__.__name__}" + (f": {msg}" if msg else ""),
                            detail=_traceback_tail(e),
                            known=[f"{name} on {host} raised {e.__class__.__name__}"])

    setattr(wrapper, _TOOL_MARK, True)
    return wrapper


# _spill / _save_to_host live in output.py; imported lazily to avoid an import cycle.
def _spill(*a, **k):
    from .output import _spill as f
    return f(*a, **k)

def _save_to_host(*a, **k):
    from .output import _save_to_host as f
    return f(*a, **k)
