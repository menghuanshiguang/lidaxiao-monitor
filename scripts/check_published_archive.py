"""Block report and processing-state regressions between Git revisions."""
import argparse
import json
import re
import subprocess
from pathlib import Path

REPORTS = "docs/reports"
DAILY = re.compile(r"docs/reports/\d{4}-\d{2}-\d{2}\.md")
SECTION = re.compile(r"^## 视频\d+[^\n]*\((BV[0-9A-Za-z]+)\)", re.MULTILINE)
INDEX_DATE = re.compile(r"\((?:\./)?(\d{4}-\d{2}-\d{2}\.md)\)")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], encoding="utf-8")


def snapshot(root, ref):
    paths = git(root, "ls-tree", "-r", "--name-only", ref, REPORTS, "state").splitlines()
    return {path: git(root, "show", f"{ref}:{path}") for path in paths}


def processed(content):
    result = {}
    for line in content.splitlines():
        fields = line.split("|", 3)
        if len(fields) >= 3:
            result[fields[0]] = fields[2]
    return result


def regressions(before, after):
    for path, old in before.items():
        if path not in after:
            yield f"Published file removed: {path}"
        elif DAILY.fullmatch(path):
            lost = set(SECTION.findall(old)) - set(SECTION.findall(after[path]))
            if lost:
                yield f"Published video sections removed from {path}: {', '.join(sorted(lost))}"
    index = f"{REPORTS}/index.md"
    removed = set(INDEX_DATE.findall(before.get(index, ""))) - set(INDEX_DATE.findall(after.get(index, "")))
    if removed:
        yield f"Published dates removed from report index: {', '.join(sorted(removed))}"
    daily = sorted(path for path in after if DAILY.fullmatch(path))
    if daily and after.get(f"{REPORTS}/latest.md") != after[daily[-1]]:
        yield f"latest.md must match {daily[-1]}"
    state = "state/processed.txt"
    old = processed(before.get(state, ""))
    new = processed(after.get(state, ""))
    missing = old.keys() - new.keys()
    if missing:
        yield f"Processing history removed: {', '.join(sorted(missing))}"
    demoted = {key for key in old.keys() & new.keys()
               if old[key] in {"ok", "no-subtitle"} and new[key] not in {"ok", "no-subtitle"}}
    if demoted:
        yield f"Successful processing records regressed: {', '.join(sorted(demoted))}"
    pointer = "state/last_bvid.txt"
    previous = before.get(pointer, "").strip()
    current = after.get(pointer, "").strip()
    if previous != current and previous in old and current in old:
        order = list(old)
        if order.index(current) < order.index(previous):
            yield f"Last processed video went backwards: {previous} -> {current}"
    hints = "state/ending_hints.json"
    lost = json.loads(before.get(hints, "{}")).keys() - json.loads(after.get(hints, "{}")).keys()
    if lost:
        yield f"Ending hints removed: {', '.join(sorted(lost))}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("head")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    problems = list(regressions(snapshot(root, args.base), snapshot(root, args.head)))
    if problems:
        print("\n".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
