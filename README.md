# 考公时政卡片自动生成系统

这是一个面向公务员考试备考的时政自动化工具：定时读取公开的 RSS 或网页列表，提取官方新闻正文，调用 OpenAI 兼容大模型生成严格 JSON，再输出 Markdown 卡片、Anki 导入文件和结构化 JSON，并可选推送到 PushPlus 与飞书。PushPlus 默认采用移动端短版布局：每日最多 5 篇，每篇最多 2 张卡片、1 道单选和 1 道填空，先记忆后测验，避免消息过长。

## 1. 安装与第一次运行

建议使用 Python 3.11 虚拟环境：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
export LLM_API_KEY="你的 OpenAI 兼容接口 Key"
python bot.py
```

如果暂时没有配置 `config.yaml` 或模型 Key，程序仍会启动、创建 `output/` 和日志，但不会生成模型卡片。真正使用前请配置公开来源和兼容接口。

## 2. 配置说明

`config.yaml` 包含 `sources`、`llm`、`filter` 和 `output` 四组配置。`llm.api_key_env` 只是环境变量名，Key 不写入 YAML，也不会写入日志。模型服务需要支持 Chat Completions；程序请求 JSON mode，并对不规范 JSON 自动重试一次。

普通网页源使用列表页中的 `<a>` 链接，并逐篇抓取正文；RSS 源读取条目摘要并按需抓取完整正文。来源应当是公开、无需登录、无需付费的官方页面。请优先使用国务院、中国政府网、部委和地方政府公开页面，并遵守对方站点的访问规则。

## 3. 输出文件

运行后默认在 `output/` 生成：

| 文件 | 用途 |
|---|---|
| `YYYY-MM-DD.md` | 当日 Markdown 汇总，含来源链接、摘要、考点、卡片和自测题 |
| `anki.csv` | UTF-8 with BOM、制表符分隔，可在 Anki 中选择 Tab 导入 |
| `cards.json` | 结构化卡片与题目数据 |
| `articles.sqlite3` | URL MD5 和标题相似度去重数据库 |
| `bot.log` | 抓取、LLM 和推送日志 |
| `llm_failures.log` | 两次 JSON 解析/调用都失败的记录 |

## 4. 定时运行

### 本地 cron

```cron
# 每天 07:30 执行；请填写绝对路径和环境变量
30 7 * * * cd /path/to/kaogong-politics-bot && /path/to/.venv/bin/python bot.py >> output/cron.log 2>&1
```

### GitHub Actions

工作流文件位于 `.github/workflows/daily.yml`。在仓库 Settings → Secrets and variables → Actions 中添加 `LLM_API_KEY`；如果需要推送，再添加 `PUSHPLUS_TOKEN` 和 `FEISHU_WEBHOOK`。工作流会把 `output/` 作为 artifact 保存，并缓存 SQLite 数据库，避免每次重复处理。

### 云函数

将本目录打包部署为定时函数即可，函数入口为 `bot.main`。函数需要可写的临时目录；若要保留跨次运行的去重库，请把 `output/articles.sqlite3` 放在对象存储或挂载盘。网络权限只需允许访问公开新闻源和已配置的模型/推送服务。

## 5. 设计与安全说明

程序默认 15 秒 HTTP 超时、请求间隔可配置，单源或单篇失败不会中断整体任务。过滤同时检查时间窗口和关键词，并按源限制数量。SQLite 先按 URL MD5 精确去重，再按最近 500 条标题做相似度去重。

程序不会主动访问登录、付费或非公开内容，也不会绕过验证码或访问控制。请不要把真实 API Key 提交到 Git；推荐只使用环境变量或 GitHub Secrets。模型只能基于抓到的原文生成内容，日期、数字、文件名和提法无法在原文确认时应输出“待核对”，使用前仍建议人工复核。
