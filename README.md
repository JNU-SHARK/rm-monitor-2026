# RM Monitor 2026 本机部署记录

本分支用于在 `maverick-server` 单机上部署 RM Monitor 2026。当前目标是：

- 使用 Kubernetes/k3s 在本机运行 RM Monitor。
- 录制 RMUC2026 区域赛官方直播源。
- 多视角原始 FLV 先保存在本机源目录，Bilibili 上传成功后再复制到长期目录。
- 飞书多维表格只记录文件路径和视频链接，不上传视频文件。
- 使用 `biliup` 将一场比赛作为一个 Bilibili 视频上传，各视角作为分 P。

这份 README 同时作为部署台账。项目结束时，优先按本文档逐项检查；脚本只能作为辅助，不能替代人工核对。

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
2. 飞书多维表格先记录相对路径，不上传视频文件，也不立即复制长期目录。
3. `biliup` 从 `/mnt/PC801/rm-monitor/records` 上传原始 FLV。
4. Bilibili 上传成功并写回飞书链接后，再复制到 `/mnt/server_data/rm-monitor/records`。
5. 长期目录复制和校验成功后，删除 `/mnt/PC801` 中对应源文件。

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

该脚本默认从 `/mnt/PC801/rm-monitor/records` 读取源 FLV。使用 `--submit` 成功上传后，会自动调用 [deploy/local/archive_match_artifacts.py](deploy/local/archive_match_artifacts.py) 复制到长期目录、校验、并删除源文件。需要只上传不归档时加 `--no-archive-after-upload`。

当前合集目标：

- 合集名：`RMUC2026南部赛区录制`
- 赛区：南部赛区
- 标题格式：`南部第1场 小组赛 吉林大学2:0西北工业大学 | RMUC2026区域赛`

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
- systemd drop-in：`/etc/systemd/system/clash-verge-service.service.d/rm-monitor-container-net-bypass.conf`
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
sudo rm -f /etc/systemd/system/rm-monitor-container-net-bypass.service
sudo rm -f /usr/local/sbin/rm-monitor-container-net-bypass
sudo rm -f /etc/systemd/system/clash-verge-service.service.d/rm-monitor-container-net-bypass.conf
sudo rmdir --ignore-fail-on-non-empty /etc/systemd/system/clash-verge-service.service.d
sudo systemctl daemon-reload
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
3. `record-dispatcher` 为该场比赛创建 14 路 `record-job`。
4. 比赛状态离开 `STARTED` 后，录制任务收到停止信号并收尾。
5. `uploader-dispatcher` 为原始 FLV 创建飞书多维表格记录，只写相对路径；飞书话题内不再回复文档链接或本地路径，也不复制长期目录、不删除 `/mnt/PC801` 源文件。
6. 赛后执行 `deploy/local/biliup_upload_match.py --zone 南部赛区 --order N --submit`，从 `/mnt/PC801/rm-monitor/records` 上传原始 FLV。
7. Bilibili 上传成功后，脚本按分 P 写回飞书多维表格视频链接，并在飞书比赛话题里只回复一次总 B 站链接；随后复制到 `/mnt/server_data/rm-monitor/records` 并校验，成功后删除源文件。

自动生成的飞书多维表格按赛区建表，例如 `RMUC 2026超级对抗赛-南部赛区`。新表字段顺序为：`场次`、`阶段`、`红方`、`蓝方`、`视角`、`文件路径`、`视频链接`；当前关闭飞书视频附件上传，因此不会创建 `录像` 附件列。

## 告警和降级

直播只有一次，优先级是：保住原始录制 > 上传 Bilibili > 归档长期存储 > 补齐飞书字段。

已配置自动告警：

- `record-job` 录制失败会通知 `lark-notifier`，群内 `@所有人`，并写明“请立即提醒席伟杰修复”。
- 飞书/上传任务失败会群内 `@所有人`。
- 长期目录复制失败会群内 `@所有人`，并点名或文本提醒席伟杰；源文件不会删除。

降级方案：

1. 官方状态没有自动触发录制：先看 `kubectl logs -n rm-monitor deploy/monitor --tail=100`。如果比赛已经开始，立即用宿主机应急录制：

```sh
deploy/local/emergency_record_live.py --zone 南部赛区 --res high --name 南部第N场-手动
```

2. Pod 内直播源异常但宿主机可访问：继续使用上面的应急录制脚本，它直接在宿主机写 `/mnt/PC801/rm-monitor/emergency`，不依赖 K8s 录制 Job。
3. 单个视角失败：不要停止其它视角。先让正常视角录完；失败视角尝试单独用应急脚本补录，必要时降到 `--res middle`。
4. Bilibili 上传失败：不要归档、不要删源。修复登录态或网络后重复执行同一条 `biliup_upload_match.py --submit`。
5. 飞书写链接失败：Bilibili 上传优先。可先加 `--no-feishu-link` 完成上传，之后用 `--add-existing-bvid BV... --zone 南部赛区 --order N --submit` 补写链接。
6. 长期目录复制失败：源文件仍在 `/mnt/PC801`。修好 `/mnt/server_data` 后执行：

```sh
deploy/local/archive_match_artifacts.py --zone 南部赛区 --order N --submit --delete-source
```

7. `/mnt/PC801` 空间不足：停止非必要写入，优先保留当前比赛源 FLV；不要在复制到长期目录并校验前手动删除源文件。

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
