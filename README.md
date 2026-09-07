# 决策情报日报

每天用可核查信息改善资产、职业、经营与生活决策。每日 10—20 条，按论点、论据、论证、行动、成本、反证组织；首页优先展示三项行动。

- 阅读站点：https://100apps.github.io/decision-daily/
- 时区：Asia/Shanghai（北京时间）
- 计划启动时间：每天 09:00；联网研究、校验和发布需要时间，完成后更新网页。
- 首期：2026-09-07 晚间初始化版。

## 长期目录

```text
daily/2026/09/2026-09-07.md    每日源文件，按年/月归档
docs/                        生成的 GitHub Pages 网站
docs/daily/2026/09/           网页和可下载 Markdown
docs/archive/                年月归档与往期入口
prompts/daily.md              长期编辑与核查规则
config/site.json             站点、时区与保留策略
scripts/build_site.py        校验并构建静态网站
scripts/run_daily.py         隔离生成、归档、提交、验证发布
ops/                         systemd 定时器与服务定义
tests/                       关键构建行为测试
.runtime/                    本机运行状态与日志，不提交 Git
```

旧期保持可追溯。新证据和更正在新一期复盘中说明；不得每天复制旧文换日期。公开内容只使用通用画像和显式示例金额，不放入私人账户或客户资料。

## 编辑标准

模块包括资产与现金流、职业与能力、公司与经营、全球变化与跨境机会、生活与安全。相关性和行动价值优先于热度。引用原始法规、统计、研究与公司披露，区分已核实事实、发布方主张和分析推断。

任何金额都说明真实或假设输入。投资计算包含上涨时的机会成本；本金回收不算利润；工时价值不等于现金收入；不编造健康损害概率和精准收益率。完整规则见 [编辑任务](prompts/daily.md)。

## 本地操作

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/build_site.py
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/run_daily.py --publish-only --date 2026-09-07 --no-push
```

若系统 Python 没有 venv/pip，需要先准备可用的项目虚拟环境。生产定时器使用该项目 `.venv/bin/python`。

## 定时运行

使用当前常驻 Linux 机器的 systemd timer，显式指定 Asia/Shanghai。机器需要开机联网，Codex 与 GitHub 登录需要有效。复用本机已登录的 Codex CLI，不把登录凭据放进仓库或 GitHub Actions。

生成阶段在 `.runtime/runs/` 的独立工作目录中运行，只产出 Markdown；真实仓库的提交和推送由固定脚本执行。校验失败不发布。并发执行有文件锁，同日已有日报会复用，避免重复生成。离线错过时间后恢复运行时补执行当前日期；不会伪造缺失日期日报。

服务失败后每 15 分钟重试，六小时内最多三次启动；本地状态在 `.runtime/last-run.json`，模型日志保留 90 天。失败时网站保留最近成功一期；“今日尚未发布”的提示不会把旧内容伪装为新内容。

```bash
systemctl status decision-daily.timer
systemctl list-timers decision-daily.timer
journalctl -u decision-daily.service -n 80
systemctl start decision-daily.service
systemctl disable --now decision-daily.timer
```

Pages 使用 main 分支的 `/docs` 目录。推送成功后还会核验线上 Markdown、当期阅读页和首页；三项内容都与本次构建一致才记录 published 状态。提交或网络中断后按本地事务记录续传，遇到用户修改会保留并停止。

## 方法与能力参考

- [Codex 非交互执行官方文档](https://learn.chatgpt.com/docs/non-interactive-mode)
- [GitHub Pages 发布来源](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)

本项目内容为 AI 辅助研究，不构成收益保证。可核查证据、明确适用对象与持续纠错是日报的基本要求。
