# Horizon Robotics (9660.HK) Monitor

地平线机器人信息源自动监测工具，跑在 GitHub Actions 上。

## 它做什么

每天早上 9:00 (北京时间) 自动跑一次：

1. 抓 **HKEX 披露** (`hkexnews.hk`) — 年报、半年报、配股、股权激励、重大合同、利润警告
2. 抓 **华尔街见闻** (`wallstreetcn.com`) — 实时财经新闻
3. 抓 **财新网** (`caixin.com`) 和 **36氪** (`36kr.com`) — 深度报道
4. 按重要度分级 (1-5) 生成 `digest.md`
5. 把结果 commit 回这个 repo，并存历史快照到 `runs/digest-YYYY-MM-DD.md`

## 一次性部署 (5 步)

### 1. 创建 GitHub repo

到 GitHub 点 **New repository** → 名字随便起，比如 `horizon-monitor` → **Private** → Create

### 2. 上传文件

把这 4 个文件放进 repo 根目录：

```
horizon-monitor/
├── horizon_monitor.py           # 主脚本
├── requirements.txt             # Python 依赖
├── README.md                    # 这个文件
└── .github/
    └── workflows/
        └── monitor.yml          # GitHub Actions 配置
```

最简单的方法 — 在 repo 里点 **Add file → Upload files**，把整个目录拖进去。

### 3. 启用 GitHub Actions 写权限

这一步关键，不做的话 Action 没法把 digest 提交回 repo：

**Settings → Actions → General → Workflow permissions**
→ 选 **Read and write permissions** → Save

### 4. 第一次运行

**Actions** 标签 → 左边选 `Horizon Monitor` → 右上 **Run workflow** → 直接点 **Run workflow**

跑完大概 30-60 秒。如果绿了，回 repo 主页就能看到自动生成的 `digest.md`。

### 5. 自动每天跑

不用做任何事 — `monitor.yml` 已经配好 `cron: '0 1 * * *'`，每天 UTC 01:00 (北京 09:00) 自动触发。

## 怎么读 digest

每次运行后，`digest.md` 会按重要度分组：

- 🔴 **必读** — 财报、利润警告、内幕消息
- 🟠 **高** — 配股/收购、HSD/J6/大众相关、新合同
- 🟡 **中** — 股权激励、董事变更、产品发布
- ⚪ **低** — 月度报表等例行披露

每条带原文链接。HKEX 的链接直接跳到 PDF。

历史记录在 `runs/digest-YYYY-MM-DD.md`，方便回溯。

## 收到推送 (可选)

GitHub 默认会就 commit/Action 失败发邮件。如果想要更主动的：

**Telegram 推送**：在 `monitor.yml` 末尾加一步 — 调用 Telegram bot API 发送 digest.md 摘要。需要先建个 bot 拿 token，告诉我可以补这一段配置。

**GitHub Issue 推送**：每次运行新建一个 Issue，标题=日期，正文=digest，移动端 GitHub App 会推送。这种最简单，要的话告诉我加。

## 本地手动跑

```bash
pip install -r requirements.txt
python horizon_monitor.py --test           # 连通性测试
python horizon_monitor.py --since 180      # 跑一次
python horizon_monitor.py --reset --json   # 重置状态 + 输出 JSON
```

## 调整与定制

### 改变运行频率

`.github/workflows/monitor.yml` 里改 cron：

```yaml
schedule:
  - cron: '0 1 * * *'      # 每天一次
  - cron: '0 1,9 * * *'    # 每天两次 (北京 09:00, 17:00)
  - cron: '0 1 * * 1-5'    # 工作日早上
```

### 调整重要度分级

打开 `horizon_monitor.py`，找 `score_hkex()` 和 `score_news()`，里面是纯关键词字典。比如想把"股权激励"提到必读级，把它从 importance 3 移到 5 即可。

### 加监测源

参考脚本里现有的 `fetch_via_ddg()` 模式，3 行代码就能加一个新的 site。

## 故障排查

**Action 跑红了？** 点进失败的 run，看哪个 step 红：

- `Run connectivity test` 红 — 网络问题或源接口变化。看输出日志，多半是 HKEX 偶尔改 servlet 参数。改 `fetch_hkex()` 里的 params 即可。
- `Commit digest` 红 — 99% 是没开写权限。回到上面第 3 步。
- 其他 — 把日志贴给我，一起看。

**digest 是空的？** 检查 `state/seen.json` — 如果之前已经见过所有条目，新运行就没东西可报。在 Actions 里手动运行，把 `reset_state` 选成 `true` 就会重新标记所有条目为新。

## 局限说明

1. 不解析 PDF 内容 — 只告诉你"有新文件、标题/类别"。手读 PDF 仍是必要的。
2. DDG 抓的财新/36氪条目没有可靠日期，用当前日期占位。
3. 华尔街见闻深度付费内容只能拿到标题摘要，全文需要会员。

## 为什么不在这里加 Bloomberg / NYT / WSJ？

排除是有意的：付费墙 + 反爬 ToS + 对地平线这种港股小盘几乎零独家覆盖。彭博/WSJ 偶尔有相关报道，一般会被见闻/财新转发，所以中文一手源覆盖率反而高得多。
