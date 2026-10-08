"""`make run` が起動した Python プロセスの実行記録（version 2）。

Makefile が PYTHONPATH にこのディレクトリを足すので、Python はどのコードより先に
このファイルを import する。AIRAS_OBSERVE_DIR が無ければ何もしない。

終了時に AIRAS_OBSERVE_DIR/<pid>-<開始時刻>.json へ書き、Makefile が `merge` で
observed.json に結合する。

- calls:   関数ごとに 1 項目。対象は、実験コード（src/）で定義された関数、実験コードから
           直接呼ばれた依存の関数（stdlib と、依存同士の呼び出しは除く）、宣言された関数
           （AIRAS_OBSERVE_INTEGRATION: run の design の repository_integration から、走らせる
           リポジトリの method_entry と、値を宣言した各 argument の関数。Makefile が
           `sitecustomize.py integration <run_id>` で引く）。
           項目は呼び出し回数、引数ごとの「取った値 → 回数」（異なり値 50 まで、異なり数は
           1000 まで数える。数値は min/max、長さのあるものは length_min/max）、先頭 3 回の
           全引数と戻り値。宣言された関数は全呼び出しで値を記録し、それ以外は 4 回目から
           スカラーと文字列だけ値を見て、他は型と長さだけ見る。
           値は平文（200 文字超は型・長さ・sha256。メモリアドレス入りの repr は型だけ）。
           秘密の値を含む文字列は `{"redacted": <環境変数名>, "len": n}` に、鍵の形の文字列
           （sk- / ghp_ / hf_ / AKIA / JWT など）は sha256 に置き換える。秘密の値は、基盤が
           AIRAS_SECRET_NAMES で渡す名前（Actions secrets の一覧。ローカルでは
           ~/.airas/credentials.json のキー）の環境変数から集める
- src_modules: import された実験コードの各ファイルの sha256。gate が実行コミットの同じ
           ファイルと比べ、読まれたコードがコミットのものかを見る
- loaded_file_hashes: import された上流パッケージ（method_entry のパッケージ）の各ファイルの
           sha256。record のスナップショットと比べ、原本のまま走ったかを見る
- loaded_definitions: 上流の各クラス・関数・メソッドの定義元。monkeypatch は定義元が
           実験コード（src/）になり、exec で作ったものは "<string>" になる
- foreign_definitions: 上流以外の依存（scipy、pathlib、…）の名前のうち定義元が実験コードの
           もの。依存の差し替え
- upstream_extensions: 実験コードで定義されたクラスのうち上流クラスを継承するもの。
           基底と、基底にもあるメソッド名（override）
- reaches: 実験コードが起点の open（インタプリタと依存の配下は除く。一時ディレクトリは
           ディレクトリに畳む）、connect、名前解決、実験コードが起動した（または python の）
           子プロセス、実験コードによる環境変数の変更、このフックを外す操作。回数で集約
- process: argv、Python 版、起動時の環境変数（値は引数と同じ規則）

判断はしない。gate が record の宣言と照合する。
"""

import atexit
import hashlib
import json
import os
import re
import sys
import sysconfig
import tempfile
import threading
import time
import types

_OUT_DIR = os.environ.get("AIRAS_OBSERVE_DIR")
_SELF = os.path.abspath(__file__)
_CWD = os.getcwd()
_EXPERIMENT_CODE = os.path.join(_CWD, "src") + os.sep
_INTEGRATION = json.loads(os.environ.get("AIRAS_OBSERVE_INTEGRATION") or "{}")
_ENTRY = _INTEGRATION.get("method_entry", "")
# argument は module.Class.method.arg なので、最後の arg を落とした関数を観測する
_COMPONENTS = {
    _ENTRY,
    *(a.rsplit(".", 1)[0] for a in _INTEGRATION.get("arguments", [])),
} - {""}
_PACKAGES = {_ENTRY.split(".")[0]} if _ENTRY else set()
_STDLIB = tuple(
    {sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"]}
)
# 実験コード起点でも記録しない open: インタプリタと依存の配下、擬似ファイルシステム、OS のデータ
_LIBRARY = tuple(
    p.rstrip(os.sep) + os.sep
    for p in {sys.base_prefix, sys.prefix, *_STDLIB, "/proc", "/sys", "/dev", "/usr/share"}
)
_TMP = tempfile.gettempdir()
_GENERATOR = 0x20 | 0x80 | 0x200  # CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR
_SECRET_NAME = re.compile(
    r"key|token|secret|passw|credential|auth|private|cookie|session|header",
    re.IGNORECASE,
)
# ponytail: 既知の鍵の接頭辞だけ。新しいプロバイダが出たら足す
_KEY_LIKE = re.compile(r"(sk-|ghp_|gho_|github_pat_|hf_|AKIA|eyJ|xox[abp]-|AIza|glpat-)\S{10,}")
_OMITTED = ("NotGiven", "NotGivenType", "Sentinel")  # 省略の印は値ではない
_SAMPLES, _VALUES, _DISTINCT = 3, 50, 1000


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

_kind: dict[types.CodeType, str] = {}  # code → "declared" | "src" | "dep" | ""（見ない）
_first_lasti: dict[types.CodeType, int] = {}
_active: dict[int, dict] = {}  # 戻り値を待つ sample
_fns: dict[str, dict] = {}
_opens: dict[str, dict] = {}
_connects: dict[str, int] = {}
_lookups: dict[str, int] = {}
_spawns: dict[str, dict] = {}
_env_changes: dict[str, int] = {}
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


def _is_stdlib(file: str) -> bool:
    return (file.startswith(_STDLIB) or file.startswith("<")) and (
        "site-packages" not in file and "dist-packages" not in file
    )


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
        if len(v) <= 200 and not _KEY_LIKE.search(v):
            return v
        return {"type": "str", "len": len(v), "sha256": _sha(v.encode())}
    if " at 0x" in r:  # メモリアドレスは再現不能なので型だけ
        return {"type": type(v).__name__}
    if len(r) <= 200:
        return {"type": type(v).__name__, "repr": r}
    return {"type": type(v).__name__, "len": len(r), "sha256": _sha(r.encode())}


def _where():
    """イベントを起こした Python の場所（caller）、その上にある実験コードの場所、そして
    stdlib を抜けて最初に出会うのが実験コードか（実験コードが直接起こしたか）。
    このファイルの hook 関数の分だけ上に辿る。起こしたのがこのファイル自身なら "self"。"""
    f = sys._getframe(0)
    while f is not None and f.f_code in _HOOK_CODES:
        f = f.f_back
    if f is None:
        return None, None, False
    if f.f_code.co_filename == _SELF:
        return "self", None, False
    caller = f"{f.f_code.co_filename}:{f.f_lineno}"
    direct = None
    while f is not None:
        file = f.f_code.co_filename
        if direct is None and not _is_stdlib(file):
            direct = file.startswith(_EXPERIMENT_CODE)
        if file.startswith(_EXPERIMENT_CODE):
            return caller, f"{file}:{f.f_lineno}", bool(direct)
        f = f.f_back
    return caller, None, False


def _classify(frame, code) -> str:
    if code.co_name.startswith("<") or not code.co_flags & 0x02:  # lambda、内包表記、クラス本体
        return ""
    qualname = f"{frame.f_globals.get('__name__', '')}.{getattr(code, 'co_qualname', code.co_name)}"
    if qualname in _COMPONENTS:
        return "declared"
    file = code.co_filename
    if file.startswith(_EXPERIMENT_CODE):
        return "src"
    if _is_stdlib(file) or file == _SELF:
        return ""
    return "dep"


def _note(a: dict, v, name: str, full: bool) -> None:
    """引数 1 つの集計。full なら値を全部記録、そうでなければスカラーと文字列だけ"""
    a["calls"] += 1
    a["types"].add(type(v).__name__)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        a["min"] = v if "min" not in a else min(a["min"], v)
        a["max"] = v if "max" not in a else max(a["max"], v)
    elif hasattr(v, "__len__"):
        try:
            n = len(v)
            a["length_min"] = n if "length_min" not in a else min(a["length_min"], n)
            a["length_max"] = n if "length_max" not in a else max(a["length_max"], n)
        except Exception:
            pass
    if full or v is None or isinstance(v, (bool, int, float, str)):
        rec = _to_json_value(v, name)
    else:
        rec = {"type": type(v).__name__}
    key = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    if key in a["values"]:
        a["values"][key][1] += 1
    elif len(a["values"]) < _VALUES:
        a["values"][key] = [rec, 1]
    if len(a["seen"]) < _DISTINCT:
        a["seen"].add(key)


def _profile(frame, event, arg):
    # 関数の call / return を受け、対象なら引数を関数ごとに集計し、先頭 3 回は戻り値も取る
    if event[1] == "_":  # c_call / c_return / c_exception は見ない
        return
    try:
        code = frame.f_code
        if event == "call":
            kind = _kind.get(code)
            if kind is None:
                kind = _kind[code] = _classify(frame, code)
            if not kind:
                return
            if kind == "dep":  # 依存は実験コードから直接呼ばれたときだけ
                back = frame.f_back
                if back is None or not back.f_code.co_filename.startswith(_EXPERIMENT_CODE):
                    return
            generator = code.co_flags & _GENERATOR
            if generator:
                # ジェネレータは再開のたびに call が来る。最小の f_lasti が初回の入口
                first = _first_lasti.get(code)
                if first is None or frame.f_lasti < first:
                    _first_lasti[code] = first = frame.f_lasti
                if frame.f_lasti > first:
                    return
            name = f"{frame.f_globals.get('__name__', '')}.{getattr(code, 'co_qualname', code.co_name)}"
            fn = _fns.get(name)
            if fn is None:
                fn = _fns[name] = {"calls": 0, "args": {}, "samples": []}
            fn["calls"] += 1
            sampling = len(fn["samples"]) < _SAMPLES
            full = sampling or kind == "declared"
            n = code.co_argcount + code.co_kwonlyargcount
            names = list(code.co_varnames[:n])
            if code.co_flags & 0x04:
                names.append(code.co_varnames[n])
                n += 1
            if code.co_flags & 0x08:
                names.append(code.co_varnames[n])
            loc = frame.f_locals
            sample: dict = {}
            for k in names:
                if k not in loc or k == "self":
                    continue
                v = loc[k]
                if type(v).__name__ in _OMITTED:
                    continue
                a = fn["args"].get(k)
                if a is None:
                    a = fn["args"][k] = {"calls": 0, "types": set(), "values": {}, "seen": set()}
                _note(a, v, k, full)
                if sampling:
                    sample[k] = _to_json_value(v, k)
            if sampling:
                rec = {"args": sample}
                fn["samples"].append(rec)
                if not generator:  # yield でも return が来るので戻り値は取らない
                    _active[id(frame)] = rec
        elif event == "return":
            rec = _active.pop(id(frame), None)
            if rec is not None:
                rec["ret"] = _to_json_value(arg)
    except Exception as e:  # 観測の不具合で run を止めない
        if len(_errors) < 100:
            _errors.append(f"profile {event}: {e!r}")


def _audit(event, args):
    # audit イベントを受け、_where() で発生源を特定して reaches に積む
    try:
        if event == "open":
            caller, code, _ = _where()
            # 実験コードが起点の open だけ。import 時、fd、ライブラリ配下は依存の内部なので見ない
            if (
                code is None
                or isinstance(args[0], int)
                or (caller or "").startswith("<frozen importlib")
            ):
                return
            path = os.path.abspath(str(args[0]))
            if path.startswith(_LIBRARY):
                return
            if path.startswith(_CWD + os.sep):
                path = os.path.relpath(path, _CWD)
            elif path.startswith(_TMP + os.sep):
                path = _TMP  # 一時ファイルは名前がランダムなのでディレクトリに畳む
            rec = _opens.setdefault(path, {"experiment_code": code})
            mode = str(args[1])
            rec[mode] = rec.get(mode, 0) + 1
        elif event == "socket.connect":
            address = args[1]
            key = f"{address[0]}:{address[1]}" if isinstance(address, tuple) else str(address)
            _connects[key] = _connects.get(key, 0) + 1
        elif event == "socket.getaddrinfo":
            host = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
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
            python = bool(argv) and "python" in os.path.basename(argv[0])
            if hooked and python:
                for a in argv[1:]:
                    if not a.startswith("-"):
                        break
                    if not a.startswith("--") and set(a[1:]) & {"I", "S", "E"}:
                        hooked = False
                    if a[:2] in ("-c", "-m"):
                        break
            caller, code, direct = _where()
            if direct or python:  # 依存が起動する lscpu などは見ない
                key = json.dumps([event, argv[:50], hooked])
                rec = _spawns.get(key)
                if rec is None:
                    rec = _spawns[key] = {
                        "event": event,
                        "argv": argv[:50],
                        "hooked": hooked,
                        "experiment_code": code,
                        "n": 0,
                    }
                rec["n"] += 1
            if event == "os.exec":  # 成功すると atexit が走らないので今書く
                _finish()
        elif event in ("os.putenv", "os.unsetenv"):
            caller, code, direct = _where()
            if direct:  # 依存が自分の都合で触る OPENBLAS_* などは見ない
                name = os.fsdecode(args[0])
                _env_changes[name] = _env_changes.get(name, 0) + 1
        elif event in ("sys.setprofile", "sys.settrace", "sys.addaudithook"):
            caller, code, direct = _where()
            if caller == "self" or (caller and os.sep + "threading.py:" in caller):
                return
            if event == "sys.addaudithook" and not direct:  # filelock などの監査は外しではない
                return
            _tamper.append({"event": event, "caller": caller, "experiment_code": code})
    except Exception as e:  # 観測の不具合で run を止めない
        if len(_errors) < 100:
            _errors.append(f"audit {event}: {e!r}")


_HOOK_CODES = {_where.__code__, _profile.__code__, _audit.__code__}


def _reset_after_fork():
    for c in (_fns, _active, _opens, _connects, _lookups, _spawns, _env_changes):
        c.clear()
    _tamper.clear()
    _errors.clear()


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


def _definitions():
    """(src_modules, loaded_file_hashes, loaded_definitions, foreign_definitions)"""
    src_mods, mods, syms, foreign = {}, {}, {}, {}
    for name, mod in list(sys.modules.items()):
        file = getattr(mod, "__file__", None)
        if not file:
            continue
        if file.startswith(_EXPERIMENT_CODE):
            src_mods[os.path.relpath(file, _CWD)] = _file_sha(file)
            continue
        upstream = name.split(".")[0] in _PACKAGES
        if upstream:
            entry = {"file": file, "sha256": _file_sha(file)}
            cached = getattr(mod, "__cached__", None)
            if cached and os.path.exists(cached):
                entry["cached"] = {"file": cached, "sha256": _file_sha(cached)}
            mods[name] = entry
        table = {}
        for attr, obj in list(vars(mod).items()):
            if attr.startswith("__"):
                continue
            try:
                owner = getattr(obj, "__module__", None) or ""
                # package 内の別モジュールで定義されたものの再 export は、定義元で記録する
                if owner != name and owner.split(".")[0] in _PACKAGES:
                    continue
                if isinstance(obj, types.FunctionType):
                    f = obj.__code__.co_filename
                    if upstream:
                        # exec で作った関数（定義元 "<string>"）は出自不明なので残す。stdlib の "<frozen …>" は対象外
                        if _ours(obj.__module__, f) or f.startswith("<string>"):
                            table[attr] = _origin(obj)
                    elif f.startswith(_EXPERIMENT_CODE):
                        foreign[f"{name}.{attr}"] = os.path.relpath(f, _CWD)
                elif isinstance(obj, type):
                    defining = sys.modules.get(owner)
                    own = owner == name or _ours(owner, getattr(defining, "__file__", None))
                    if not own:  # import したクラスは定義元のモジュールで見る
                        continue
                    if upstream:
                        table[attr] = {"module": owner}
                    for member, value in list(vars(obj).items()):
                        if isinstance(value, (staticmethod, classmethod)):
                            value = value.__func__
                        if not isinstance(value, types.FunctionType):
                            continue
                        f = value.__code__.co_filename
                        # dataclass 等が生成した dunder（co_filename "<string>"）は記録しないが、
                        # 通常名のメソッドが "<string>" なら exec による差し替えの疑いがあるので残す
                        generated = f.startswith("<string>")
                        if generated and member.startswith("__"):
                            continue
                        if upstream:
                            if generated or _ours(value.__module__, f):
                                table[f"{attr}.{member}"] = _origin(value)
                        elif f.startswith(_EXPERIMENT_CODE):
                            foreign[f"{name}.{attr}.{member}"] = os.path.relpath(f, _CWD)
            except Exception as e:  # 1 つの属性の不具合で記録全体を失わない
                if len(_errors) < 100:
                    _errors.append(f"definitions {name}.{attr}: {e!r}")
        if upstream:
            syms[name] = table
    return src_mods, mods, syms, foreign


def _finish():
    src_mods, mods, syms, foreign = _definitions()
    calls = {}
    for name, fn in _fns.items():
        calls[name] = {
            "calls": fn["calls"],
            "args": {
                k: {
                    **{kk: vv for kk, vv in a.items() if kk not in ("types", "values", "seen")},
                    "types": sorted(a["types"]),
                    "values": list(a["values"].values()),
                    "seen": sorted(a["seen"]),
                }
                for k, a in fn["args"].items()
            },
            "samples": fn["samples"],
        }
    out = {
        "version": 2,
        "hook": {"sha256": _file_sha(_SELF)},  # 誰が観察したか。宣言は record
        "process": {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "argv": sys.argv,
            "cwd": _CWD,
            "python": sys.version.split()[0],
            "env": {k: _to_json_value(v, k) for k, v in sorted(os.environ.items())},
            "started": _started,
            "ended": time.time(),
        },
        "src_modules": src_mods,
        "loaded_file_hashes": mods,
        "loaded_definitions": syms,
        "foreign_definitions": foreign,
        "upstream_extensions": _upstream_extensions(),
        "calls": calls,
        "reaches": {
            "opens": _opens,
            "connects": _connects,
            "getaddrinfo": _lookups,
            "spawns": list(_spawns.values()),
            "env_changes": _env_changes,
            "tamper": _tamper,
        },
        "errors": _errors,
    }
    path = os.path.join(_OUT_DIR, f"{os.getpid()}-{int(_started * 1000)}.json")
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False, default=str, separators=(",", ":"))


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


def _union(dst: dict, src: dict, path: str, errors: list) -> None:
    """dict の木を結合する。同じ鍵に違う値があれば先勝ちで、errors に残す"""
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
        elif isinstance(dst[k], dict) and isinstance(v, dict):
            _union(dst[k], v, f"{path}.{k}", errors)
        elif dst[k] != v:
            errors.append(f"{path}.{k} differs between processes")


def _patched(origin: dict) -> bool:
    """定義元が実験コードか exec か"""
    file = origin.get("file") or ""
    module = origin.get("module") or ""
    return "/src/" in file or file.startswith("<string>") or module.startswith("src.")


def merge(d: str, run_id: str, out: str) -> None:
    """プロセスごとの記録を observed.json に結合する。
    全プロセスで同じ節（hook / 定義 / 継承 / env）は上位に 1 回、calls と reaches は回数を足す"""
    import glob

    processes = [json.load(open(f)) for f in sorted(glob.glob(d + "/*.json"))]
    errors: list[str] = []
    shared: dict = {}
    for key in (
        "hook",
        "src_modules",
        "loaded_file_hashes",
        "foreign_definitions",
        "upstream_extensions",
    ):
        shared[key] = {}
        for p in processes:
            _union(shared[key], p.pop(key, None) or {}, key, errors)
    # 定義元はプロセスで違い得る（親だけが差し替えた）。差し替えの側を残す
    definitions: dict = shared.setdefault("loaded_definitions", {})
    for p in processes:
        for module, table in (p.pop("loaded_definitions", None) or {}).items():
            dst = definitions.setdefault(module, {})
            for name, origin in table.items():
                if name not in dst or (_patched(origin) and not _patched(dst[name])):
                    dst[name] = origin
    envs = [p["process"]["env"] for p in processes]
    if envs and all(e == envs[0] for e in envs):
        shared["env"] = envs[0]
        for p in processes:
            p["process"].pop("env")

    calls: dict = {}
    for p in processes:
        for fn, rec in p.pop("calls", {}).items():
            m = calls.setdefault(fn, {"calls": 0, "args": {}, "samples": []})
            m["calls"] += rec["calls"]
            m["samples"] = (m["samples"] + rec["samples"])[:_SAMPLES]
            for k, a in rec["args"].items():
                ma = m["args"].setdefault(
                    k, {"calls": 0, "types": set(), "values": {}, "seen": set()}
                )
                ma["calls"] += a["calls"]
                ma["types"].update(a["types"])
                ma["seen"].update(a["seen"])
                for value, n in a["values"]:
                    key = json.dumps(value, sort_keys=True, ensure_ascii=False)
                    if key in ma["values"]:
                        ma["values"][key]["calls"] += n
                    elif len(ma["values"]) < _VALUES:
                        ma["values"][key] = {"value": value, "calls": n}
                for key, pick in (
                    ("min", min),
                    ("max", max),
                    ("length_min", min),
                    ("length_max", max),
                ):
                    if key in a:
                        ma[key] = pick(ma[key], a[key]) if key in ma else a[key]
    for m in calls.values():
        for a in m["args"].values():
            a["type"] = "|".join(sorted(a.pop("types")))
            a["distinct"] = min(len(a.pop("seen")), _DISTINCT)
            a["values"] = sorted(a["values"].values(), key=lambda e: -e["calls"])

    reaches: dict = {
        "opens": {},
        "connects": {},
        "getaddrinfo": {},
        "spawns": {},
        "env_changes": {},
        "tamper": [],
    }
    for p in processes:
        r = p.pop("reaches", {})
        for path, rec in r.get("opens", {}).items():
            dst = reaches["opens"].setdefault(path, {"experiment_code": rec.get("experiment_code")})
            for mode, n in rec.items():
                if mode != "experiment_code":
                    dst[mode] = dst.get(mode, 0) + n
        for key in ("connects", "getaddrinfo", "env_changes"):
            for k, n in r.get(key, {}).items():
                reaches[key][k] = reaches[key].get(k, 0) + n
        for s in r.get("spawns", []):
            key = json.dumps([s["event"], s["argv"], s["hooked"]])
            dst = reaches["spawns"].setdefault(key, {**s, "n": 0})
            dst["n"] += s["n"]
        reaches["tamper"] += r.get("tamper", [])
        errors += p.pop("errors", [])
    reaches["spawns"] = list(reaches["spawns"].values())

    merged = {
        "version": 2,
        "run_id": run_id,
        **shared,
        "calls": calls,
        "reaches": reaches,
        "processes": [p["process"] for p in processes],
        "errors": errors,
    }
    with open(out, "w") as f:
        json.dump(merged, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    if sys.argv[1] == "integration":  # python3 sitecustomize.py integration <run_id>
        print(json.dumps(integration(sys.argv[2])))
    else:  # python3 sitecustomize.py merge <dir> <run_id> <out>
        merge(*sys.argv[2:])
elif _OUT_DIR:
    install()
