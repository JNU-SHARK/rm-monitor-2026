# RM Monitor 2026 赛季交接

本文记录 `maverick-server` 在 RMUC 2026 录制任务结束后的最终状态，供下一届同学接手。日常启动、排障和审计命令见 [OPERATIONS.md](OPERATIONS.md)，完整部署背景见仓库根目录 [README.md](../../README.md)。

## 最终状态

最终核对时间：`2026-08-14`（Asia/Taipei）。

- `rm-monitor` namespace 和 Kubernetes 对象保留，7 个 Deployment 均已缩容为 `0`。
- namespace 内没有运行中的 Pod 或 Job。
- 所有 `rm-monitor-*` systemd 服务均已停止；自动队列、dashboard、集群守护和 RM 专用代理均已禁用，定时器没有下一次运行时间。
- 2026 复活赛、全国赛和适应性训练的日期型 timer 文件仍保留在仓库和主机上作为历史配置，但下一届不得直接启用。
- `/mnt/PC801/rm-monitor` 下已无 `.flv`、`.mp4`、`.ts` 或 `.m4s` 媒体文件。
- 约 `3.7GB` 的全国赛适应性训练本地媒体缓存已永久删除。正赛源目录仅保留约 `1.7MB` 的每场 `README.md` 审计索引，空的缓存目录壳由 root 所有。
- Postgres/Redis 持久状态、repo 内 `logs/`、`cookies.json` 和 NAS 长期录像均保留。
- `/mnt/PC801` 清理后可用空间约 `886GB`。
- `/mnt/server_data` 最终检查时可用约 `3.6TB`、使用率约 `92%`，主机视角为可写 `rw`，真实创建/删除目录探针通过。下届开赛前仍须重新核对挂载和容量。

## 赛事完整性审计

数据库以 `DONE` 比赛为起点统计，不会漏掉零录制任务的比赛。

| 赛期 | DONE 比赛 | 最终源文件 | 成功上传 | 结论 |
| --- | ---: | ---: | ---: | --- |
| 全国赛 | 98 | 1372 | 1372 | 98 场均为 14 路，未发现缺口 |
| 复活赛 | 32 | 393 | 393 | 第 5–32 场为 14 路；第 1–3 场为 0 路，第 4 场为 1 路 |

复活赛前 4 场属于本届已知历史缺口，不应被后续同学误判为待上传缓存：

- 第 1–3 场没有 record task、artifact 或 upload。
- 第 4 场只有 1 个最终源文件，且该文件已有成功 upload 记录。
- 第 5–32 场共 `28 × 14 = 392` 个最终源文件，全部已有成功 upload 记录。

最终只读盘点的 NAS 规模如下。这里包含按场录像、正赛备用缓存等文件，不能直接拿媒体文件数与数据库的最终源文件数一一比较。

| 目录 | 媒体文件 | 体量 |
| --- | ---: | ---: |
| `RMUC 2026超级对抗赛/复活赛` | 765 | 约 1.4TB |
| `RMUC 2026超级对抗赛/全国赛` | 2520 | 约 4.5TB |

## Bilibili 与飞书

2026 最终配置位于 [rm-monitor-event.conf](rm-monitor-event.conf)：

| 赛期 | Bilibili 合集 | 合集 ID | 正片分区 ID |
| --- | --- | ---: | ---: |
| 复活赛 | `RMUC2026复活赛全视角录制` | `8693730` | `9687660` |
| 全国赛 | `RMUC2026全国赛全视角录制` | `8694808` | `9688876` |

以上 ID 仅用于复盘本届，下一届必须新建合集并替换，不能复用。

全国赛适应性训练期间，官方训练地址曾返回历史比赛画面。本机曾观察到稿件 `BV17BMD6ZEPM`，标题为 `[全国赛适应性训练] 2026-08-03 14:00-15:00 全视角`。本机没有执行远端删除；交接账号后应在 Bilibili 创作中心人工确认该稿件当前是否仍存在以及是否需要删除。不要依据已经清空的本地上传状态文件推断远端状态。

以下内容是敏感状态，不进入 Git：

- Bilibili 登录态：仓库根目录 `cookies.json`，已由 `.gitignore` 排除。
- 飞书应用密钥：Kubernetes Secret `rm-monitor-lark`。
- NAS 挂载凭据：主机 `/etc/fstab` 或系统凭据配置。

移交账号时应通过受控渠道交接，并在下一届首次正式使用前轮换或重新登录。

## 主机保留项

为了让下一届能够复核本届数据，本次没有执行 `deploy/local/cleanup.sh --delete-data`，也没有删除 namespace、PV/PVC、数据库、Redis、Docker 镜像、日志或登录态。

保留的主要位置：

- 仓库：`/home/maverick/RM/rm-monitor-2026`
- 运行日志：`/home/maverick/RM/rm-monitor-2026/logs`
- Kubernetes namespace：`rm-monitor`
- 本地状态：`/mnt/PC801/rm-monitor/postgres`、`/mnt/PC801/rm-monitor/redis`
- NAS 归档：`/mnt/server_data/rm-monitor/records`
- systemd unit：`/etc/systemd/system/rm-monitor-*`
- 容器网络规则：`ip rule show` 中可能仍有 `pref 8998` 规则；服务已禁用，但 oneshot 服务没有自动撤销既有规则
- CIFS 配置：`/etc/fstab` 中 `/mnt/server_data` 项以及 `/etc/fstab.rm-monitor-*.bak`
- UFW：局域网 dashboard 的 `18080/tcp` 规则仍可能存在

`deploy/local/cleanup.sh --confirm --delete-data` 会同时删除本地记录和 NAS 长期录像，交接场景严禁使用。

## 下一届首次启动

不要直接启动本届服务。建议从新分支完成以下步骤：

1. 阅读本文、[OPERATIONS.md](OPERATIONS.md) 和根目录 [README.md](../../README.md)，再检查 Git 历史与工作区。
2. 新建下一届赛事配置，至少更新 event、zone、合集名称、合集 ID、分区 ID、比赛日期和适应性训练日期。
3. 删除或重建所有日期写死为 `2026-07-31`、`2026-08-03`、`2026-08-04` 的 timer/service；不要重新启用旧 timer。
4. 确认 NAS 使用 `rw` 挂载，剩余容量满足整届录制，并从宿主机和 Pod 各做一次创建、写入、读取、删除探针。
5. 重新登录 Bilibili、轮换飞书凭据，并先用私密测试稿验证合集和飞书回填。
6. 检查官方 `schedule.json` 和 `live_game_info.json` 的新赛事字段；不要假设赛事名、赛区名、14 路角色或 URL 结构与 2026 相同。
7. 用 `kubectl apply -k deploy/local` 恢复 Deployment，再核对 7 个 Deployment 均为 `1/1`。
8. 只安装和启动新赛季 unit；先 dry-run，再做 10 秒全视角拉流测试，最后才启用自动队列和守护 timer。

启动前的最低通过条件：

```sh
findmnt -T /mnt/server_data
df -h /mnt/PC801 /mnt/server_data
kubectl get deploy,pods,jobs,pvc -n rm-monitor
systemctl list-units --type=service --all 'rm-monitor-*' --no-pager
systemctl list-timers --all 'rm-monitor-*' --no-pager
GOCACHE=/tmp/rm-monitor-go-cache go test ./...
python3 -m compileall -q deploy/local
kubectl kustomize deploy/local >/dev/null
```

正式比赛前还必须重跑 [OPERATIONS.md](OPERATIONS.md) 中的直播源、音视频轨、上传 dry-run、NAS 写入与完整性审计流程。

## 临时只启动数据库

赛后状态默认缩容为 0。需要查询历史数据时，只启动 Postgres：

```sh
kubectl scale deployment/postgres -n rm-monitor --replicas=1
kubectl rollout status deployment/postgres -n rm-monitor --timeout=120s
```

查询完成后恢复：

```sh
kubectl scale deployment/postgres -n rm-monitor --replicas=0
```

不要为了只读审计直接执行 `kubectl apply -k deploy/local`，因为它会恢复整套服务的副本数。
