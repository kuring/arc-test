# Official ARC drain/resume test

独立验证官方 GitHub Actions Runner Controller 的暂停、恢复和排队任务行为。所有测试任务只在 `kuring/arc-test` 中创建。项目不依赖其他仓库的代码、镜像、容量监控或配置。

## 环境

首轮版本固定在 `versions.env`：官方 ARC **0.15.0**、官方 runner **2.338.0**。使用官方 OCI Helm charts 和原始镜像地址。单节点 kind，默认 `minRunners=0, maxRunners=3`；不启用代理、镜像替换、Docker-in-Docker 或额外调度组件。

需要 Docker 已启动、Python 3.9+、GNU Make。`make tools` 将经过官方校验和验证的 kind、kubectl、Helm、GitHub CLI 安装到项目 `.bin/`，不更改系统工具。

```bash
git clone https://github.com/kuring/arc-test.git
cd arc-test
make tools
export PATH="$PWD/.bin:$PATH"
gh auth login
make doctor
make up
make smoke
make test
```

ARC 认证使用 `ARC_TOKEN`，未设置时使用当前 GitHub CLI 登录凭证；也支持忽略的 `.state/github-token`（权限 600）。Token 必须拥有本仓库所需的 runner 管理权限；官方 Helm 文档支持 classic PAT。GitHub CLI 凭证还需要触发、读取和取消本仓库工作流的权限。不要使用工作流 `GITHUB_TOKEN` 作为 ARC 注册凭证。Token 通过标准输入写入 Kubernetes Secret，不进入命令参数、Git 或报告。未来可按官方指南切换 GitHub App。

工作流 `probe.yml` 必须先存在于仓库默认分支并启用。尚未发布时，测试会在修改容量前报错。无需创建 PR 或临时分支。

## 自动测试

```bash
make test                       # smoke → startup → resume → restart，遇失败停止
make test SCENARIO=startup       # 容量 0 下排队，恢复为 3
make test SCENARIO=resume        # 运行中任务 + 暂停期间排队 + 恢复
make test SCENARIO=restart       # 独立记录主动重建 listener 的影响
```

每个场景先完成一个真实 runner 上的任务，证明认证、调度和执行正常。暂停场景将 `minRunners`、`maxRunners` 均设为 0，等待配置和 runner Pod 数量收敛；确认三个任务保持 queued 且未分配 runner，默认观察 60 秒。resume 场景另外确认运行中的 240 秒任务在暂停期间成功结束，且排队任务仍未执行。随后通过官方 Helm chart 恢复最大容量 3，等待原始三个 job ID 成功完成。

恢复等待默认最多 600 秒，队列等待由控制脚本计时，不依赖 GitHub job 的 `timeout-minutes`。恢复超时后，先保存现场，再提交一个新任务观察是否唤醒旧队列；即使诊断成功，原场景仍判失败。自动测试退出时尝试取消本环境的未完成 probe、恢复容量 3，并保留集群和证据；清理失败会导致非零退出码。

`restart` 是干预测试：先按容量 0 排队，再恢复 3，并主动重建 listener；不能用它代替普通恢复测试的结果。初版不保证 listener 关闭期间形成队列，相关离线窗口实验应另行设计。

可直接使用控制脚本调整等待：

```bash
python3 scripts/arc_test.py test --scenario resume --timeout 600 --hold-seconds 60
```

## 手工操作

```bash
make status                     # 显示当前 scale set 名称、listener UID 和任务状态
make drain
make submit COUNT=3 DURATION=10  # 输出工作流链接
make status
make resume
make status
make collect
make cancel                     # 仅取消本环境创建的 probe
make restart                    # 主动重建 listener；属于诊断干预
make down                       # 先卸载 ARC runner，再删除本环境集群
```

也可以从 GitHub Actions 页面手工运行 **ARC probe**：填入 `make status` 返回的 `scale_set`、唯一 `batch`、任务数量和持续时间。这些页面手工创建的任务不会被脚本自动取消，应从页面自行取消。

只有一个修改环境的命令能同时运行；自动测试过程中可用 `make status` 观察。集群名称随机且有 `arc-test-` 前缀，kubeconfig 保存在 `.state/`，所有操作显式指定 context 和集群 UID，不改变默认 kubeconfig。

## 证据与结果边界

报告在 `.state/results/<时间-标识>/report.md` 和 `results.json`，包含场景结论、原任务链接、恢复前后 listener UID，以及版本信息。定期采集 controller、listener、runner 日志、Kubernetes 事件和资源快照；Pod 状态包含实际 imageID。日志隐藏凭证和 broker/session URL，不收集 Secrets。`make collect` 的手工快照位于 `.state/manual/`。

检查脚本本身：`make check`。GitHub 的 **Check test harness** 工作流只执行代码检查；真实测试在 ECS 上通过 `make test` 运行，不会自动登录 ECS 或改动其他环境。

官方 ARC 的 Helm 容量修改可能重建 listener。报告记录这一现象，但不假定它与同一 session 内动态修改容量完全等价。官方配置路径通过，只证明该路径在本次环境中通过，不能直接排除其他调度路径的问题。

正常失败和 Ctrl-C 会尝试恢复；断电、kill -9、网络中断无法保证自动恢复。重连后执行 `make collect`、`make cancel`、`make resume`，或 `make down`。保留的状态用于识别本环境资源；不要手工删除 `.state/state.json`。

## ECS

```bash
workbench connect -i <your-ecs-instance-id>
cd /root/arc-test
make doctor
make test
```

运行长测试可在 tmux 中执行，或让独立后台进程写日志后查看报告。所有环境操作都使用以上相同入口。

官方参考：[安装 ARC](https://docs.github.com/en/actions/tutorials/use-actions-runner-controller/get-started)、[runner scale set 配置与 drain](https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/deploy-runner-scale-sets)、[ARC 0.15.0](https://github.com/actions/actions-runner-controller/releases/tag/gha-runner-scale-set-0.15.0)。
