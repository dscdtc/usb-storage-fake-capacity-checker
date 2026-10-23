#!/usr/bin/env python3
"""把 web/ 打包成小红书「小工具」(Builder Hub) 可审核的 zip。

小红书小工具的硬性要求与我们的差异只有两点：
  1. zip 根目录必须直接是 index.html，不能再套一层文件夹、不能引用网络资源；
  2. 容器内不能跳转站外，所以页面里的 GitHub 外链要降级成纯文本。

除此之外 web/ 本身就是纯静态、零依赖、无网络请求，直接复用，不另做一套页面。
修改页面请改 web/，然后重新执行本脚本，不要手改 dist/。

用法：
    python build_xhs.py
"""

import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "web")
OUT_DIR = os.path.join(ROOT, "dist", "xhs")
OUT_ZIP = os.path.join(ROOT, "dist", "usb-health-check-xhs.zip")

REPO = "dscdtc/usb-storage-fake-capacity-checker"
REPO_URL = "https://github.com/" + REPO

# 每条替换都必须精确命中一次，源码一改就报错，避免悄悄生成一个过期包。
HTML_REPLACEMENTS = [
    # ① 第 0 步的两个下载按钮 → 纯文本，避免容器里的死链
    (
        '        <a class="btn btn-primary btn-sm" href="%s" target="_blank" rel="noopener">在 GitHub 打开项目</a>\n'
        '        <a class="btn btn-sm" href="%s/archive/refs/heads/main.zip" target="_blank" rel="noopener">下载 ZIP（免 git）</a>\n'
        '        <span class="micro">MIT 协议 · 纯 Python 标准库 · 不用 pip install</span>' % (REPO_URL, REPO_URL),
        '        <span class="micro"><b>开源仓库</b>：<code>github.com/%s</code><br>'
        '        在电脑浏览器里打开即可下载（ZIP 或 git clone 都行）；MIT 协议 · 纯 Python 标准库 · 不用 pip install</span>' % REPO,
    ),
    # ② 恢复区的兜底链接 → 指回页面内步骤
    (
        '还没下载项目？<a href="%s" target="_blank" rel="noopener">去 GitHub 拿</a>' % REPO_URL,
        '还没下载项目？回到第 ② 步就能看到仓库地址',
    ),
    # ③ 页脚外链 → 纯文本
    (
        '<p class="micro"><a class="foot-link" href="%s" target="_blank" rel="noopener">GitHub 源码与命令行工具 →</a></p>' % REPO_URL,
        '<p class="micro">命令行工具源码：<code>github.com/%s</code></p>' % REPO,
    ),
]

JS_REPLACEMENTS = [
    # 顶栏 GitHub 入口指向站外，容器里点了也出不去，直接让它不出现
    ("const REPO_URL = '%s';" % REPO_URL, "const REPO_URL = '';"),
]


def read(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as f:
        return f.read()


def apply(text, pairs, label):
    for old, new in pairs:
        n = text.count(old)
        if n != 1:
            sys.exit(
                "[错误] %s 中预期命中 1 次，实际 %d 次：\n%s" % (label, n, old[:120])
            )
        text = text.replace(old, new)
    return text


def main():
    if not os.path.isfile(os.path.join(SRC, "index.html")):
        sys.exit("[错误] 找不到 web/index.html")

    html = apply(read("index.html"), HTML_REPLACEMENTS, "index.html")
    js = apply(read("app.js"), JS_REPLACEMENTS, "app.js")
    css = read("styles.css")

    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR)

    files = {"index.html": html, "styles.css": css, "app.js": js}
    for name, content in files.items():
        with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

    os.makedirs(os.path.dirname(OUT_ZIP), exist_ok=True)
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in files:  # 根目录直放，不套文件夹
            z.write(os.path.join(OUT_DIR, name), arcname=name)

    # ---- 自检 ----
    print("包内文件：")
    with zipfile.ZipFile(OUT_ZIP) as z:
        for info in z.infolist():
            print("  %-14s %7.1f KB" % (info.filename, info.file_size / 1024))
    size = os.path.getsize(OUT_ZIP)
    print("zip 体积：%.1f KB（上限 10 MB，余量充足）" % (size / 1024))

    bad = []
    for name, content in files.items():
        if name == "index.html":
            if "<script>" in content:
                bad.append("index.html 含内联 <script>")
            if re.search(r'<a[^>]+href="https?://', content):
                bad.append("index.html 仍有站外 <a> 链接")
            if re.search(r"<(img|script|link)[^>]+(src|href)=\"https?://", content):
                bad.append("index.html 有远程资源引用")
        if name == "styles.css" and re.search(r"url\(\s*['\"]?https?://", content):
            bad.append("styles.css 有远程资源引用")
        if name in ("app.js",) and re.search(r"\b(fetch|XMLHttpRequest|importScripts)\s*\(", content):
            bad.append("app.js 含网络请求")

    urls = sorted(set(re.findall(r"https?://[^\s'\"<>)]+", html)))
    print("页面内出现的 URL（应仅有纯文本）：")
    for u in urls:
        print("  " + u)

    if bad:
        print("\n[不合格] " + "；".join(bad))
        return 1
    print("\n[通过] 无内联脚本、无站外链接、无网络请求、无远程资源")
    print("产物：%s" % OUT_ZIP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
