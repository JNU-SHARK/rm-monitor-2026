# RM Monitor 2026 本机部署记录

本分支用于在 `maverick-server` 单机上部署 RM Monitor 2026。本届目标是：

- 使用 Kubernetes/k3s 在本机运行 RM Monitor。
- 录制 RMUC2026 复活赛与全国赛官方直播源。
- 多视角原始 FLV 先保存在本机源目录，Bilibili 上传成功后再复制到长期目录。
- 飞书多维表格只记录文件路径和视频链接，不上传视频文件。
- 使用 `biliup` 将一场比赛作为一个 Bilibili 视频上传，各视角作为分 P。

这份 README 同时作为部署台账。项目结束时，优先按本文档逐项检查；脚本只能作为辅助，不能替代人工核对。

赛中/赛后运维速查见 [deploy/local/OPERATIONS.md](deploy/local/OPERATIONS.md)，其中记录了完整性审计 SQL、关停顺序、重启方式和 2026 区域赛收尾时发现的已知缺口。

> [!IMPORTANT]
> RMUC 2026 录制任务已于 `2026-08-14` 完成交接关停。7 个 Kubernetes Deployment 均已缩容为 0，所有 RM Monitor systemd 服务和 timer 已停止、禁用，本地媒体缓存已清空。最终审计结果、保留项、已知缺口和下一届启动步骤见 [deploy/local/HANDOFF_2026.md](deploy/local/HANDOFF_2026.md)。

## 仓库

- 当前仓库：`https://github.com/JNU-SHARK/rm-monitor-2026.git`
- 源仓库：`https://github.com/scutrobotlab/rm-monitor.git`
- 当前工作分支：`dev`
- 本地路径：`/home/maverick/RM/rm-monitor-2026`

## 运行方式

本机使用 k3s，不需要外部 Kubernetes 集群。服务部署在 namespace：

```sh
rm-monitor
```

本地部署入口：

```sh
kubectl apply -k deploy/local
```

常用状态检查：

```sh
kubectl get pods -n rm-monitor -o wide
kubectl get jobs -n rm-monitor
kubectl get pv,pvc -n rm-monitor
```

## Dashboard 看板与日志

本机只读 dashboard：

```sh
python3 deploy/local/dashboard.py --host 0.0.0.0 --port 18080
```

已安装为 systemd 服务时：

```sh
sudo systemctl status rm-monitor-dashboard.service
sudo systemctl restart rm-monitor-dashboard.service
```

访问地址：

```sh
http://127.0.0.1:18080
http://192.168.16.3:18080
```

局域网访问已通过 UFW 仅向 `192.168.16.0/24` 开放 `enp5s0:18080/tcp`。dashboard 不写业务数据库，不创建任务；它只读取 Kubernetes 日志、Pod/Job/Deployment 状态、Postgres 任务状态、存储容量、官方赛程/直播接口，以及本机脚本日志。

首页按值班视角展示红/黄/绿状态，默认筛选“今天”：

- 总判断：当前是否需要立即处理。
- 当前状态：服务运行、官方直播、自动录制、Bili 上传、存储空间、告警信号；日志信号只按最近 15 分钟参与当前判断。
- 需要处理：只列当前要看的问题。
- 今日累计：录制/上传/转码任务数、源文件数量和体量、脚本事件、当天日志采样数量。默认排除名称包含“测试”的比赛任务，避免测试污染正式值班视图。
- 诊断详情：默认折叠，排障时再看分布和原始日志。

异常面板会显示这些问题：

- Deployment 未就绪、Pod 非 Running/Succeeded、容器重启、Job 失败。
- 最近 24 小时内非测试比赛的录制、转码、上传任务失败。
- 比赛已 STARTED 但没有录制任务。
- `/mnt/PC801` 或 `/mnt/server_data` 容量接近或达到危险线。
- 官方赛程或直播接口不可访问。
- 本机 `biliup` 上传、长期归档、应急录制脚本出现 ERROR/WARN。
- 当前筛选范围内的 ERROR/WARN 日志。

本机脚本日志默认写入：

```sh
logs/biliup-upload.log
logs/archive-artifacts.log
logs/emergency-record.log
logs/continuous-cache.log
```

每行是 JSON，便于 dashboard 解析。`download.log` 和 `ds_update.log` 也会作为只读日志源出现在 dashboard 中。

## 数据目录

录制源文件目录：

```sh
/mnt/PC801/rm-monitor/records
```

长期保存目录：

```sh
/mnt/server_data/rm-monitor/records
```

目前 PV：

```sh
rm-monitor-records
```

容量配置在 [deploy/local/kustomization.yaml](deploy/local/kustomization.yaml)，当前为 `900Gi`。长期目录必须按比赛/任务创建子目录，不要直接把文件放在根目录。

默认顺序是：

1. 录制写入 `/mnt/PC801/rm-monitor/records`。
2. 飞书多维表格先记录相对路径，不上传视频文件。
3. 最终 `视角.flv` 生成并稳定后，独立长期归档队列立即复制到 `/mnt/server_data/rm-monitor/records` 并校验，不等待 Bilibili 上传排队。默认校验文件存在和大小一致；需要完整 SHA256 校验时手动加 `--verify-checksum`。
4. `biliup` 从 `/mnt/PC801/rm-monitor/records` 上传原始 FLV。
5. 只有 Bilibili 上传成功、飞书链接回填完成、长期目录复制校验成功后，才删除 `/mnt/PC801` 中对应源文件。

## 外部服务

飞书配置通过 Kubernetes Secret `rm-monitor-lark` 注入，包含：

- `app-id`
- `app-secret`
- `bitable-app-token`

辅助脚本：

```sh
deploy/local/configure-lark-secret.sh
```

Bilibili 上传使用 `biliup`，本机登录态文件当前在仓库根目录：

```sh
cookies.json
```

上传辅助脚本：

```sh
deploy/local/biliup_upload_match.py
```

该脚本默认从 `/mnt/PC801/rm-monitor/records` 读取源 FLV。Bilibili 投稿默认使用转载模式：`--copyright 2 --source "RoboMaster 官方直播"`。自动队列会同时运行 [deploy/local/archive_auto_queue.py](deploy/local/archive_auto_queue.py) 提前归档；上传成功后脚本会再次校验长期目录，确认 Bilibili 和归档都成功后才删除本地源文件。同一场比赛的归档、校验、删除会通过本地锁串行化，避免重复拷贝和源文件清理互相抢同一批文件。默认归档校验只检查文件大小，避免在比赛日反复读取几十 GB 的 SMB 目标文件；需要完整哈希校验时加 `--verify-checksum`。需要只上传不做归档校验/清理时加 `--no-archive-after-upload`。

当前两套合集目标：

| 官方区域 | 赛期 | Bilibili 合集 | 合集 ID | 正片分区 ID | 标题后缀 |
| --- | --- | --- | ---: | ---: | --- |
| `复活赛` | `2026-07-31` 至 `2026-08-02` | `RMUC2026复活赛全视角录制` | `8693730` | `9687660` | `RMUC2026复活赛` |
| `全国赛` | `2026-08-04` 至 `2026-08-09` | `RMUC2026全国赛全视角录制` | `8694808` | `9688876` | `RMUC2026全国赛` |

标题示例：`复活赛第1场 小组赛 红方学校2:0蓝方学校 | RMUC2026复活赛`
和 `全国赛第1场 小组赛 红方学校2:0蓝方学校 | RMUC2026全国赛`。

非敏感赛事参数集中保存在
[deploy/local/rm-monitor-event.conf](deploy/local/rm-monitor-event.conf)。本机 systemd
队列、dashboard 和官方备用缓存服务都读取该文件。修改赛事参数后需要执行
`sudo systemctl daemon-reload` 并重启相关服务。

2026 复活赛与全国赛的最终录制和上传审计已经完成。全国赛 98 场共 1372 个最终源文件，全部上传；复活赛第 5–32 场完整，第 1–3 场无录制、第 4 场仅 1 路。最终数据与异常说明以 [deploy/local/HANDOFF_2026.md](deploy/local/HANDOFF_2026.md) 为准。

上传脚本会同时校验合集 ID、合集名称和正片分区 ID，防止复活赛与全国赛串稿。如果删除、重建或重命名合集，需要同步更新 `rm-monitor-event.conf`。

本届适应性训练使用独立的一次性 systemd timer：复活赛在 `2026-07-31 08:00`
开始录制并写入复活赛合集，全国赛在 `2026-08-03 08:00` 开始录制并写入全国赛合集。复活赛训练
因 NAS 恢复窗口延长至次日 `00:00`，实际直播约在 `20:05` 至 `21:11` 可用；其上传延后到次日中午。
全国赛训练录制到 `2026-08-04 00:00`，上传最晚运行到 `06:00`，避开当日
`09:00` 正赛。正赛和训练上传共用 `logs/biliup-upload-global.lock`，不会并发投稿。

这些 timer 已过期且已禁用，仅作为 2026 历史配置保留。下一届必须按新赛程重建，不能直接重新启用。

测试标题不要带 `RM-Monitor`，按需要使用 `录制测试`。

## 网络改动

本机 Clash Verge/Mihomo 开启了 TUN 和 fake-ip DNS。此前容器内解析 `rtmp.djicdn.com` 得到 `198.18.x.x`，但 Pod 出口没有正确完成 fake-ip 链路，导致官方直播源在 Pod 中 TLS 失败。

当前处理方式：让容器来源网段直连主路由表，绕开 Mihomo fake-ip 策略表。

已加入的系统路由规则：

```sh
ip rule add pref 8998 from 10.42.0.0/16 lookup main
ip rule add pref 8998 from 172.17.0.0/16 lookup main
ip rule add pref 8998 from 172.18.0.0/16 lookup main
ip rule add pref 8998 from 172.19.0.0/16 lookup main
```

相关文件：

- [deploy/local/container-net-bypass.sh](deploy/local/container-net-bypass.sh)
- [deploy/local/rm-monitor-container-net-bypass.service](deploy/local/rm-monitor-container-net-bypass.service)
- [deploy/local/clash-verge-rm-monitor-bypass.conf](deploy/local/clash-verge-rm-monitor-bypass.conf)

实际安装位置：

```sh
/usr/local/sbin/rm-monitor-container-net-bypass
/etc/systemd/system/rm-monitor-container-net-bypass.service
/etc/systemd/system/clash-verge-service.service.d/rm-monitor-container-net-bypass.conf
```

本机还存在一个历史代理转发服务：

```sh
rm-monitor-proxy-relay.service
```

它监听 `192.168.16.3:7897` 并转发到 `127.0.0.1:7897`。当前 RM Monitor 的 Kubernetes Deployment 已不再显式设置代理环境变量，但项目结束时仍应检查这个服务是否还需要保留。

## 存储挂载

长期目录 `/mnt/server_data` 是 CIFS/SMB 挂载：

```sh
//192.168.16.251/团队文件-Server_Data /mnt/server_data cifs ...
```

赛前检查时这个挂载曾出现 `过旧的文件句柄`。已在 `/etc/fstab` 的该挂载项中加入 `noserverino`，并重新挂载，避免 CIFS server inode 导致 stale handle。修改前已自动备份 `/etc/fstab` 到：

```sh
/etc/fstab.rm-monitor-*.bak
```

赛前建议确认：

```sh
findmnt -T /mnt/server_data
df -h /mnt/server_data /mnt/server_data/rm-monitor/records
kubectl exec -n rm-monitor deployment/uploader-dispatcher -- df -h /server-data
```

## 本机系统足迹

项目结束时重点检查这些位置：

- Kubernetes namespace：`rm-monitor`
- Kubernetes PV/PVC：`rm-monitor-records`
- 本地镜像：`rm-monitor/*:latest`
- 录制目录：`/mnt/PC801/rm-monitor/records`
- 长期目录：`/mnt/server_data/rm-monitor/records`
- `/etc/fstab` 中 `/mnt/server_data` 的 CIFS 挂载项，当前包含 `noserverino`
- `/etc/fstab.rm-monitor-*.bak` 备份文件
- systemd 服务：`rm-monitor-container-net-bypass.service`
- systemd 服务：`rm-monitor-proxy-relay.service`
- systemd 服务：`rm-monitor-dashboard.service`
- systemd drop-in：`/etc/systemd/system/clash-verge-service.service.d/rm-monitor-container-net-bypass.conf`
- UFW 规则：`18080/tcp on enp5s0 ALLOW IN 192.168.16.0/24`
- 路由规则：`ip rule show` 中 `pref 8998` 的容器网段规则
- Bilibili 登录态：`cookies.json`
- 本地日志：`download.log`、`ds_update.log`
- Python 包：`biliup`
- k3s 本身。如果这台机器还要跑别的服务，不要直接卸载 k3s。

## 结束清理清单

项目结束后按顺序检查：

1. 停止 RM Monitor 所有正在运行的录制、上传、通知任务。
2. 确认 `/mnt/server_data/rm-monitor/records` 中长期录像已完整保留。
3. 确认是否还需要 `/mnt/PC801/rm-monitor/records` 中的源目录数据。
4. 导出或备份飞书多维表格中需要保留的链接和路径。
5. 删除 Kubernetes 资源：

```sh
kubectl delete -k deploy/local --ignore-not-found=true
kubectl delete namespace rm-monitor --ignore-not-found=true
kubectl delete pv rm-monitor-records --ignore-not-found=true
```

6. 删除 RM Monitor 相关 systemd 改动：

```sh
sudo systemctl disable --now rm-monitor-container-net-bypass.service
sudo systemctl disable --now rm-monitor-dashboard.service
sudo rm -f /etc/systemd/system/rm-monitor-container-net-bypass.service
sudo rm -f /etc/systemd/system/rm-monitor-dashboard.service
sudo rm -f /usr/local/sbin/rm-monitor-container-net-bypass
sudo rm -f /etc/systemd/system/clash-verge-service.service.d/rm-monitor-container-net-bypass.conf
sudo rmdir --ignore-fail-on-non-empty /etc/systemd/system/clash-verge-service.service.d
sudo systemctl daemon-reload
```

如不再需要局域网 dashboard 访问，删除 UFW 规则：

```sh
sudo ufw delete allow in on enp5s0 from 192.168.16.0/24 to any port 18080 proto tcp
```

7. 删除容器直连路由规则：

```sh
for cidr in 10.42.0.0/16 172.17.0.0/16 172.18.0.0/16 172.19.0.0/16; do
  while ip rule show | grep -q "from ${cidr} lookup main"; do
    sudo ip rule del pref 8998 from "$cidr" lookup main || break
  done
done
sudo ip route flush cache
```

8. 如果 `rm-monitor-proxy-relay.service` 不再被其他项目使用，删除它：

```sh
sudo systemctl disable --now rm-monitor-proxy-relay.service
sudo rm -f /etc/systemd/system/rm-monitor-proxy-relay.service
sudo systemctl daemon-reload
```

9. 如果要恢复本项目对 CIFS 挂载的修改，对照 `/etc/fstab.rm-monitor-*.bak` 检查 `/etc/fstab` 中 `/mnt/server_data` 的挂载项。

10. 按需要删除本地镜像：

```sh
docker image ls --format '{{.Repository}}:{{.Tag}}' \
  | grep -E '^(rm-monitor/|ghcr\.io/scutrobotlab/rm-monitor/)' \
  | xargs -r docker rmi
```

11. 按需要删除敏感文件和日志：

```sh
rm -f cookies.json download.log ds_update.log
```

12. 按需要卸载 `biliup`：

```sh
python3 -m pip uninstall -y biliup
```

13. 只有确认本机不再需要 k3s 时，才考虑卸载 k3s：

```sh
sudo /usr/local/bin/k3s-uninstall.sh
```

辅助脚本 [deploy/local/cleanup.sh](deploy/local/cleanup.sh) 可以执行部分清理，但 README 中的清单才是最终核对依据。

## 赛前检查

正式比赛前建议按这个顺序检查：

```sh
kubectl get pods -n rm-monitor
kubectl exec -n rm-monitor deployment/postgres -- psql -U rm_monitor -d rm_monitor -c "select latest_status,count(*) from matches group by latest_status order by latest_status;"
findmnt -T /mnt/server_data
df -h /mnt/PC801 /mnt/server_data
sudo ip rule show | sed -n '1,20p'
systemctl is-active rm-monitor-container-net-bypass.service
```

当前自动录制触发链路：

1. `monitor` 每秒扫描官方 `schedule.json`。
2. 正式比赛状态从 `WAITING` 变为 `STARTED` 后，创建 `match_rounds.STARTED`。
3. `record-dispatcher` 为该场比赛创建 14 路 `record-job`，内部文件名为 `视角.part1.flv`。
4. 单路直播如果 15 秒没有读到数据，当前分段会收尾；只要输出文件含音/视频流，就保留为有效分段，并自动创建 `part2`、`part3` 继续录。
5. 比赛状态离开 `STARTED` 后，录制任务收到停止信号并收尾；随后每个视角的全部分段会用 `ffmpeg -c copy` 合并成最终 `视角.flv`。
6. `uploader-dispatcher` 只处理最终 `视角.flv`，不会把内部 `partN` 分段写入飞书或上传链路；飞书多维表格只写相对路径。
7. `deploy/local/archive_auto_queue.py` 看到赛后最终文件稳定后，会先复制到 `/mnt/server_data/rm-monitor/records` 并做大小校验；这条线不等待 Bilibili 上传队列。完整 SHA256 校验可在手动归档命令中加 `--verify-checksum`。
8. `deploy/local/biliup_auto_queue.py` 赛后按队列调用 `deploy/local/biliup_upload_match.py --submit`，从 `/mnt/PC801/rm-monitor/records` 上传最终原始 FLV。
9. B 站上传成功后写回飞书多维表格视频链接，并在飞书比赛话题里只回复一次总 B 站链接。
10. 只有 Bilibili 上传流程和 `/mnt/server_data/rm-monitor/records` 长期归档复制校验都成功后，脚本才删除 `/mnt/PC801` 上对应的本地源文件并把数据库产物标为 `DELETED`。任一侧失败都会保留本地源文件并发告警。

赛区级 A/B 连续缓存是可选降级方案，默认关闭，只在重要比赛或官方误切/网络风险较高时临时启用。缓存不区分场次和 round，只按赛区、日期、视角、lane 写 60 秒 FLV 分片：

```text
/mnt/PC801/rm-monitor/records/_continuous_cache/RMUC 2026超级对抗赛/全国赛/YYYY-MM-DD/a/视角/YYYYMMDD_HHMMSS.flv
/mnt/PC801/rm-monitor/records/_continuous_cache/RMUC 2026超级对抗赛/全国赛/YYYY-MM-DD/b/视角/YYYYMMDD_HHMMSS.flv
```

启动或重建 A/B 缓存：

```sh
deploy/local/start_continuous_cache_jobs.py --zone 全国赛 --res high --lanes a,b --segment-time 60 --stagger-seconds 30 --apply --replace
```

关闭 A/B 缓存：

```sh
kubectl delete job -n rm-monitor -l app.kubernetes.io/name=continuous-cache-ab
```

A/B 两路独立拉同一视角源，B lane 延迟 30 秒启动。赛后按确认后的比赛时间窗出片：优先使用 `a` lane；若某个 A 分片缺失、过小或 `ffprobe` 失败，则用同时间段的 `b` lane 分片替代。若官方源头本身全局 404，A/B 可能同时缺失；此方案主要抵抗本机网络抖动、单个 ffmpeg 退出和单个分片损坏。旧的按场次 `record-job` 链路暂时保留作兜底，但上传前必须确认最终文件来自正确时间窗。

为某个视角生成 A/B 回退清单：

```sh
deploy/local/cache_fallback_manifest.py \
  --date 2026-08-04 \
  --zone 全国赛 \
  --role 主视角 \
  --start '2026-08-04 09:08:00' \
  --end '2026-08-04 09:20:00' \
  --probe \
  --write-concat /tmp/全国第N场-主视角.concat.txt
```

清单无 `gaps` 时，可用 `ffmpeg -f concat -safe 0 -i /tmp/全国第N场-主视角.concat.txt -c copy 输出.flv` 生成该视角成品。正式上传前，应对 14 个视角分别生成清单并确认没有 gap。

连续缓存不会随普通上传归档自动清理。需要清理时显式启用：

```sh
deploy/local/cleanup_continuous_cache.py --match-id 30902 --submit --enabled
```

自动生成的飞书多维表格按赛区建表，例如 `RMUC 2026超级对抗赛-全国赛`。新表字段顺序为：`场次`、`阶段`、`红方`、`蓝方`、`视角`、`文件路径`、`视频链接`；当前关闭飞书视频附件上传，因此不会创建 `录像` 附件列。

## 告警和降级

直播只有一次，优先级是：保住原始录制 > 上传 Bilibili > 归档长期存储 > 补齐飞书字段。

已配置自动告警：

- `record-job` 录制失败会通知 `lark-notifier`，群内 `@所有人`，并写明“请立即提醒席伟杰修复”。
- 飞书/上传任务失败会群内 `@所有人`。
- 长期目录复制失败会群内 `@所有人`，并点名或文本提醒席伟杰；源文件不会删除。
- 自动续录时，前一个分段若可用会记为成功并写 WARN 日志；dashboard 会把 WARN 暴露出来，但最终上传仍等赛后合并文件生成。

降级方案：

1. 官方状态没有自动触发录制：先看 `kubectl logs -n rm-monitor deploy/monitor --tail=100`。如果比赛已经开始，立即用宿主机应急录制：

```sh
deploy/local/emergency_record_live.py --zone 全国赛 --res high --name 全国第N场-手动
```

2. Pod 内直播源异常但宿主机可访问：继续使用上面的应急录制脚本，它直接在宿主机写 `/mnt/PC801/rm-monitor/emergency`，不依赖 K8s 录制 Job。
3. 单个视角失败：不要停止其它视角。先让正常视角录完；失败视角尝试单独用应急脚本补录，必要时降到 `--res middle`。
4. Bilibili 上传失败：不要删源；长期归档复制可能已经并行完成，但本地源文件仍会保留。先检查 `logs/biliup-auto-queue-submissions.json` 和日志中是否已经出现 BVID；已有 BVID 时使用 `--add-existing-bvid BV... --submit` 补完合集、飞书和归档，不能重新投稿。自动队列会持久化已发现的 BVID，并在 B 站列表接口不可用时暂停新投稿。
5. 飞书写链接失败：Bilibili 上传优先。可先加 `--no-feishu-link` 完成上传，之后用 `--add-existing-bvid BV... --zone 全国赛 --order N --submit` 补写链接。
6. 长期目录复制失败：源文件仍在 `/mnt/PC801`。修好 `/mnt/server_data` 后执行：

```sh
deploy/local/archive_match_artifacts.py --zone 全国赛 --order N --submit
```

确认 Bilibili 上传也成功后，再执行 `deploy/local/archive_match_artifacts.py --zone 全国赛 --order N --submit --delete-source-only` 清理本地源文件。

7. `/mnt/PC801` 空间不足：停止非必要写入，优先保留当前比赛源 FLV；不要在复制到长期目录并校验前手动删除源文件。

宿主机正赛备用录制和适应性训练录制均采用本地优先策略。NAS
不可达时继续写入 `/mnt/PC801`，只暂停长期归档和本地源清理；NAS
恢复后会在后续归档轮次补拷。每分钟运行的集群守护同时重放容器直连
路由，并从 `record-dispatcher` 内验证 Kubernetes API `/livez`，避免调度器
因 API 路由断开而无法创建录制 Job。

## 快速验证

验证容器内能访问官方 HLS，并确认音视频轨：

```sh
url=$(kubectl exec -n rm-monitor deployment/postgres -- \
  psql -U rm_monitor -d rm_monitor -At -c "select source_url from record_tasks where id=20")

kubectl run live-record-smoke -n rm-monitor \
  --image=rm-monitor/record-job:latest \
  --image-pull-policy=IfNotPresent \
  --restart=Never \
  --env="URL=$url" \
  --command -- /bin/sh -lc \
  'ffmpeg -hide_banner -loglevel warning -y -i "$URL" -t 10 -c copy /tmp/live-smoke.flv && ffprobe -hide_banner -v error -show_entries stream=index,codec_type,codec_name -of json /tmp/live-smoke.flv'
```

验证后删除临时 Pod：

```sh
kubectl delete pod -n rm-monitor live-record-smoke --ignore-not-found
```

## 2026-05-12 赛前状态快照

本次赛前检查结果：

- `k3s`、`rm-monitor-container-net-bypass.service`、`clash-verge-service.service` 均为 `active`。
- `rm-monitor` namespace 中 7 个核心 Deployment 均为 `1/1`。
- 数据库中 `RMUC 2026超级对抗赛 / 南部赛区` 有 16 场 `GROUP` 比赛，状态均为 `WAITING`。
- 当前没有 `STARTED` 的 match round，也没有未完成的 Kubernetes Job。
- `/mnt/PC801` 可用约 `958G`，`/mnt/server_data` 可用约 `19T`；Pod 内 `/server-data` 写入和校验通过。
- `/etc/fstab` 中 `/mnt/server_data` 的 CIFS 挂载项已包含 `noserverino`。
- 容器网段直连路由规则 `pref 8998` 已存在。
- 从 `record-job` 临时 Pod 检查南部赛区 `high` 直播源，14 个视角全部可访问，均含 `h264` 视频和 `aac` 音轨。
- `kubectl apply -k deploy/local --dry-run=server` 通过，`go test ./...` 通过。

注意：官方直播赛程接口当前没有暴露可用的开赛时间字段，自动触发依赖官方比赛状态从 `WAITING` 变为 `STARTED`。因此正式比赛前保持 `monitor` 运行即可，不需要手动按时间启动录制。
