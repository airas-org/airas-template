"""run の観測（airas-org/airas #1092 / #1094）。

Makefile が PYTHONPATH で読ませるので agent のコードより先に入り、`make run` が
起動した全 Python プロセスで動く。宣言した component の呼び出し引数、読み込んだ
上流モジュールのハッシュ、到達したファイル・接続先・子プロセスを記録し、終了時に
AIRAS_OBSERVE_DIR/<pid>.json へ書く。Makefile がそれらを observed.json に結合する。
判断はしない。宣言との照合は gate の仕事。
"""

import atexit
import hashlib
import itertools
import json
import os
import sys
import threading
import time
import types

_OUT_DIR = os.environ.get("AIRAS_OBSERVE_DIR")
_SELF = os.path.abspath(__file__)
_AGENT = os.path.join(os.getcwd(), "src") + os.sep
_PACKAGES = {p for p in os.environ.get("AIRAS_OBSERVE_PACKAGES", "").split(",") if p}
_COMPONENTS = {
    c for c in os.environ.get("AIRAS_OBSERVE_COMPONENTS", "").split(",") if c
}
_NAMES = {c.rsplit(".", 1)[-1] for c in _COMPONENTS}
_GENERATOR = 0x20 | 0x80 | 0x200  # CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR

_watched: dict[types.CodeType, str] = {}
_first_lasti: dict[types.CodeType, int] = {}
_active: dict[int, dict] = {}
_seq = itertools.count()
_calls: list[dict] = []
_opens: dict[str, dict] = {}
_opens_other: dict[str, int] = {}
_connects: dict[str, dict] = {}
_lookups: dict[str, int] = {}
_spawns: list[dict] = []
_env_changes: list[dict] = []
_tamper: list[dict] = []
_dlopen: list[dict] = []
_errors: list[str] = []
_started = time.time()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return _sha(f.read())
    except OSError:
        return None


def _val(v):
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return (
            v
            if len(v) <= 200
            else {"type": "str", "len": len(v), "sha256": _sha(v.encode())}
        )
    try:
        r = repr(v)
    except Exception:
        r = "<unrepr>"
    if len(r) <= 200:
        return {"type": type(v).__name__, "repr": r}
    return {"type": type(v).__name__, "len": len(r), "sha256": _sha(r.encode())}


def _where():
    """イベントを起こした Python フレーム（caller）と、その上にある agent のコード（src/）。
    起こしたのがこのファイル自身なら caller は "self"。"""
    caller = agent = None
    try:
        f = sys._getframe(2)
    except ValueError:  # 起動直後でまだ Python のフレームが無い
        return None, None
    if f.f_code.co_filename == _SELF:
        return "self", None
    while f is not None:
        fn = f.f_code.co_filename
        if caller is None:
            caller = f"{fn}:{f.f_lineno}"
        if fn.startswith(_AGENT):
            agent = f"{fn}:{f.f_lineno}"
            break
        f = f.f_back
    return caller, agent


def _profile(frame, event, arg):
    if event[1] == "_":  # c_call / c_return / c_exception は見ない
        return
    try:
        _profile_event(frame, event, arg)
    except Exception as e:  # 観測の不具合で run を止めない
        if len(_errors) < 100:
            _errors.append(f"profile {event}: {e!r}")


def _profile_event(frame, event, arg):
    code = frame.f_code
    if event == "call":
        name = _watched.get(code)
        if name is None:
            if code.co_name not in _NAMES:
                return
            qualname = getattr(code, "co_qualname", code.co_name)
            full = f"{frame.f_globals.get('__name__', '')}.{qualname}"
            if full not in _COMPONENTS:
                return
            _watched[code] = name = full
        if code.co_flags & _GENERATOR:
            # ジェネレータは再開のたびに call が来る。最小の f_lasti が初回の入口
            first = _first_lasti.get(code)
            if first is None or frame.f_lasti < first:
                _first_lasti[code] = first = frame.f_lasti
            if frame.f_lasti > first:
                return
        n = code.co_argcount + code.co_kwonlyargcount
        names = list(code.co_varnames[:n])
        if code.co_flags & 0x04:
            names.append(code.co_varnames[n])
            n += 1
        if code.co_flags & 0x08:
            names.append(code.co_varnames[n])
        loc = frame.f_locals
        rec = {
            "seq": next(_seq),
            "fn": name,
            "pid": os.getpid(),
            "thread": threading.get_ident(),
            "args": {k: _val(loc[k]) for k in names if k in loc and k != "self"},
        }
        _calls.append(rec)
        _active[id(frame)] = rec
    elif event == "return" and code in _watched:
        rec = _active.pop(id(frame), None)
        if rec is not None:
            rec["ret"] = _val(arg)


def _hooked(args, env) -> bool:
    """子プロセスにもこの hook が入るか（PYTHONPATH を引き継ぎ、-I/-S/-E で site を切っていない）"""
    env = os.environ if env is None else env
    if (
        os.path.dirname(_SELF) not in str(env.get("PYTHONPATH", ""))
        or "AIRAS_OBSERVE_DIR" not in env
    ):
        return False
    argv = [str(a) for a in (args or [])]
    return not (
        argv
        and "python" in os.path.basename(argv[0])
        and {"-I", "-S", "-E"} & set(argv[1:4])
    )


def _audit(event, args):
    try:
        _audit_event(event, args)
    except Exception as e:  # 観測の不具合で run を止めない
        if len(_errors) < 100:
            _errors.append(f"audit {event}: {e!r}")


def _audit_event(event, args):
    if event == "open":
        caller, agent = _where()
        # import 時の open と fd の open は数だけ
        if (
            agent is None
            or isinstance(args[0], int)
            or (caller or "").startswith("<frozen importlib")
        ):
            _opens_other[caller or "?"] = _opens_other.get(caller or "?", 0) + 1
            return
        key = f"{args[0]}"
        rec = _opens.setdefault(key, {"mode": str(args[1]), "agent": agent, "n": 0})
        rec["n"] += 1
    elif event == "socket.connect":
        caller, agent = _where()
        key = str(args[1])
        rec = _connects.setdefault(key, {"caller": caller, "agent": agent, "n": 0})
        rec["n"] += 1
    elif event == "socket.getaddrinfo":
        host = str(args[0])
        _lookups[host] = _lookups.get(host, 0) + 1
    elif event in ("subprocess.Popen", "os.exec", "os.posix_spawn"):
        argv = args[1] if event == "subprocess.Popen" else args[1]
        env = args[3] if event == "subprocess.Popen" else args[2]
        caller, agent = _where()
        _spawns.append(
            {
                "event": event,
                "argv": [str(a) for a in (argv or [])][:50],
                "hooked": _hooked(argv, env),
                "caller": caller,
                "agent": agent,
            }
        )
    elif event in ("os.putenv", "os.unsetenv"):
        _env_changes.append({"event": event, "name": os.fsdecode(args[0])})
    elif event in ("sys.setprofile", "sys.settrace", "sys.addaudithook"):
        caller, agent = _where()
        if caller == "self" or (caller and os.sep + "threading.py:" in caller):
            return
        _tamper.append({"event": event, "caller": caller, "agent": agent})
    elif event == "ctypes.dlopen":
        caller, agent = _where()
        _dlopen.append({"name": str(args[0]), "caller": caller, "agent": agent})


def _modules():
    mods, syms = {}, {}
    for name, mod in list(sys.modules.items()):
        if name.split(".")[0] not in _PACKAGES or not getattr(mod, "__file__", None):
            continue
        entry = {"file": mod.__file__, "sha256": _file_sha(mod.__file__)}
        cached = getattr(mod, "__cached__", None)
        if cached and os.path.exists(cached):
            entry["cached"] = {"file": cached, "sha256": _file_sha(cached)}
        mods[name] = entry
        table = {}
        for attr, obj in list(vars(mod).items()):
            if attr.startswith("__"):
                continue
            if isinstance(obj, types.FunctionType):
                table[attr] = {
                    "module": obj.__module__,
                    "file": obj.__code__.co_filename,
                }
            elif isinstance(obj, type):
                table[attr] = {"module": obj.__module__}
        syms[name] = table
    return mods, syms


def _reset_after_fork():
    for c in (_calls, _spawns, _env_changes, _tamper, _dlopen, _errors):
        c.clear()
    for d in (_active, _opens, _opens_other, _connects, _lookups):
        d.clear()


def _finish():
    mods, syms = _modules()
    out = {
        "hook": {
            "sha256": _file_sha(_SELF),
            "packages": sorted(_PACKAGES),
            "components": sorted(_COMPONENTS),
        },
        "process": {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "argv": sys.argv,
            "cwd": os.getcwd(),
            "python": sys.version.split()[0],
            "env_names": sorted(os.environ),
            "started": _started,
            "ended": time.time(),
        },
        "modules": mods,
        "symbols": syms,
        "calls": _calls,
        "reaches": {
            "opens": _opens,
            "opens_other": _opens_other,
            "connects": _connects,
            "getaddrinfo": _lookups,
            "spawns": _spawns,
            "env_changes": _env_changes,
            "dlopen": _dlopen,
            "tamper": _tamper,
        },
        "errors": _errors,
    }
    path = os.path.join(_OUT_DIR, f"{os.getpid()}-{int(_started * 1000)}.json")
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False, default=str)


if _OUT_DIR:
    os.makedirs(_OUT_DIR, exist_ok=True)
    sys.addaudithook(_audit)
    sys.setprofile(_profile)
    threading.setprofile(_profile)
    os.register_at_fork(after_in_child=_reset_after_fork)
    atexit.register(_finish)
