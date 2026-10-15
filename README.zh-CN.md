# Official ARC drain/resume test

[English](README.md) | 简体中文

独立验证官方 GitHub Actions Runner Controller 的暂停、恢复和排队任务行为。所有测试任务只在 `kuring/arc-test` 中创建。项目不依赖其他仓库的代码、镜像、容量监控或配置。

## 问题背景与测试目的

此前测试遇到的具体问题是：将 runner 容量设为 `minRunners=0, maxRunners=0` 后提交的 GitHub Actions job，会保持 `queued` 且未分配 runner；随后将最大容量恢复为 3，虽然 ARC 容量配置已经生效、listener 正常运行，原有排队任务仍未自动开始执行。已有实验还观察到，追加提交一个新任务后，旧任务以原 job ID 恢复执行。

`maxRunners=3` 表示最多允许创建 3 个 runner，并不表示已经创建 runner 或恢复了任务调度。因此，容量配置恢复与原有排队任务恢复执行，需要分别验证。

本项目使用独立的官方 ARC 环境复现这一问题，减少其他仓库的定制代码和组件对实验的影响，重点验证：

1. 正常容量下，任务能在本环境的真实 runner 上成功执行。
2. 暂停期间，新提交的任务保持排队且不分配 runner；已经运行的任务能够正常结束。
3. 恢复容量后，原有排队任务能否在不追加新任务、不额外重启 listener、不提高最小容量的情况下，以原 job ID 成功完成。

重启 listener、提交新任务和提高 `minRunners` 等操作用于辅助诊断，结果单独记录。干预后恢复不代表仅恢复容量的测试通过，也不能据此确定 GitHub 服务端根因。

## 测试环境与适用范围

默认版本固定在 `versions.env`：官方 ARC **0.15.0**、官方 runner **2.338.0**。使用官方 OCI Helm charts 和原始镜像地址。单节点 kind，默认 `minRunners=0, maxRunners=3`；不启用代理、镜像替换、Docker-in-Docker 或额外调度组件。

本项目通过官方 Helm chart 修改容量。该操作可能重建 listener，报告会记录修改前后的 listener UID；不能假定它与同一 session 内动态修改容量完全等价。实验通过或失败，只说明对应版本、环境和操作路径下的结果，不能直接外推到其他调度路径。

## 准备与快速开始

### 前置条件

需要 Docker 已启动、Python 3.9+、GNU Make。`make tools` 将经过官方校验和验证的 kind、kubectl、Helm、GitHub CLI 安装到项目 `.bin/`，不更改系统工具。

ARC 认证优先使用 `ARC_TOKEN`，其次使用 `GH_TOKEN`、被 Git 忽略的 `.state/github-token`，最后使用当前 GitHub CLI 登录凭证。若使用凭证文件，应将权限设为 600。ARC 凭证必须拥有本仓库所需的 runner 管理权限；官方 Helm 文档支持 classic PAT。GitHub CLI 使用的凭证还需要触发、读取和取消本仓库工作流的权限。不要使用工作流 `GITHUB_TOKEN` 作为 ARC 注册凭证。ARC 注册凭证通过标准输入写入 Kubernetes Secret，不进入命令参数、Git 或报告。

工作流 `.github/workflows/probe.yml` 必须先存在于仓库默认分支并启用。尚未发布或已被禁用时，自动测试会在修改容量前报错。运行测试无需创建 PR 或临时分支。

### 创建环境并运行测试

```bash
git clone https://github.com/kuring/arc-test.git
cd arc-test
make tools
export PATH="$PWD/.bin:$PATH"
gh auth login
make doctor
make up
export KUBECONFIG="$PWD/.state/kubeconfig"
make test
```

`make test` 已包含 smoke。若只想先确认环境能执行任务，可单独运行 `make smoke`。`make doctor` 只检查工具、Docker 和仓库访问；runner 的注册、调度与执行由真实任务验证。

### 配置 KUBECONFIG

`make up` 创建集群时会生成项目专用的 `.state/kubeconfig`。在项目根目录执行以下命令，让当前终端中直接运行的 `kubectl`、`helm` 使用该测试集群：

```bash
export PATH="$PWD/.bin:$PATH"
export KUBECONFIG="$PWD/.state/kubeconfig"
kubectl config current-context   # 应为 kind-arc-test-<本环境随机标识>
kubectl get nodes                # 确认节点为 Ready
```

环境变量仅对当前终端及其子进程生效。打开新终端或重新连接测试节点后，先进入项目根目录，再执行以上配置；集群尚未创建时，先运行 `make up`。项目的 `make` 操作已显式指定 kubeconfig 和 context，不依赖此环境变量。该配置不会修改 `~/.kube/config`。

`make down` 会删除项目 kubeconfig；若当前终端已按以上步骤配置，清理后执行 `unset KUBECONFIG`，恢复使用默认的 `~/.kube/config`。

## 自动测试场景与通过条件

```bash
make test                       # smoke → startup → resume → restart，遇失败停止
make test SCENARIO=startup       # 无运行中任务时暂停，排队后恢复容量
make test SCENARIO=resume        # 运行中任务 + 暂停期间排队 + 恢复
make test SCENARIO=restart       # 恢复容量后主动重建 listener 的干预测试
```

每个场景先完成一个真实 runner 上的基线任务，证明认证、调度和执行正常。

| 场景 | 测试过程与通过条件 |
| --- | --- |
| `smoke` | 基线任务成功后，再提交三个任务，均在本环境的 runner 上成功完成。 |
| `startup` | 无运行中任务时将容量设为 0，等待 runner Pod 清空，再提交三个任务；确认它们保持 queued 且未分配 runner，恢复容量后原始三个 job ID 均成功完成。 |
| `resume` | 先启动一个 240 秒任务，再将容量设为 0；确认它在暂停期间成功结束，另外提交的三个任务持续排队且未分配 runner；恢复容量后原始三个排队 job ID 均成功完成。 |
| `restart` | 按 `startup` 的方式暂停并形成队列，恢复容量后额外主动重建 listener；原始三个 job ID 均成功完成。该结果单独作为干预测试记录。 |

`startup` 也会先运行基线任务，验证的是无运行中任务时的暂停与恢复，不代表全新集群的首次启动。`restart` 不保证任务在 listener 关闭期间进入队列；listener 离线窗口实验需要另行设计。

暂停场景将 `minRunners`、`maxRunners` 均设为 0，等待配置与 runner Pod 数量收敛。三个排队任务默认至少观察 60 秒；`resume` 还会继续确认运行中任务完成之前，它们始终没有被调度。恢复通过官方 Helm chart 将 `minRunners=0, maxRunners=3` 写回配置。

恢复后等待原任务成功完成的默认超时为 600 秒。队列等待由控制脚本计时，不依赖 GitHub job 的 `timeout-minutes`。当恢复后的等待超时，且原始三个任务仍 queued、未分配 runner 时，脚本会先保存现场，再提交一个新任务，最多额外观察 180 秒；即使诊断成功，原场景仍判失败。

`make test` 遇到首个失败场景就停止，后续场景可能未执行；未执行不代表通过。自动测试退出时尝试取消本环境的未完成 probe、恢复容量 3，并保留集群和证据；场景失败或清理失败都会导致非零退出码。

可直接使用控制脚本调整等待；`--timeout` 用于各阶段的任务等待，不是整个测试的总时长：

```bash
python3 scripts/arc_test.py test --scenario resume --timeout 600 --hold-seconds 60
```

## 手工操作

| 命令 | 作用 |
| --- | --- |
| `make status` | 显示当前环境、scale set 名称、listener UID 和已记录任务的状态。 |
| `make drain` | 将 minRunners/maxRunners 置 0，等待 scale set 与 listener 容量配置收敛。 |
| `make submit COUNT=3 DURATION=10` | 提交 COUNT 个 probe job，各运行 DURATION 秒，输出工作流链接；COUNT 支持 1～3。 |
| `make resume` | 将 minRunners 置 0、maxRunners 恢复为 3，等待容量配置与 listener 就绪。 |
| `make collect` | 采集集群快照、任务状态、事件与日志到 `.state/manual/`。 |
| `make cancel` | 仅取消本环境通过脚本创建并记录的未完成 probe。 |
| `make restart` | 主动重建 listener；属于诊断干预。 |
| `make down` | 先卸载 ARC runner，再删除本环境集群，保留证据。 |

`make drain` 本身不等待 runner Pod 数量收敛；自动测试另有等待步骤。手工操作时需自行确认 runner Pod 状态。`make resume` 完成只表示配置恢复且 listener 就绪，仍需检查原排队任务是否开始执行并成功完成。

也可以从 GitHub Actions 页面手工运行 **ARC probe**：填入 `make status` 返回的 `scale_set`、唯一 `batch`、任务数量和持续时间。这些页面手工创建的任务不会被脚本自动取消，应从页面自行取消。

脚本同一时间只允许一个修改环境的命令运行；自动测试过程中可用 `make status` 观察。集群名称随机且有 `arc-test-` 前缀，kubeconfig 保存在 `.state/`，脚本使用显式 context 并校验集群 UID，不改变默认 kubeconfig。手工 Helm 操作不受脚本的并发锁保护，应在自动测试结束后执行。

## 诊断实验步骤

### 建立共同的排队现场

以下手工实验采用统一观察窗口：容量为 0 时观察排队至少 60 秒，恢复容量后观察 600 秒，额外干预后观察 180 秒。需手工计时并反复执行 `make status`；单次状态查询不代表完成观察窗口。使用其他时长时，应在实验记录中注明。

每种干预都应重新构造同样的现场，一次只测试一种额外干预。上一轮结束后先取消残留 probe、恢复容量；基线验证成功后再暂停：

```bash
make cancel
make resume
make smoke
make status                      # 记录 scale_set、context 和 listener UID
make drain
make status                      # 记录容量为 0 后的 listener UID
kubectl get pods -n arc-runners -o wide
```

使用已配置的项目 KUBECONFIG，确认没有 Pending、Running 或 Unknown 状态的 runner Pod 后，再提交旧队列：

```bash
make submit COUNT=3 DURATION=30  # 三个任务作为“旧队列”
make status                      # 至少观察 60 秒：均 queued 且未分配 runner
make collect                     # 保存暂停期间的现场
make resume
make status                      # 持续观察 600 秒，检查原 job ID 和执行结果
make collect                     # 保存仅恢复容量后的现场，记录采集目录
```

若原任务已开始执行，应继续观察是否成功完成，并记录实际等待时间，不再追加唤醒干预。若 600 秒内始终 queued 且未分配 runner，则记录“仅恢复容量未在观察窗口内恢复调度”，再选择下面一种干预。若任务已获分配但执行失败，应另查执行失败原因，不能归入未唤醒问题。

### 提交新任务

在共同现场仍未恢复的前提下执行：

```bash
make submit COUNT=1 DURATION=5   # 记录新任务提交时间与链接
make status                      # 持续观察 180 秒，检查旧 job ID 与 runner 分配情况
make collect                     # 保存干预后的现场
```

若旧任务以原 job ID 从 queued 转为 in_progress/completed，并分配 runner，记录“提交新任务后观察到旧队列恢复调度”，并继续检查是否全部成功。若窗口结束后仍未获分配，则记录“本次观察窗口内未恢复”。

提交新任务与 runner 建立会话是不同事件。应分别记录新任务提交、listener 分配消息、runner Pod 创建、runner 会话建立和旧 job 开始执行的时间；仅凭提交新任务后恢复，不能断言具体由 runner 会话建立触发。

### 重启 listener

重新建立共同现场后执行：

```bash
make status                      # 记录干预前 listener UID
make restart
make status                      # 确认 listener UID 已变化；持续观察旧队列 180 秒
make collect
```

记录重建后的 listener 是否就绪，以及原 job ID 是否获得 runner 并开始执行。listener 重建成功与旧队列恢复是两个独立结果。

### 提高 minRunners

重新建立共同现场后，将最小容量提高为 1。`make resume` 固定将 `minRunners` 设为 0，因此此实验使用手工 Helm 命令：

```bash
# 替换 <scale_set>、<context>；<arc_version> 使用 .state/state.json 中的 versions.ARC_VERSION
helm --kubeconfig .state/kubeconfig --kube-context <context> upgrade <scale_set> \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
  -n arc-runners --version <arc_version> --reuse-values --set minRunners=1,maxRunners=3 --wait --timeout 300s
kubectl get autoscalingrunnersets -n arc-runners -o yaml
make status                      # 持续观察旧队列 180 秒
make collect
```

通过实际 scale set 配置确认 `minRunners=1, maxRunners=3`；`make status` 中保存的环境容量不能代替实际配置检查。记录 runner 创建、会话建立与旧任务恢复的时间顺序，不预先认定触发原因。

### 记录结果并恢复环境

每轮记录环境版本、scale set、原 workflow/job ID、listener UID、各观察窗口的起止时间、干预时间、执行结果，以及干预前后的采集目录。一次实验中的多个事件应保留各自时间线。

证据采集完成后执行：

```bash
make cancel
make resume                      # 复位为 minRunners=0, maxRunners=3
```

复位会结束最小容量为 1 的实验条件，下一轮需重新建立共同现场。临时修改 EphemeralRunnerSet replicas 的实验属于额外的底层干预，此处仅保留已有观察，不提供通用操作步骤。

## 已有实验结果与结论边界

以下是官方 ARC 0.15.0、runner 2.338.0 环境下已有实验的观察汇总，并非每次运行的预期结果。其中重启 listener 和提高 minRunners 的手工观察未单独保存证据目录；实验日期、完整观察时长和可共享证据尚未在 README 中逐项归档，复测时应按上一节补齐。

| 操作 | 已有实验观察 | 说明 |
| --- | --- | --- |
| 仅恢复容量（`make resume`） | 观察窗口内未恢复 | 原 job 持续 queued、未分配 runner，listener 未观察到对应分配消息。 |
| 重启 listener（`make restart`） | 观察窗口内未恢复 | listener UID 已变化，原任务仍未获分配；该手工观察未单独保存证据目录。 |
| 提交新任务（`make submit`） | 观察到旧队列恢复 | 原 job ID 不变并成功执行；观察顺序为先收到分配消息，后创建 runner。 |
| 将 minRunners 改为 1 | 观察到旧队列恢复 | 提高最小容量后观察到恢复；该手工观察未单独保存证据目录，不足以确定触发机制。 |
| 临时创建 runner（replicas patch） | 观察到旧队列恢复 | 观察到 runner 建立 Broker 会话后旧队列恢复，需保留该实验自身的时间线。 |

临时创建 runner 的调查证据保存在本地 `.state/investigation-38538867417/`，不随仓库提交。其他未提供可共享证据的条目仅作为历史观察，不能当作独立可复核的实验报告。

提交新任务与临时创建 runner 的实验时间线不同，不能统一归因为“新 runner 会话建立触发唤醒”。仅重建 listener 未恢复的观察，也不能外推为 listener 在所有场景下都与恢复无关。所有干预结果均不能代替仅恢复容量的验收，也不能确定 GitHub 服务端根因。

## 报告、证据与异常恢复

### 查看报告与证据

自动测试报告在 `.state/results/<时间-标识>/`，时间使用 UTC：

- `report.md`：场景结果、任务链接和版本概览。
- `results.json`：已执行场景的详细结果、暂停前与恢复后的 listener UID、诊断结果和清理错误。
- 资源快照与日志：controller、listener、runner 日志，Kubernetes 事件及资源配置，GitHub job 状态等；Pod 状态包含实际 imageID。

测试期间定期采集现场；进入失败处理时额外采集到 `before-diagnostic-<场景>/`。日志隐藏凭证和 broker/session URL，不收集 Secrets。手工 `make collect` 的快照位于 `.state/manual/<UTC 时间>/`；请保留命令输出的路径，以区分干预前后的证据。

### 检查测试脚本

`make check` 执行测试脚本的单元测试和 Python 编译检查。GitHub 的 **Check test harness** 工作流还检查工具安装脚本的语法；它不执行真实 ARC 场景。真实测试需在测试节点上运行 `make test`，不会自动连接节点或改动其他环境。

### 中断后恢复与清理

正常失败和 Ctrl-C 会尝试取消未完成 probe 并恢复容量；断电、kill -9、网络中断无法保证自动恢复。重连后先执行 `make collect` 保留现场，再按需要处理：

- 需要保留旧队列继续诊断时，先查看 `make status`，避免取消原任务。
- 结束实验但保留环境时，执行 `make cancel`、`make resume`。
- 不再使用测试环境时，执行 `make down`；若已设置项目 KUBECONFIG，再执行 `unset KUBECONFIG`。

`make test` 会启动新测试，不会从中断位置继续。保留的 `.state/state.json` 用于识别本环境资源，不要手工删除。

## 官方参考

[安装 ARC](https://docs.github.com/en/actions/tutorials/use-actions-runner-controller/get-started)、[runner scale set 配置与 drain](https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/deploy-runner-scale-sets)、[ARC 0.15.0](https://github.com/actions/actions-runner-controller/releases/tag/gha-runner-scale-set-0.15.0)。
