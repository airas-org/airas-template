"""`make run` が起動した Python プロセスの実行記録。

Makefile が PYTHONPATH にこのディレクトリを足すので、Python はどのコードより先に
このファイルを import する。AIRAS_OBSERVE_DIR が無ければ何もしない。

終了時に AIRAS_OBSERVE_DIR/<pid>-<開始時刻>.json へ書くもの:
- calls:   AIRAS_OBSERVE_COMPONENTS（module.Class.method のカンマ区切り）の関数の
           呼び出し。実際に束縛された引数（省略した既定値を含む）と戻り値
- modules: AIRAS_OBSERVE_PACKAGES（カンマ区切り）の各モジュールのファイル sha256
- symbols: そのモジュールの関数・クラスの定義元。monkeypatch は定義元が src/ になる
- reaches: open、connect、名前解決、子プロセス起動、環境変数の変更、このフックを
           外す操作。それぞれ起こした場所（src/ 配下なら agent のコード）付き

判断はしない。Makefile がプロセス分を observed.json に結合し、gate が record の
宣言と照合する。
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
    """イベントを起こした Python の場所（caller）と、その上にある src/ の場所（agent）。
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
        code = frame.f_code
        if event == "call":
            name = _watched.get(code)
            if name is None:
                if code.co_name not in _NAMES:
                    return
                qualname = getattr(code, "co_qualname", code.co_name)
                name = f"{frame.f_globals.get('__name__', '')}.{qualname}"
                if name not in _COMPONENTS:
                    return
                _watched[code] = name
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
    except Exception as e:  # 観測の不具合で run を止めない
        if len(_errors) < 100:
            _errors.append(f"profile {event}: {e!r}")


def _audit(event, args):
    try:
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
            rec = _opens.setdefault(
                f"{args[0]}", {"mode": str(args[1]), "agent": agent, "n": 0}
            )
            rec["n"] += 1
        elif event == "socket.connect":
            caller, agent = _where()
            rec = _connects.setdefault(
                str(args[1]), {"caller": caller, "agent": agent, "n": 0}
            )
            rec["n"] += 1
        elif event == "socket.getaddrinfo":
            host = str(args[0])
            _lookups[host] = _lookups.get(host, 0) + 1
        elif event in ("subprocess.Popen", "os.exec", "os.posix_spawn"):
            argv = [str(a) for a in (args[1] or [])]
            env = args[3] if event == "subprocess.Popen" else args[2]
            env = os.environ if env is None else env
            # 子にもこのフックが入るか: PYTHONPATH を引き継ぎ、-I/-S/-E で site を切っていない
            hooked = (
                os.path.dirname(_SELF) in str(env.get("PYTHONPATH", ""))
                and "AIRAS_OBSERVE_DIR" in env
                and not (
                    argv
                    and "python" in os.path.basename(argv[0])
                    and {"-I", "-S", "-E"} & set(argv[1:4])
                )
            )
            caller, agent = _where()
            _spawns.append(
                {
                    "event": event,
                    "argv": argv[:50],
                    "hooked": hooked,
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
    except Exception as e:  # 観測の不具合で run を止めない
        if len(_errors) < 100:
            _errors.append(f"audit {event}: {e!r}")


def _reset_after_fork():
    for c in (_calls, _spawns, _env_changes, _tamper, _errors):
        c.clear()
    for d in (_active, _opens, _opens_other, _connects, _lookups):
        d.clear()


def _finish():
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
