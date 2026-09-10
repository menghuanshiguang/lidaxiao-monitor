#!/usr/bin/env python3
"""把一次性诊断文件从仓库里删除(自删, 保证仓库回到原样)。"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FILES = ["diag_dl.py", "_selfdelete.py", ".github/workflows/diag-dl.yml"]


def gh(args, inp=None):
    p = subprocess.run(["gh"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", input=inp, env=os.environ)
    if p.returncode != 0:
        print("gh 失败:", p.stderr[:300])
        sys.exit(1)
    return json.loads(p.stdout) if p.stdout.strip() else None


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    ref = gh(["api", "repos/%s/git/ref/heads/main" % repo])
    parent = ref["object"]["sha"]
    base = gh(["api", "repos/%s/git/commits/%s" % (repo, parent)])["tree"]["sha"]
    entries = [{"path": f, "mode": "100644", "type": "blob", "sha": None} for f in FILES]
    tree = gh(["api", "--method", "POST", "repos/%s/git/trees" % repo, "--input", "-"],
              inp=json.dumps({"base_tree": base, "tree": entries}))
    c = gh(["api", "--method", "POST", "repos/%s/git/commits" % repo, "--input", "-"],
           inp=json.dumps({"message": "清理: 删除一次性下载诊断脚本(自删)",
                           "tree": tree["sha"], "parents": [parent]}))
    gh(["api", "--method", "PATCH", "repos/%s/git/refs/heads/main" % repo, "--input", "-"],
       inp=json.dumps({"sha": c["sha"], "force": False}))
    print("已自删, 提交:", c["sha"][:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
