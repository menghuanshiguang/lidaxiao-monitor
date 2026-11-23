import re, sys, shutil, os
sys.path.insert(0, '/var/minis/workspace/lidaxiao-monitor')
os.chdir('/var/minis/workspace/lidaxiao-monitor')
from lidaxiao import audit as A

p = 'docs/reports/2026-11-21.md'
text = open(p, encoding='utf-8').read()
lines = text.splitlines()

sec_re = re.compile(r"^## 视频\d+:《(.+?)》\s*\(?(BV\w+)\)?\s*$")
blocks = []
cur = None
for i, ln in enumerate(lines):
    m = sec_re.match(ln)
    if m:
        if cur:
            blocks.append((cur[0], cur[1], lines[cur[2]:i]))
        cur = (m.group(1), m.group(2), i)
if cur:
    blocks.append((cur[0], cur[1], lines[cur[2]:]))

new_lines = []
for title, bvid, body in blocks:
    if f"视频《{title}》" in text.split("## 视频清单")[0]:
        continue
    line = A.make_summary_line(title, "\n".join(body))
    if line:
        new_lines.append(line)
        print("补摘要:", line)

if new_lines:
    text = re.sub(r"(## 今日摘要\n)", r"\1" + "\n".join(new_lines) + "\n", text, count=1)
out = A.resort_report(text)
open(p, 'w', encoding='utf-8').write(out)
shutil.copy2(p, 'docs/reports/latest.md')
print("\n--- 修后摘要区 ---")
print("\n".join(out.splitlines()[:10]))
