"""sitecustomize.py の動作確認。`python .airas/test_sitecustomize.py` で実行する。

偽の上流 package と実験コード（src/）を一時ディレクトリに作り、フック付きで走らせ、
記録を検査する。"""

import glob
import json
import os
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))

UPSTREAM = """
def propose(data, n_basis=10, *, seed=None):
    return [1, 2, 3]
def stream():
    yield "a"
    yield "b"
    return "done"
class Controller:
    def run(self, max_iterations, eval_debug_rounds=5):
        return list(stream()) + propose(None)
    def helper(self):
        return 0
"""

EXPERIMENT = """
import os, socket, subprocess, sys, threading
import fakepkg
def main():
    open(__file__).close()
    open(os.path.join(os.environ["AIRAS_OBSERVE_DIR"], "w.txt"), "w").close()
    open(os.path.join(os.environ["AIRAS_OBSERVE_DIR"], "w.txt"), "a").close()
    socket.getaddrinfo("localhost", 80)
    fakepkg.Controller().run(20)
    fakepkg.propose([0] * 1000, seed=3)
    t = threading.Thread(target=lambda: fakepkg.propose("thread")); t.start(); t.join()
    fakepkg.propose = lambda *a, **k: []                  # 関数の差し替え
    fakepkg.Controller.helper = lambda self: 1            # メソッドの差し替え
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
            "AIRAS_OBSERVE_PACKAGES": "fakepkg",
            "AIRAS_OBSERVE_COMPONENTS": "fakepkg.Controller.run,fakepkg.propose,fakepkg.stream",
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
        assert calls[4][1]["data"]["sha256"]  # 文字列も平文では残らない
        assert (
            len(calls) == 5
        )  # 差し替え後の propose は上流の code ではないので数えない
        assert parent["calls"][0]["ret"]["type"] == "list"
        assert parent["calls"][4]["thread"] != parent["calls"][0]["thread"]

        syms = parent["symbols"]["fakepkg"]
        assert syms["propose"]["file"].endswith("src/adapter.py")
        assert syms["Controller.helper"]["file"].endswith("src/adapter.py")
        assert syms["Controller.run"]["file"].endswith("fakepkg/__init__.py")
        assert parent["modules"]["fakepkg"]["sha256"]

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
        assert child["symbols"]["fakepkg"]["propose"]["file"].endswith(
            "fakepkg/__init__.py"
        )
    print("ok")


if __name__ == "__main__":
    main()
