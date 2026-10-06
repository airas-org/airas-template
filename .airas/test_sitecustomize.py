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
"""

EXPERIMENT = """
import os, socket, subprocess, sys, threading
import fakepkg
class Tuned(fakepkg.Controller):                              # 継承と override
    def run(self, *a, **k):
        return super().run(*a, **k)
    def extra(self):
        return 0
def main():
    secret = os.environ["MY_SECRET_VALUE"]
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
        calls = [(c["fn"], c["args"]) for c in parent["calls"]]
        assert calls[0] == (
            "fakepkg.Controller.run",
            {"max_iterations": 20, "eval_debug_rounds": 5},
        )
        assert calls[1] == ("fakepkg.stream", {}) and "ret" not in parent["calls"][1]
        assert calls[2][0] == "fakepkg.propose" and calls[2][1]["seed"] is None
        assert calls[3][1]["seed"] == 3 and calls[3][1]["data"]["type"] == "list"
        assert calls[4][1]["data"] == "thread"  # 平文
        assert calls[5][1]["url"] == "http://h:8000/v1"
        assert calls[5][1]["api_key"] == {
            "redacted": "MY_SECRET_VALUE",
            "len": len(SECRET),
        }
        assert calls[6][1]["url"]["redacted"] == "MY_SECRET_VALUE"
        assert calls[6][1]["api_key"] == "short"
        assert calls[7][1]["data"]["redacted"] == "MY_SECRET_VALUE"
        assert (
            len(calls) == 8
        )  # 差し替え後の propose は上流の code ではないので数えない
        assert parent["calls"][0]["ret"]["type"] == "list"
        assert parent["calls"][4]["thread"] != parent["calls"][0]["thread"]
        assert SECRET not in json.dumps(parent) and "t0kenvalue2" not in json.dumps(
            parent
        )

        env_rec = parent["process"]["env"]
        assert env_rec["FAKE_MODE"] == "fast"
        assert (
            env_rec["AIRAS_SECRET_NAMES"] == "MY_SECRET_VALUE"
        )  # 名前の一覧は伏せない
        assert env_rec["MY_SECRET_VALUE"]["redacted"] == "MY_SECRET_VALUE"
        assert env_rec["FAKE_TOKEN"]["redacted"] == "FAKE_TOKEN"

        syms = parent["loaded_definitions"]["fakepkg"]
        assert (
            "dedent" not in syms and "Path" not in syms and "abstractmethod" not in syms
        )  # import した名前は記録しない（stdlib の frozen モジュール由来も）
        assert (
            syms["generated"]["file"] == "<string>" and parent["errors"] == []
        )  # exec 由来（module None）は出自不明として残し、落ちない
        assert (
            "Config" in syms and "Config.__init__" not in syms
        )  # 生成メソッドは記録しない
        assert list(parent["hook"]) == ["sha256"]  # 宣言の写しは持たない
        assert (
            "Controller.ext" not in syms
        )  # 外部定義は記録しない。snapshot との突き合わせで欠落として見える
        assert syms["Path.is_dir"]["file"].endswith(
            "src/adapter.py"
        )  # import したクラスへの差し替えは残す
        assert "Path.exists" not in syms
        assert syms["propose"]["file"].endswith("src/adapter.py")
        assert syms["Controller.helper"]["file"].endswith("src/adapter.py")
        assert syms["Controller.run"]["file"].endswith("fakepkg/__init__.py")
        assert parent["loaded_file_hashes"]["fakepkg"]["sha256"]
        assert parent["upstream_extensions"] == {
            "adapter.Tuned": {"bases": ["fakepkg.Controller"], "overrides": ["run"]}
        }

        r = parent["reaches"]
        assert r["opens"][f"{tmp}/src/adapter.py"]["modes"] == {"r": 1}
        assert r["opens"][f"{tmp}/out/w.txt"]["modes"] == {"w": 1, "a": 1}
        assert all(
            v["experiment_code"].startswith(f"{tmp}/src/") for v in r["opens"].values()
        )
        assert r["getaddrinfo"] == {"localhost": 1}
        assert [s["hooked"] for s in r["spawns"]] == [True, False]
        assert r["env_changes"] == [{"event": "os.putenv", "name": "FOO"}]
        assert [t["event"] for t in r["tamper"]] == ["sys.setprofile"]
        assert r["tamper"][0]["experiment_code"].startswith(f"{tmp}/src/")

        assert [c["fn"] for c in child["calls"]] == ["fakepkg.propose"]
        assert child["loaded_definitions"]["fakepkg"]["propose"]["file"].endswith(
            "fakepkg/__init__.py"
        )
        # 結合: 全プロセスで同じ節は上位に 1 回だけ
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
        assert merged["run_id"] == "t" and len(merged["processes"]) == 2
        assert "hook" in merged and "loaded_file_hashes" in merged
        assert all(
            "hook" not in p and "loaded_file_hashes" not in p for p in merged["processes"]
        )
        # env は子に FOO が足されているので同じにならず、各プロセスに残る
        assert "env" not in merged
        assert all("env" in p["process"] for p in merged["processes"])
        assert all(
            "loaded_definitions" in p for p in merged["processes"]
        )  # 親は差し替え後なので子と違う
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
