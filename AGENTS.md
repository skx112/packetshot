# AGENTS.md —— 给智能体的使用说明书

本文件面向**其他 AI Agent / 自动化框架**（ZCode、Claude Code、Cursor、AutoGPT 等）。
拉取本仓库后，按本文件即可直接调用 PacketShot，把渗透测试数据包渲染成报告截图。

## 1. 这是什么

PacketShot 把**原始 HTTP 请求/响应**渲染成 Burp 风格的 PNG 截图：

- 内置 Web 渲染页，不依赖 Burp / Yakit / Reqable；
- 无头 Chromium（Playwright）截图，2x 高清；
- 可选 `--send` 真实重放请求，截图中的响应来自真实目标；
- 自动附带**北京时间 (UTC+8)** 水印，满足攻防演练报告要求；
- 支持命中 payload 高亮，截图打码定位清晰；
- 响应内容可读化：gzip/deflate/br 解压、chunked 解码、二进制 hexdump。

## 2. 安装

```bash
git clone https://github.com/skx112/packetshot.git packetshot && cd packetshot
pip install .                                  # 安装为 `packetshot` 命令
python -m playwright install chromium          # 首次需要下载浏览器内核
```

依赖已下载过 Chromium 的环境可跳过最后一步。不想安装时也可以直接运行
`python3 packetshot.py ...`（见第 5 节）。

## 3. 调用契约（Agent 必须遵守）

- 入口：`packetshot`（或 `python -m packetshot`，或 `python3 packetshot.py`）
- stdout：成功时**只有一行 —— 生成的 PNG 路径**。直接取这一行贴进报告。
- stderr：人类可读日志（重放状态、字节数、耗时、解压提示），可忽略或展示。
- 退出码：
  - `0` 成功；
  - `1` 数据包/网络/渲染错误（**包括重放超时、空响应、非法正则**）；
  - `2` 命令行参数错误。
- 只有退出码为 0 时才应使用 stdout 中的路径；非 0 时按失败处理。
- 建议输出到临时目录，如 `/tmp/<vuln-id>.png`，避免污染仓库。

## 4. 常用配方

```bash
# A. 已有请求 + 响应（Agent 抓包落盘后渲染）
packetshot --req req.txt --resp resp.txt \
           --hl "<payload>" -o /tmp/vuln.png

# B. 真实重放（截图响应来自真实目标，推荐用于漏洞佐证）
packetshot --req req.txt --send --hl "<payload>" -o /tmp/vuln.png

# C. HTTPS 目标（自签证书默认忽略校验）
packetshot --req req.txt --send --scheme https -o /tmp/vuln.png

# D. 经代理重放（Burp/ZAP 上游代理、内网出口）
packetshot --req req.txt --send --proxy http://127.0.0.1:8080 -o /tmp/vuln.png

# E. 从 stdin 喂请求
cat req.txt | packetshot --req - --send -o /tmp/vuln.png

# F. 正则高亮 / 多组高亮 / 自定义标题
packetshot --req req.txt --resp resp.txt \
           --hl "UNION|SELECT" --hl-regex --title "SQL 注入验证" -o /tmp/vuln.png
packetshot --req req.txt --send --hl "admin'--",password -o /tmp/vuln.png

# G. 大响应/超长行控制
packetshot --req req.txt --send --max-lines 300 --wrap -o /tmp/vuln.png
```

## 5. 未安装时的直跑方式

```bash
cd <repo> && python3 packetshot.py --req ... -o ...
```

或把仓库路径加入 `PYTHONPATH` 后 `python3 -m packetshot ...`。

## 6. 输入格式要求

请求/响应均为 Burp Suite `Copy to file` 的原始格式（首行请求行/状态行，
头部每行一个，空行后是 body）。示例见 `examples/`。

- 请求行里的 payload 允许带未编码空格（如 `GET /a?id=1 UNION SELECT ...`）；
- 请求体末尾的换行会被忽略；
- 响应文件 CRLF / LF 换行均可；
- 重放时**自动重算 Content-Length**，粘贴数据包无需手工修正；
- 重放时强制 `Connection: close`，并按 Content-Length / chunked 终止块
  严格确定响应边界；
- 支持 `Host: example.com:8443`、`Host: [::1]:8080` 等形式。

## 7. 常见错误

| 现象 | 原因 / 处理 |
|---|---|
| `playwright` 导入失败 | 未安装：`pip install playwright` |
| `Executable doesn't exist` | 未下载内核：`python -m playwright install chromium` |
| `[!] 未在 Ns 内收到任何响应` | 目标超时/不可达；检查网络或用 `--scheme`、`--proxy` |
| `[!] 响应文件为空` | `--resp` 指向空文件；改用 `--send` 或补全响应 |
| `[!] 无法解析请求行` | 数据包不完整/格式错误；确认首行是 `METHOD PATH HTTP/x` |
| `[!] 请求缺少 Host 头` | 数据包不完整，补上 `Host:` 后重试 |
| `[!] 无效的正则表达式` | `--hl-regex` 的模式非法；修正或去掉 `--hl-regex` |
| `[!] 渲染失败：...` | 页面脚本异常/渲染超时；检查 `--max-lines`、`--width` 是否极端 |
| 重放返回 `400 Bad Request` | 请求行/头部含非法字符（如未编码空格），属目标端真实行为；可改用编码后的 payload |
| 图片特别宽/高 | 用 `--wrap` 换行、`--max-lines N` 限制行数 |

## 8. 报告使用建议

- 高亮参数 `--hl` 填**实际注入 payload**，截图中黄色底纹即漏洞点；
- 页脚北京时间与响应头时间可互相印证，避免"截图时间造假"质疑；
- 一次漏洞验证建议同时保留：请求包、真实响应包、状态码与耗时；
- 压缩响应会自动解压并在卡片标题标注"gzip 已解压"，头部保持原样；
- 仅限已授权目标使用。
