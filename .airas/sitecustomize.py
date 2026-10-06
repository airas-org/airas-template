"""`make run` が起動した Python プロセスの実行記録。

Makefile が PYTHONPATH にこのディレクトリを足すので、Python はどのコードより先に
このファイルを import する。AIRAS_OBSERVE_DIR が無ければ何もしない。

終了時に AIRAS_OBSERVE_DIR/<pid>-<開始時刻>.json へ書くもの:
- calls:   AIRAS_OBSERVE_INTEGRATION（run の design の repository_integration から:
           走らせるリポジトリの method_entry と、値を宣言した各 argument の関数。Makefile が
           `sitecustomize.py integration <run_id>` で引く）の関数の呼び出し。
           実際に束縛された引数（省略した既定値を含む）と戻り値。
           値は平文（200 文字超は型・長さ・sha256）。ただし秘密の値を含む文字列は
           `{"redacted": <環境変数名>, "len": n}` に置き換える。秘密の値は、基盤が
           AIRAS_SECRET_NAMES で渡す名前（Actions secrets の一覧。ローカルでは
           ~/.airas/credentials.json のキー）の環境変数から集める
- loaded_file_hashes: import された上流パッケージ（method_entry のパッケージ）の各ファイルの
           sha256。record のスナップショットと比べ、原本のまま走ったかを見る
- loaded_definitions: 上流の各クラス・関数・メソッドの定義元。monkeypatch は定義元が
           実験コード（src/）になり、exec で作ったものは "<string>" になる
- upstream_extensions: 実験コードで定義されたクラスのうち上流クラスを継承するもの。
           基底と、基底にもあるメソッド名（override）
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
_INTEGRATION = json.loads(os.environ.get("AIRAS_OBSERVE_INTEGRATION") or "{}")
_ENTRY = _INTEGRATION.get("method_entry", "")
# argument は module.Class.method.arg なので、最後の arg を落とした関数を観測する
_COMPONENTS = {
    _ENTRY,
    *(a.rsplit(".", 1)[0] for a in _INTEGRATION.get("arguments", [])),
} - {""}
_PACKAGES = {_ENTRY.split(".")[0]} if _ENTRY else set()
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
    # AIRAS_SECRET_NAMES は名前の一覧であって値ではない
    return (names | {n for n in os.environ if _SECRET_NAME.search(n)}) - {
        "AIRAS_SECRET_NAMES"
    }


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
            # 実験コードが起点の open だけ。import 時、fd、Python 本体配下は依存の内部なので見ない
            if (
                code is None
                or isinstance(args[0], int)
                or (caller or "").startswith("<frozen importlib")
                or os.path.abspath(str(args[0])).startswith(sys.prefix + os.sep)
            ):
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
    for d in (_active, _opens, _connects, _lookups):
        d.clear()


def _origin(fn) -> dict:
    return {"module": fn.__module__, "file": fn.__code__.co_filename}


def _ours(module: str | None, file: str | None) -> bool:
    """監視 package で定義されたもの、または実験コード（差し替え）で定義されたものか。
    import してきた stdlib や他 package の名前は記録しない。exec で作った関数は
    module が None"""
    return (
        (module or "").split(".")[0] in _PACKAGES
        or module == "__main__"
        or bool(file and file.startswith(_EXPERIMENT_CODE))
    )


def _upstream_extensions() -> dict:
    """実験コード（src/ と __main__）で定義されたクラスのうち、上流クラスを継承するもの"""
    found = {}
    for name, mod in list(sys.modules.items()):
        file = getattr(mod, "__file__", None)
        if not (name == "__main__" or (file and file.startswith(_EXPERIMENT_CODE))):
            continue
        for attr, obj in list(vars(mod).items()):
            if not (isinstance(obj, type) and obj.__module__ == name):
                continue
            bases = [b for b in obj.__mro__[1:] if b.__module__.split(".")[0] in _PACKAGES]
            if bases:
                found[f"{name}.{attr}"] = {
                    "bases": [f"{b.__module__}.{b.__qualname__}" for b in bases],
                    # 基底にもある関数メンバーだけ。ABC が置く _abc_impl などの属性は数えない
                    "overrides": [
                        m
                        for m, v in vars(obj).items()
                        if not m.startswith("__")
                        and isinstance(v, (types.FunctionType, staticmethod, classmethod))
                        and any(m in vars(b) for b in bases)
                    ],
                }
    return found


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
            try:
                owner = getattr(obj, "__module__", None)
                # package 内の別モジュールで定義されたものの再 export は、定義元で記録する
                if owner != name and (owner or "").split(".")[0] in _PACKAGES:
                    continue
                if isinstance(obj, types.FunctionType):
                    # exec で作った関数（定義元 "<string>"）は出自不明なので残す。stdlib の "<frozen …>" は対象外
                    if _ours(obj.__module__, obj.__code__.co_filename) or obj.__code__.co_filename.startswith("<string>"):
                        table[attr] = _origin(obj)
                elif isinstance(obj, type):
                    owner = sys.modules.get(obj.__module__)
                    if _ours(obj.__module__, getattr(owner, "__file__", None)):
                        table[attr] = {"module": obj.__module__}
                    # import したクラスでも、実験コードで差し替えたメソッドは残す。
                    # dataclass 等が生成した dunder（co_filename "<string>"）は記録しないが、
                    # 通常名のメソッドが "<string>" なら exec による差し替えの疑いがあるので残す
                    for member, value in list(vars(obj).items()):
                        if isinstance(value, (staticmethod, classmethod)):
                            value = value.__func__
                        if not isinstance(value, types.FunctionType):
                            continue
                        generated = value.__code__.co_filename.startswith("<string>")
                        if generated and member.startswith("__"):
                            continue
                        if generated or _ours(value.__module__, value.__code__.co_filename):
                            table[f"{attr}.{member}"] = _origin(value)
            except Exception as e:  # 1 つの属性の不具合で記録全体を失わない
                if len(_errors) < 100:
                    _errors.append(f"definitions {name}.{attr}: {e!r}")
        syms[name] = table
    out = {
        "hook": {
            "sha256": _file_sha(_SELF)
        },  # 誰が観察したか。宣言は record
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
        "loaded_file_hashes": mods,
        "loaded_definitions": syms,
        "upstream_extensions": _upstream_extensions(),
        "calls": _calls,
        "reaches": {
            "opens": _opens,
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
        json.dump(out, f, ensure_ascii=False, default=str, indent=1)


def install() -> None:
    """import 時（Python が sitecustomize として読んだとき）: フックを入れる"""
    os.makedirs(_OUT_DIR, exist_ok=True)
    sys.addaudithook(_audit)
    sys.setprofile(_profile)
    threading.setprofile(_profile)
    os.register_at_fork(after_in_child=_reset_after_fork)
    atexit.register(_finish)


def integration(run_id: str) -> dict:
    """run_id の design の repository_integration から、フックが観測するもの: 走らせる
    リポジトリ（s1.r1）の method_entry と各 argument。凍結前は design.json（id は並び順
    s1, s2, … / r1, r2, …）、凍結後は record。record は追記式なので最後の宣言が生きる"""
    found = {}
    for path in (".research/design.json", ".research/record.json"):
        if not os.path.exists(path):
            continue
        doc = json.load(open(path))
        repositories = {}
        for i, s in enumerate(doc.get("literature", [])):
            sid = s.get("id", f"s{i + 1}")
            for j, r in enumerate(s.get("repositories", [])):
                repositories[r.get("id", f"{sid}.r{j + 1}")] = r
        for h in doc.get("hypotheses", []):
            for c in h.get("claims", []):
                for d in c.get("designs", []):
                    if not any(r.get("run_id") == run_id for r in d.get("runs", [])):
                        continue
                    integration = d.get("repository_integration")
                    if not integration:  # 最新の宣言に統合が無ければ観測対象なし
                        found = {}
                        continue
                    repository = repositories.get(integration.get("repository_id"), {})
                    found = {
                        "method_entry": repository.get("method_entry", ""),
                        "arguments": [a["argument"] for a in integration.get("arguments", [])],
                    }
    return found


def merge(d: str, run_id: str, out: str) -> None:
    """プロセスごとの記録を observed.json に結合する。全プロセスで同じ節
    （hook / loaded_* / upstream_extensions / process.env）は上位に 1 回だけ書き、各プロセスからは外す"""
    import glob

    processes = [json.load(open(f)) for f in sorted(glob.glob(d + "/*.json"))]
    shared = {}
    for key in ("hook", "loaded_file_hashes", "loaded_definitions", "upstream_extensions"):
        values = [p[key] for p in processes if p.get(key)]
        if values and all(v == values[0] for v in values):
            shared[key] = values[0]
            for p in processes:
                p.pop(key, None)
    envs = [p["process"]["env"] for p in processes]
    if envs and all(e == envs[0] for e in envs):
        shared["env"] = envs[0]
        for p in processes:
            p["process"].pop("env")
    json.dump(
        {"version": 1, "run_id": run_id, **shared, "processes": processes},
        open(out, "w"),
        ensure_ascii=False,
        indent=1,
    )


if __name__ == "__main__":
    if sys.argv[1] == "integration":  # python3 sitecustomize.py integration <run_id>
        print(json.dumps(integration(sys.argv[2])))
    else:  # python3 sitecustomize.py merge <dir> <run_id> <out>
        merge(*sys.argv[2:])
elif _OUT_DIR:
    install()
