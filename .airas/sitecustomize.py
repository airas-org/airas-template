"""`make run` が起動した Python プロセスの実行記録。

Makefile が PYTHONPATH にこのディレクトリを足すので、Python はどのコードより先に
このファイルを import する。AIRAS_OBSERVE_DIR が無ければ何もしない。

終了時に AIRAS_OBSERVE_DIR/<pid>-<開始時刻>.json へ書くもの:
- calls:   AIRAS_OBSERVE_COMPONENTS（module.Class.method のカンマ区切り）の関数の
           呼び出し。実際に束縛された引数（省略した既定値を含む）と戻り値。
           値は平文（200 文字超は型・長さ・sha256）。ただし秘密の値を含む文字列は
           `{"redacted": <環境変数名>, "len": n}` に置き換える。秘密の値は、基盤が
           AIRAS_SECRET_NAMES で渡す名前（Actions secrets の一覧。ローカルでは
           ~/.airas/credentials.json のキー）の環境変数から集める
- modules: AIRAS_OBSERVE_PACKAGES（カンマ区切り）の各モジュールのファイル sha256
- symbols: そのモジュールの関数・クラス・メソッドの定義元。monkeypatch は定義元が
           実験コード（src/）になる
- reaches: open、connect、名前解決、子プロセス起動、環境変数の変更、このフックを
           外す操作。それぞれ起こした場所と、その上にある実験コードの場所付き
- process: argv、Python 版、起動時の環境変数（値は引数と同じ規則）

判断はしない。Makefile がプロセス分を observed.json に結合し、gate が record の
宣言と照合する。
"""

import atexit
import hashlib
import itertools
import json
import os
import re
import sys
import threading
import time
import types

_OUT_DIR = os.environ.get("AIRAS_OBSERVE_DIR")
_SELF = os.path.abspath(__file__)
_EXPERIMENT_CODE = os.path.join(os.getcwd(), "src") + os.sep
_PACKAGES = {p for p in os.environ.get("AIRAS_OBSERVE_PACKAGES", "").split(",") if p}
_COMPONENTS = {
    c for c in os.environ.get("AIRAS_OBSERVE_COMPONENTS", "").split(",") if c
}
_NAMES = {c.rsplit(".", 1)[-1] for c in _COMPONENTS}
_GENERATOR = 0x20 | 0x80 | 0x200  # CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR
_SECRET_NAME = re.compile(
    r"key|token|secret|passw|credential|auth|private|cookie|session", re.IGNORECASE
)


def _secret_names() -> set[str]:
    """伏せる環境変数の名前。基盤が渡す AIRAS_SECRET_NAMES（Actions secrets の名前一覧）、
    無ければローカルの ~/.airas/credentials.json のキー。名前の規則は足し忘れの保険"""
    names = {n for n in os.environ.get("AIRAS_SECRET_NAMES", "").split(",") if n}
    if not names:
        try:
            with open(os.path.expanduser("~/.airas/credentials.json")) as f:
                names = set(json.load(f))
        except (OSError, ValueError):
            pass
    return names | {n for n in os.environ if _SECRET_NAME.search(n)}


_SECRET_NAMES = _secret_names()
# 伏せる値 → 名前。8 文字未満は誤爆するので対象外
_SECRET_VALUES = {
    os.environ[n]: n for n in _SECRET_NAMES if len(os.environ.get(n, "")) >= 8
}

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


def _to_json_value(v, name=""):
    """name は引数名か環境変数名。秘密の名前の値と、秘密の値を含む文字列は伏せる"""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    try:
        r = v if isinstance(v, str) else repr(v)
    except Exception:
        r = "<unrepr>"
    secret = name if name in _SECRET_NAMES else None
    if secret is None:
        secret = next((n for s, n in _SECRET_VALUES.items() if s in r), None)
    if secret is not None:
        return {"redacted": secret, "len": len(r)}
    if isinstance(v, str):
        if len(v) <= 200:
            return v
        return {"type": "str", "len": len(v), "sha256": _sha(v.encode())}
    if len(r) <= 200:
        return {"type": type(v).__name__, "repr": r}
    return {"type": type(v).__name__, "len": len(r), "sha256": _sha(r.encode())}


def _where():
    """イベントを起こした Python の場所（caller）と、その上にある実験コードの場所。
    このファイルの hook 関数の分だけ上に辿る。起こしたのがこのファイル自身なら "self"。"""
    f = sys._getframe(0)
    while f is not None and f.f_code in _HOOK_CODES:
        f = f.f_back
    if f is None:
        return None, None
    if f.f_code.co_filename == _SELF:
        return "self", None
    caller = f"{f.f_code.co_filename}:{f.f_lineno}"
    while f is not None:
        if f.f_code.co_filename.startswith(_EXPERIMENT_CODE):
            return caller, f"{f.f_code.co_filename}:{f.f_lineno}"
        f = f.f_back
    return caller, None


def _profile(frame, event, arg):
    # 関数の call / return を受け、監視対象の component なら引数と戻り値を _calls に積む
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
            generator = code.co_flags & _GENERATOR
            if generator:
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
                "args": {
                    k: _to_json_value(loc[k], k)
                    for k in names
                    if k in loc and k != "self"
                },
            }
            _calls.append(rec)
            if not generator:  # yield でも return が来るので戻り値は取らない
                _active[id(frame)] = rec
        elif event == "return" and code in _watched:
            rec = _active.pop(id(frame), None)
            if rec is not None:
                rec["ret"] = _to_json_value(arg)
    except Exception as e:  # 観測の不具合で run を止めない
        if len(_errors) < 100:
            _errors.append(f"profile {event}: {e!r}")


def _audit(event, args):
    # audit イベントを受け、_where() で発生源を特定して reaches の各変数に積む
    try:
        if event == "open":
            caller, code = _where()
            # import 時の open と fd の open は数だけ
            if (
                code is None
                or isinstance(args[0], int)
                or (caller or "").startswith("<frozen importlib")
            ):
                _opens_other[caller or "?"] = _opens_other.get(caller or "?", 0) + 1
                return
            rec = _opens.setdefault(
                f"{args[0]}", {"modes": {}, "experiment_code": code}
            )
            mode = str(args[1])
            rec["modes"][mode] = rec["modes"].get(mode, 0) + 1
        elif event == "socket.connect":
            caller, code = _where()
            rec = _connects.setdefault(
                str(args[1]), {"caller": caller, "experiment_code": code, "n": 0}
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
            )
            if hooked and argv and "python" in os.path.basename(argv[0]):
                for a in argv[1:]:
                    if not a.startswith("-"):
                        break
                    if not a.startswith("--") and set(a[1:]) & {"I", "S", "E"}:
                        hooked = False
                    if a[:2] in ("-c", "-m"):
                        break
            caller, code = _where()
            _spawns.append(
                {
                    "event": event,
                    "argv": argv[:50],
                    "hooked": hooked,
                    "caller": caller,
                    "experiment_code": code,
                }
            )
            if event == "os.exec":  # 成功すると atexit が走らないので今書く
                _finish()
        elif event in ("os.putenv", "os.unsetenv"):
            _env_changes.append({"event": event, "name": os.fsdecode(args[0])})
        elif event in ("sys.setprofile", "sys.settrace", "sys.addaudithook"):
            caller, code = _where()
            if caller == "self" or (caller and os.sep + "threading.py:" in caller):
                return
            _tamper.append({"event": event, "caller": caller, "experiment_code": code})
    except Exception as e:  # 観測の不具合で run を止めない
        if len(_errors) < 100:
            _errors.append(f"audit {event}: {e!r}")


_HOOK_CODES = {_where.__code__, _profile.__code__, _audit.__code__}


def _reset_after_fork():
    for c in (_calls, _spawns, _env_changes, _tamper, _errors):
        c.clear()
    for d in (_active, _opens, _opens_other, _connects, _lookups):
        d.clear()


def _origin(fn) -> dict:
    return {"module": fn.__module__, "file": fn.__code__.co_filename}


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
                table[attr] = _origin(obj)
            elif isinstance(obj, type):
                table[attr] = {"module": obj.__module__}
                for member, value in list(vars(obj).items()):
                    if isinstance(value, (staticmethod, classmethod)):
                        value = value.__func__
                    if isinstance(value, types.FunctionType):
                        table[f"{attr}.{member}"] = _origin(value)
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
            "env": {k: _to_json_value(v, k) for k, v in sorted(os.environ.items())},
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
