# 运行与恢复

调度表达式：`*-*-* 09:00:00 Asia/Shanghai`。09:00 是启动时间，生成与核查完成后发布。当前方案由本机 systemd 执行；它不是 ChatGPT Scheduled 列表中的任务。

## 首次安装

先准备依赖、Codex 登录、GitHub 登录与 main 分支的 origin，再校验并安装定时器：

```bash
systemd-analyze verify ops/decision-daily.service ops/decision-daily.timer
install -m 644 ops/decision-daily.service /etc/systemd/system/decision-daily.service
install -m 644 ops/decision-daily.timer /etc/systemd/system/decision-daily.timer
systemctl daemon-reload
systemctl enable --now decision-daily.timer
systemctl list-timers decision-daily.timer
```

不重启 Codex 远程连接服务，也不修改其数据库。unit 文件以仓库版本为准；路径更改后同步修改 ExecStart 和 WorkingDirectory。

## 故障处理

1. 查 `.runtime/last-run.json` 与 `journalctl -u decision-daily.service`，区分生成、校验、提交、网络推送或 Pages 发布问题。
2. 若本地登录失效，在这台机器正常恢复 `codex login` 或 `gh auth login`，不把令牌复制到仓库、命令日志或日报。
3. 若有用户未提交修改，先人工保留并完成这些改动，不运行硬重置或强推。
4. 修复原因后 `systemctl start decision-daily.service`。同日已有日报不会重新生成。
5. 停止未来触发：`systemctl disable --now decision-daily.timer`；这不会删除历史日报。

机器离线时不能生成，恢复后由 Persistent 定时器补执行当前日。长期停机不会自动补写过去日期的“当时情报”。模型日志仅在 `.runtime/` 保存，90 天后清理，不公开上传。

## 内容质量

结构校验、来源链接与联网调用记录可以拒绝明显不合格产物，但不能证明每项事实都正确。读者纠错应在下一期说明并关联原议题。每周回顾判断是否过时、行动是否适用、是否获得实际反馈；没有反馈不记作成功。
