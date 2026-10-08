"""sitecustomize.py の動作確認。`python .airas/test_sitecustomize.py` で実行する。

偽の上流 package と実験コード（src/）を一時ディレクトリに作り、フック付きで走らせ、
記録を検査する。"""

import glob
import json
import os
import secrets
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
INTEGRATION = {  # フックが観測するもの: method_entry と、argument（module.Class.method.arg）の関数
    "method_entry": "fakepkg.Controller.run",
    "arguments": ["fakepkg.propose.seed", "fakepkg.stream.n", "fakepkg.connect.url"],
}
SECRET = secrets.token_hex(8)  # 伏せられるべき値。実行ごとに作る

UPSTREAM = """
from pathlib import Path
from textwrap import dedent
_g = {}
exec("def generated():\\n    return 1", _g)
generated = _g["generated"]  # __module__ が None の関数
from dataclasses import dataclass
from abc import abstractmethod  # <frozen abc> 由来。定義として記録しない
@dataclass
class Config:
    n: int = 1
def propose(data, n_basis=10, *, seed=None):
    return [1, 2, 3]
def stream():
    yield "a"
    yield "b"
    return "done"
def connect(url, api_key="x"):
    return url
import abc
class Controller(abc.ABC):  # ABC は _abc_impl を各クラスに置く。override に数えないこと
    def run(self, max_iterations, eval_debug_rounds=5):
        return list(stream()) + propose(None)
    def helper(self):
        return 0
class NotGiven:                 # openai 流の「省略」の印
    def __repr__(self):
        return "NOT_GIVEN"
NOT_GIVEN = NotGiven()
def create(model, n=NOT_GIVEN):
    return model
def inner(y):                   # 依存同士の呼び出し。記録しない
    return y
def helper_fn(x):               # 宣言していないが実験コードから直接呼ばれる
    return inner(x)
"""

EXPERIMENT = """
import os, socket, subprocess, sys, threading
import fakepkg
class Tuned(fakepkg.Controller):                              # 継承と override
    def run(self, *a, **k):
        return super().run(*a, **k)
    def extra(self):
        return 0
def step(i):
    return i
def prompt(text):
    return len(text)
def main():
    secret = os.environ["MY_SECRET_VALUE"]
    for i in range(60):                                       # 異なり値 50 の上限を超える
        step(i)
    for _ in range(3):                                        # 長い文字列は sha で数える
        prompt("p" * 300)
        prompt("q" * 400)
    fakepkg.helper_fn(7)
    fakepkg.helper_fn("sk-" + "a" * 30)                       # 鍵の形は sha に
    fakepkg.helper_fn(object())                               # アドレス入り repr は型だけ
    fakepkg.create("m")                                       # 省略の印の n は記録しない
    open(__file__).close()
    open(os.path.join(os.environ["AIRAS_OBSERVE_DIR"], "w.txt"), "w").close()
    open(os.path.join(os.environ["AIRAS_OBSERVE_DIR"], "w.txt"), "a").close()
    socket.getaddrinfo("localhost", 80)
    fakepkg.Controller().run(20)
    fakepkg.propose([0] * 1000, seed=3)
    t = threading.Thread(target=lambda: fakepkg.propose("thread")); t.start(); t.join()
    fakepkg.connect("http://h:8000/v1", api_key=secret)          # 値で伏せる
    fakepkg.connect(f"http://h:8000/v1?k={secret}", api_key="short")  # URL に含まれても伏せる
    fakepkg.propose({"headers": {"Authorization": f"Bearer {secret}"}})  # dict の repr でも
    fakepkg.propose = lambda *a, **k: []                  # 関数の差し替え
    fakepkg.Controller.helper = lambda self: 1            # メソッドの差し替え
    fakepkg.Controller.ext = staticmethod(fakepkg.dedent) # 外部定義の関数を載せる
    fakepkg.Path.is_dir = lambda self: True               # import したクラスのメソッドの差し替え
    os.putenv("FOO", "1")
    subprocess.run([sys.executable, "-c", "import fakepkg; fakepkg.propose(1)"], check=True)
    subprocess.run([sys.executable, "-IS", "-c", "print(1)"], check=True, capture_output=True)
    sys.setprofile(None)                                  # フックを外す
"""


def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(f"{tmp}/fakepkg")
        os.makedirs(f"{tmp}/src")
        os.makedirs(f"{tmp}/out")
        with open(f"{tmp}/fakepkg/__init__.py", "w") as f:
            f.write(textwrap.dedent(UPSTREAM))
        with open(f"{tmp}/src/adapter.py", "w") as f:
            f.write(textwrap.dedent(EXPERIMENT))
        with open(f"{tmp}/run.py", "w") as f:
            f.write(
                "import sys; sys.path.insert(0, 'src'); import adapter; adapter.main()\n"
            )
        env = {
            **os.environ,
            "PYTHONPATH": HERE,
            "AIRAS_OBSERVE_DIR": f"{tmp}/out",
            "AIRAS_OBSERVE_INTEGRATION": json.dumps(INTEGRATION),
            "AIRAS_SECRET_NAMES": "MY_SECRET_VALUE",  # 基盤が渡す名前一覧
            "MY_SECRET_VALUE": SECRET,
            "FAKE_TOKEN": "t0kenvalue2",  # 一覧に無くても名前の規則で伏せる
            "FAKE_MODE": "fast",
        }
        subprocess.run([sys.executable, "run.py"], cwd=tmp, env=env, check=True)
        files = sorted(glob.glob(f"{tmp}/out/*.json"))
        assert len(files) == 2, files  # 親と、フック付きの子（-IS の子は書かない）
        recs = [json.load(open(f)) for f in files]
        parent = next(r for r in recs if r["process"]["argv"] == ["run.py"])
        child = next(r for r in recs if r["process"]["argv"] == ["-c"])

        assert parent["errors"] == [], parent["errors"]
        assert child["calls"]["fakepkg.propose"]["calls"] == 1
        assert list(parent["hook"]) == ["sha256"]  # 宣言の写しは持たない

        # 結合: 定義は union、calls と reaches は回数を足す
        subprocess.run(
            [
                sys.executable,
                f"{HERE}/sitecustomize.py",
                "merge",
                f"{tmp}/out",
                "t",
                f"{tmp}/observed.json",
            ],
            check=True,
        )
        merged = json.load(open(f"{tmp}/observed.json"))
        assert merged["version"] == 2 and merged["run_id"] == "t"
        assert merged["errors"] == [], merged["errors"]
        assert len(merged["processes"]) == 2
        # env は子に FOO が足されているので同じにならず、各プロセスに残る
        assert "env" not in merged and all("env" in p for p in merged["processes"])
        env_rec = next(p["env"] for p in merged["processes"] if p["argv"] == ["run.py"])
        assert env_rec["FAKE_MODE"] == "fast"
        assert (
            env_rec["AIRAS_SECRET_NAMES"] == "MY_SECRET_VALUE"
        )  # 名前の一覧は伏せない
        assert env_rec["MY_SECRET_VALUE"]["redacted"] == "MY_SECRET_VALUE"
        assert env_rec["FAKE_TOKEN"]["redacted"] == "FAKE_TOKEN"

        calls = merged["calls"]
        run = calls["fakepkg.Controller.run"]
        assert run["calls"] == 1
        assert run["args"]["max_iterations"] == {
            "type": "int",
            "calls": 1,
            "distinct": 1,
            "values": [{"value": 20, "calls": 1}],
            "min": 20,
            "max": 20,
        }
        assert run["args"]["eval_debug_rounds"]["values"] == [{"value": 5, "calls": 1}]
        assert run["samples"][0]["ret"]["type"] == "list"
        stream = calls["fakepkg.stream"]
        assert stream["calls"] == 1 and "ret" not in stream["samples"][0]  # 再開は数えない
        propose = calls["fakepkg.propose"]
        assert propose["calls"] == 5  # 親 4 回 + 子 1 回。差し替え後の lambda は数えない
        assert propose["args"]["seed"]["values"] == [
            {"value": None, "calls": 4},
            {"value": 3, "calls": 1},
        ]
        data = propose["args"]["data"]["values"]
        assert any(v["value"] == "thread" for v in data)  # 平文
        assert any(
            isinstance(v["value"], dict) and v["value"].get("type") == "list" for v in data
        )  # 長い値は型・長さ・sha256
        assert any(
            isinstance(v["value"], dict) and v["value"].get("redacted") == "MY_SECRET_VALUE"
            for v in data
        )  # dict の repr に含まれる秘密も伏せる
        connect = calls["fakepkg.connect"]
        assert connect["calls"] == 2
        assert {json.dumps(v["value"]) for v in connect["args"]["api_key"]["values"]} == {
            json.dumps({"redacted": "MY_SECRET_VALUE", "len": len(SECRET)}),
            json.dumps("short"),
        }
        assert any(
            isinstance(v["value"], dict) and v["value"].get("redacted") == "MY_SECRET_VALUE"
            for v in connect["args"]["url"]["values"]
        )  # URL に含まれても伏せる
        helper = calls["fakepkg.helper_fn"]  # 宣言していない依存でも、実験コードから直接なら記録
        assert helper["calls"] == 3
        kinds = {json.dumps(v["value"], sort_keys=True) for v in helper["args"]["x"]["values"]}
        assert json.dumps(7) in kinds
        assert any('"sha256"' in k and '"len": 33' in k for k in kinds)  # 鍵の形は sha
        assert json.dumps({"type": "object"}, sort_keys=True) in kinds  # アドレスは残さない
        assert "fakepkg.inner" not in calls  # 依存同士の呼び出しは見ない
        assert list(calls["fakepkg.create"]["args"]) == ["model"]  # 省略の印は値ではない
        step = calls["adapter.step"]
        assert step["calls"] == 60
        assert step["args"]["i"]["distinct"] == 60 and len(step["args"]["i"]["values"]) == 50
        assert (step["args"]["i"]["min"], step["args"]["i"]["max"]) == (0, 59)
        text_arg = calls["adapter.prompt"]["args"]["text"]
        assert text_arg["distinct"] == 2 and text_arg["calls"] == 6
        assert (text_arg["length_min"], text_arg["length_max"]) == (300, 400)
        assert all("sha256" in v["value"] for v in text_arg["values"])
        assert calls["adapter.main"]["calls"] == 1
        assert not any("<lambda>" in fn for fn in calls)
        assert "adapter.Tuned" not in calls  # クラス本体の実行は呼び出しではない
        assert SECRET not in json.dumps(merged) and "t0kenvalue2" not in json.dumps(merged)
        assert "sk-" + "a" * 30 not in json.dumps(merged)

        assert list(merged["src_modules"]) == ["src/adapter.py"]
        assert merged["foreign_definitions"] == {
            "pathlib.Path.is_dir": "src/adapter.py"
        }  # import したクラスへの差し替えは定義元のモジュールで見る
        syms = merged["loaded_definitions"]["fakepkg"]
        assert (
            "dedent" not in syms and "Path" not in syms and "abstractmethod" not in syms
        )  # import した名前は記録しない（stdlib の frozen モジュール由来も）
        assert "Path.is_dir" not in syms  # foreign_definitions の側
        assert syms["generated"]["file"] == "<string>"  # exec 由来（module None）は出自不明として残す
        assert (
            "Config" in syms and "Config.__init__" not in syms
        )  # 生成メソッドは記録しない
        assert (
            "Controller.ext" not in syms
        )  # 外部定義は記録しない。snapshot との突き合わせで欠落として見える
        assert syms["propose"]["file"].endswith("src/adapter.py")
        assert syms["Controller.helper"]["file"].endswith("src/adapter.py")
        assert syms["Controller.run"]["file"].endswith("fakepkg/__init__.py")
        assert merged["loaded_file_hashes"]["fakepkg"]["sha256"]
        assert merged["upstream_extensions"] == {
            "adapter.Tuned": {"bases": ["fakepkg.Controller"], "overrides": ["run"]}
        }

        r = merged["reaches"]
        assert r["opens"]["src/adapter.py"]["r"] == 1  # cwd からの相対パス
        assert (r["opens"]["out/w.txt"]["w"], r["opens"]["out/w.txt"]["a"]) == (1, 1)
        assert all(
            v["experiment_code"].startswith(f"{tmp}/src/") for v in r["opens"].values()
        )
        assert r["getaddrinfo"] == {"localhost": 1}
        assert [(s["hooked"], s["n"]) for s in r["spawns"]] == [(True, 1), (False, 1)]
        assert r["env_changes"] == {"FOO": 1}
        assert [t["event"] for t in r["tamper"]] == ["sys.setprofile"]
        assert r["tamper"][0]["experiment_code"].startswith(f"{tmp}/src/")
        # integration: run の design の repository_integration から、文献（凍結前は並び順の id）の
        # method_entry と各 argument
        os.makedirs(f"{tmp}/.research")
        arguments = [{"argument": a, "value": 1} for a in INTEGRATION["arguments"]]
        design = {
            "literature": [
                {"url": "x"},
                {"title": "y", "repositories": [{"method_entry": INTEGRATION["method_entry"]}]},
            ],
            "hypotheses": [
                {
                    "claims": [
                        {
                            "designs": [
                                {"runs": [{"run_id": "other"}], "repository_integration": {"repository_id": "s1.r1"}},
                                {
                                    "runs": [{"run_id": "t"}],
                                    "repository_integration": {"repository_id": "s2.r1", "arguments": arguments},
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        with open(f"{tmp}/.research/design.json", "w") as f:
            json.dump(design, f)
        for run_id, expected in (("t", INTEGRATION), ("undeclared", {})):
            out = subprocess.run(
                [sys.executable, f"{HERE}/sitecustomize.py", "integration", run_id],
                cwd=tmp,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            assert json.loads(out) == expected, out
        # 凍結後は record（明示 id）が design.json より優先し、追記式なので同じ run の最後の宣言が生きる
        record = {
            "literature": [
                {"id": "s1", "repositories": [{"id": "s1.r1", "method_entry": "fakepkg.Controller.helper"}]}
            ],
            "hypotheses": [
                {
                    "claims": [
                        {
                            "designs": [
                                {
                                    "runs": [{"run_id": "t"}],
                                    "repository_integration": {"repository_id": "s1.r1", "arguments": arguments},
                                },
                                {
                                    "runs": [{"run_id": "t"}],
                                    "repository_integration": {
                                        "repository_id": "s1.r1",
                                        "arguments": [{"argument": "fakepkg.propose.n_basis", "value": 2}],
                                    },
                                },
                                {"runs": [{"run_id": "plain"}]},
                            ]
                        }
                    ]
                }
            ],
        }
        with open(f"{tmp}/.research/record.json", "w") as f:
            json.dump(record, f)
        for run_id, expected in (
            ("t", {"method_entry": "fakepkg.Controller.helper", "arguments": ["fakepkg.propose.n_basis"]}),
            ("plain", {}),
        ):
            out = subprocess.run(
                [sys.executable, f"{HERE}/sitecustomize.py", "integration", run_id],
                cwd=tmp,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            assert json.loads(out) == expected, out
    print("ok")


if __name__ == "__main__":
    main()
