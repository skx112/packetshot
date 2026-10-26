# PacketShot —— Agent 渲染发包截图

把「全链哨兵」文章里的 **Agent 渲染发包截图** 思路提取出来的独立实现：
不依赖 Burp / Yakit 等客户端，内置 Web 页面渲染数据包，无头浏览器截图，
可选真实重放请求，自动带 **北京时间** 水印，支持 **payload 高亮**。
专为渗透测试 / 攻防演练报告场景设计。

![效果图](docs/screenshot.png)

## 特性

- **Burp 风格渲染**：行号、语法着色（方法/头部/状态码/请求行分色）
- **真实发包**：`--send` 用原始 socket 重放请求，截图中的响应是真实返回
- **可读化响应**：gzip/deflate/br 自动解压、chunked 自动解码、二进制转 hexdump
- **北京时间水印**：页脚固定 UTC+8，交报告直接可用
- **payload 高亮**：`--hl` 指定字符串或正则，命中处黄色标记，并统计命中数
- **Agent 友好**：stdout 只输出 PNG 路径；详见 [AGENTS.md](AGENTS.md)
- **安全边界**：`</script>` 等内容按纯文本渲染，不会破坏页面或执行脚本

## 安装

```bash
git clone https://github.com/skx112/packetshot.git packetshot && cd packetshot
pip install .                                  # 安装 packetshot 命令
python -m playwright install chromium          # 首次需下载浏览器内核
```

已有 Chromium 内核的环境可跳过最后一步。也可以不安装，直接运行
`python3 packetshot.py ...` 或 `python3 -m packetshot ...`。

## 快速开始

```bash
# 已有请求+响应文件，直接渲染
packetshot --req examples/sqli-req.txt --resp examples/sqli-resp.txt \
           --hl "admin' OR '1'='1'-- -,login success" -o out/demo-sqli.png

# 真实重放后截图（响应来自真实目标，不是编造数据）
packetshot --req examples/live-post-req.txt --send \
           --hl "admin' OR '1'='1'-- -" -o out/demo-live.png
```

## 参数

| 参数 | 说明 |
|---|---|
| `--req PATH` | 原始请求文件（Burp `Copy to file` 格式），`-` 读 stdin |
| `--resp PATH` | 原始响应文件（与 `--send` 二选一），支持 CRLF/LF |
| `--send` | 真实重放请求，用真实响应渲染 |
| `--scheme http\|https` | 重放协议；默认按端口猜测，无端口时先试 https 再回退 http |
| `--proxy URL` | 重放代理，如 `http://127.0.0.1:8080`，支持 `user:pass@host:port` |
| `--verify-tls` | 校验 TLS 证书（默认不校验，方便自签/内网目标） |
| `--hl "a,b"` | 高亮字符串，逗号分隔，可重复；命中数显示在工具栏 |
| `--hl-regex` | 把 `--hl` 当正则用（非法正则会给出明确报错） |
| `--title` | 左上角标题（默认 PacketShot） |
| `--max-lines N` | 请求/响应各最多展示行数，默认 500，`0` 不限制 |
| `--wrap` | 长行自动换行（默认保持原行宽，避免超宽图片） |
| `-o PATH` | 输出 PNG（默认 `packetshot-<时间戳>.png`） |
| `--width` | 渲染宽度，默认 1180 |
| `--timeout` | 重放超时秒数，默认 15 |
| `--version` | 显示版本 |

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 成功，stdout 最后一行为 PNG 路径 |
| `1` | 数据包/网络/渲染错误（含超时、空响应、非法正则等） |
| `2` | 命令行参数错误 |

## 行为说明

- **北京时间**：页脚固定显示 UTC+8 时间，攻防演练交报告直接可用。
- **Content-Length 自动修正**：粘贴的请求体经常和头部不符，重放时按真实
  body 重算（与 Burp Repeater 行为一致），避免服务端挂起。
- **响应边界**：按 Content-Length / chunked 终止块读取，不再把 keep-alive
  连接上的多余字节混进截图。
- **超时即失败**：`--send` 未收到任何响应时以非 0 退出，不会产出"空白成功截图"。
- **压缩/编码响应**：gzip、deflate、br 自动解压后展示（响应卡标注"已解压"，
  头部保持原样）；chunked 自动解码；二进制体转 hexdump。
- **HTTPS 自动回退**：Host 未写端口时先试 https，失败再回退 http；收到
  `https://` 重定向也会自动改用 https 重放一次。
- **截图体积可控**：默认每段最多 500 行，超限会标注"已省略 N 行"；
  `--wrap` 可避免超长行把图片撑到几千像素宽。
- **TLS 证书**：默认忽略校验；需要严格校验时加 `--verify-tls`。
- **高亮实现**：先按语法分段（请求行/状态行/头部/body），段内再切高亮词，
  不存在 HTML 注入错位问题；字面量高亮词自动做正则转义。

## 目录结构

```
packetshot/
├── packetshot/            # Python 包
│   ├── cli.py             # 解析 / 重放 / 截图
│   ├── viewer.html        # Burp 风格渲染页（含北京时间页脚）
│   ├── __main__.py        # python -m packetshot
│   └── __init__.py
├── packetshot.py          # 兼容入口（未安装时直跑）
├── tests/                 # pytest 用例（本地模拟服务器，不依赖外网）
├── examples/              # 示例数据包
├── docs/screenshot.png    # 效果图
├── AGENTS.md              # 给其他智能体的使用说明
├── pyproject.toml
└── LICENSE
```

## Agent 集成

其他智能体拉取本仓库后，按 [AGENTS.md](AGENTS.md) 调用即可：
挖到漏洞 → 数据包落盘 → 一条命令出图 → stdout 最后一行为 PNG 路径。

```bash
cat > /tmp/vuln-req.txt <<'EOF'
POST /api/login HTTP/1.1
Host: target.com
Content-Type: application/x-www-form-urlencoded

username=admin'-- -&password=x
EOF

OUT=$(packetshot --req /tmp/vuln-req.txt --send \
      --hl "admin'-- -" -o /tmp/vuln-shot.png | tail -n1)
echo "$OUT"   # /tmp/vuln-shot.png
```

## 开发与测试

```bash
pip install -e ".[dev]"
python -m playwright install chromium
python -m pytest -q
```

测试使用本地模拟服务器（普通/gzip/chunked/二进制/慢响应/代理），
不访问外网；CI 在 Python 3.9 / 3.11 / 3.13 上运行同一套用例。

## 更新日志

### v0.1.1
- 修复：数据包含 `</script>` 时页面被破坏、渲染超时失败
- 修复：重放超时被当作成功、产出空白截图
- 修复：文件/网络/参数错误直接抛出 Python traceback
- 修复：LF 换行的响应文件无法解析；重定向重试可能用错端口
- 新增：gzip/deflate/br 解压、chunked 解码、二进制 hexdump
- 新增：`--max-lines`、`--wrap`、`--proxy`、`--verify-tls`、`--version`
- 新增：HTTPS 自动回退、IPv6 Host 解析、响应边界严格读取
- 新增：56 项 pytest 用例与 GitHub Actions CI

### v0.1.0
- 首个版本：渲染、真实重放、高亮、北京时间、wheel/源码包

## 免责声明

仅限**获得授权**的安全测试、攻防演练与教学场景使用。
使用者须自行确保测试行为合法合规，并承担相应责任。
