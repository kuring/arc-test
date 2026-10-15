# Official ARC drain/resume test

English | [简体中文](README.zh-CN.md)

Independently test pause, resume, and queued-job behavior with the official GitHub Actions Runner Controller (ARC). All test jobs are created only in `kuring/arc-test`. This project does not depend on code, images, capacity monitoring, or configuration from other repositories.

## Problem and test objectives

Previous tests exposed the following problem: jobs submitted while runner capacity was set to `minRunners=0, maxRunners=0` remained `queued` without an assigned runner. After restoring the maximum capacity to 3, the original queued jobs still did not start, even though the ARC capacity configuration had converged and the listener was running normally. In existing experiments, dispatching one additional job was followed by the original jobs starting with their original job IDs.

`maxRunners=3` allows up to 3 runners; it does not mean that runners have been created or that job scheduling has recovered. Capacity configuration and execution of the original queued jobs must therefore be verified separately.

This project reproduces the problem in an independent environment using official ARC components, reducing the influence of custom code and components from other repositories. It verifies that:

1. With normal capacity, jobs complete successfully on actual runners belonging to this environment.
2. While paused, newly submitted jobs remain queued without assigned runners, and jobs already running can finish successfully.
3. After capacity is restored, the original queued jobs can complete successfully with their original job IDs, without submitting more jobs, explicitly restarting the listener again, or increasing the minimum capacity.

Restarting the listener, submitting a new job, and increasing `minRunners` are diagnostic interventions with separately recorded results. Recovery after an intervention does not make the capacity-only recovery test pass and does not establish a GitHub service root cause.

## Test environment and scope

Default versions are pinned in `versions.env`: official ARC **0.15.0** and official runner **2.338.0**. The project uses official OCI Helm charts and original image references. It runs on a single-node kind cluster with `minRunners=0, maxRunners=3` by default, without a proxy, image substitution, Docker-in-Docker, or additional scheduling components.

Capacity changes use the official Helm chart. These updates may recreate the listener; the results record listener UIDs before and after the changes. This path must not be assumed equivalent to changing capacity within the same listener session. A pass or failure applies to the tested versions, environment, and operation sequence; it cannot be directly generalized to other scheduling paths.

## Prerequisites and quick start

### Prerequisites

Docker must be running. Python 3.9+ and GNU Make are required. `make tools` installs kind, kubectl, Helm, and GitHub CLI into the project's `.bin/` directory after verifying their official checksums. It does not modify system tools.

ARC authentication uses `ARC_TOKEN` first, followed by `GH_TOKEN`, the Git-ignored `.state/github-token` file, and finally the current GitHub CLI login credentials. If using the token file, set its permissions to 600. The ARC credential must have the runner management permissions required for this repository; the official Helm documentation supports classic PATs. The credential used by GitHub CLI also needs permission to dispatch, read, and cancel workflows in this repository. Do not use a workflow's `GITHUB_TOKEN` as the ARC registration credential. ARC registration credentials are written to a Kubernetes Secret through standard input and are not included in command arguments, Git, or reports.

The `.github/workflows/probe.yml` workflow must already exist on the repository's default branch and be enabled. If it has not been published or has been disabled, automated tests fail before changing capacity. Running tests does not require creating a PR or a temporary branch.

### Create the environment and run tests

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

`make test` already includes the smoke scenario. To check only whether the environment can execute jobs, run `make smoke` separately. `make doctor` checks tools, Docker, and repository access; runner registration, scheduling, and execution are verified with actual jobs.

### Configure KUBECONFIG

`make up` generates a project-specific `.state/kubeconfig` when creating the cluster. From the project root, run the following to use the test cluster when invoking `kubectl` or `helm` directly in the current terminal:

```bash
export PATH="$PWD/.bin:$PATH"
export KUBECONFIG="$PWD/.state/kubeconfig"
kubectl config current-context   # Expected: kind-arc-test-<environment-id>
kubectl get nodes                # Confirm that the node is Ready
```

These environment variables apply only to the current terminal and its child processes. After opening a new terminal or reconnecting to the test host, enter the project root and run the configuration commands again. If the cluster does not exist yet, run `make up` first. The project's `make` operations explicitly specify the kubeconfig and context and do not depend on this environment variable. This configuration does not modify `~/.kube/config`.

`make down` deletes the project kubeconfig. If you configured the current terminal as above, run `unset KUBECONFIG` after cleanup to return to the default `~/.kube/config`.

## Automated scenarios and pass criteria

```bash
make test                       # smoke -> startup -> resume -> restart; stop on failure
make test SCENARIO=startup       # Pause with no running jobs, queue jobs, then restore capacity
make test SCENARIO=resume        # Running job + jobs queued while paused + recovery
make test SCENARIO=restart       # Intervention: explicitly recreate the listener after restoring capacity
```

Each scenario first completes a baseline job on an actual runner to verify authentication, scheduling, and execution.

| Scenario | Procedure and pass criteria |
| --- | --- |
| `smoke` | After the baseline job succeeds, submit three jobs. All must complete successfully on runners belonging to this environment. |
| `startup` | With no jobs running, set capacity to 0 and wait for active runner pods to disappear. Submit three jobs and confirm that they remain queued without assigned runners. After restoring capacity, all three original job IDs must complete successfully. |
| `resume` | Start a 240-second job, then set capacity to 0. Confirm that it finishes successfully while paused and that three additional jobs remain queued without assigned runners. After restoring capacity, all three original queued job IDs must complete successfully. |
| `restart` | Pause and queue jobs as in `startup`. After restoring capacity, explicitly recreate the listener. All three original job IDs must complete successfully. Record this result separately as an intervention test. |

`startup` also runs a baseline job first. It tests pause/resume with no jobs running, rather than the first startup of a new cluster. `restart` does not guarantee that jobs enter the queue while the listener is offline; an experiment covering that offline window requires a separate design.

Pause scenarios set both `minRunners` and `maxRunners` to 0 and wait for the configuration and runner pod count to converge. The three queued jobs are observed for at least 60 seconds by default. In `resume`, the script continues to verify that they are not scheduled while the running job finishes. Recovery writes `minRunners=0, maxRunners=3` through the official Helm chart.

The default timeout for the original jobs to complete after recovery is 600 seconds. The control script times queue waits independently of the GitHub job's `timeout-minutes`. If the recovery wait times out and all three original jobs are still queued without assigned runners, the script saves evidence before submitting one new job and observing for up to another 180 seconds. Even if that diagnostic step succeeds, the original scenario remains failed.

`make test` stops after the first failed scenario, so later scenarios may not run. A scenario that did not run has not passed. On exit, automated tests attempt to cancel this environment's unfinished probes and restore maximum capacity to 3, while retaining the cluster and evidence. A scenario failure or cleanup failure produces a nonzero exit code.

To adjust waits, invoke the control script directly. `--timeout` applies to job waits in individual phases, not to the total test duration:

```bash
python3 scripts/arc_test.py test --scenario resume --timeout 600 --hold-seconds 60
```

## Manual operations

| Command | Purpose |
| --- | --- |
| `make status` | Show the current environment, scale set name, listener UID, and recorded job states. |
| `make drain` | Set minRunners/maxRunners to 0 and wait for the scale set and listener capacity configuration to converge. |
| `make submit COUNT=3 DURATION=10` | Submit COUNT probe jobs, each running for DURATION seconds, and print the workflow URL. COUNT supports 1 through 3. |
| `make resume` | Set minRunners to 0 and restore maxRunners to 3; wait for capacity configuration convergence and listener readiness. |
| `make collect` | Collect cluster snapshots, job states, events, and logs under `.state/manual/`. |
| `make cancel` | Cancel only unfinished probes created and recorded by the script for this environment. |
| `make restart` | Explicitly recreate the listener as a diagnostic intervention. |
| `make down` | Uninstall ARC runners, then delete this environment's cluster, retaining evidence. |

`make drain` itself does not wait for the runner pod count to converge; automated tests perform an additional wait. Check runner pod states yourself during manual operations. Completion of `make resume` means only that configuration has been restored and the listener is Ready. You must still verify whether the original queued jobs start and complete successfully.

You can also run **ARC probe** manually from the GitHub Actions page. Supply the `scale_set` returned by `make status`, a unique `batch`, the job count, and the duration. Jobs created through the page are not automatically canceled by the script; cancel them through the page yourself.

The script permits only one environment-modifying command at a time. Use `make status` to observe an automated test while it is running. Cluster names use the `arc-test-` prefix and a random identifier. The kubeconfig is stored in `.state/`; the script specifies an explicit context and validates the cluster UID without modifying the default kubeconfig. Manual Helm operations are not protected by the script's concurrency lock and should be performed after automated tests have ended.

## Diagnostic experiments

### Establish a common queued-job state

Use consistent observation windows for the following manual experiments: at least 60 seconds with capacity 0, 600 seconds after restoring capacity, and 180 seconds after an additional intervention. Track time manually and run `make status` repeatedly; a single status query does not cover an observation window. Record any different durations used.

Recreate the same starting state for each intervention, testing only one additional intervention per run. Cancel remaining probes from the previous run and restore capacity first. After the baseline succeeds, pause the scale set:

```bash
make cancel
make resume
make smoke
make status                      # Record scale_set, context, and listener UID
make drain
make status                      # Record listener UID after capacity reaches 0
kubectl get pods -n arc-runners -o wide
```

Use the project KUBECONFIG configured above. Confirm that no runner pods are Pending, Running, or Unknown before submitting the original queued jobs:

```bash
make submit COUNT=3 DURATION=30  # These three jobs form the original queue
make status                      # Observe for at least 60 seconds: queued, no assigned runners
make collect                     # Save evidence while paused
make resume
make status                      # Observe for 600 seconds; check original job IDs and outcomes
make collect                     # Save evidence after capacity-only recovery; record the directory
```

If the original jobs start, continue observing whether they complete successfully, record the actual wait, and do not add a wakeup intervention. If they remain queued without assigned runners throughout the 600-second window, record that restoring capacity alone did not recover scheduling within the window, then choose one intervention below. If jobs receive runners but fail during execution, investigate that execution failure separately; it is not the queued-job wakeup problem.

### Submit a new job

With the common starting state still stalled, run:

```bash
make submit COUNT=1 DURATION=5   # Record submission time and workflow URL
make status                      # Observe for 180 seconds; check original job IDs and runner assignments
make collect                     # Save evidence after the intervention
```

If the original jobs retain their IDs, transition from queued to in_progress/completed, and receive runners, record that scheduling of the original queue recovered after a new job was submitted. Continue checking whether all jobs succeed. If they remain unassigned at the end of the window, record that no recovery was observed within this window.

Submitting a new job and establishing a runner session are separate events. Record the times of the new submission, listener assignment messages, runner pod creation, runner session establishment, and execution of the original jobs separately. Recovery after a new submission alone does not establish runner session creation as the trigger.

### Restart the listener

Recreate the common starting state, then run:

```bash
make status                      # Record listener UID before the intervention
make restart
make status                      # Confirm UID changed; observe the original queue for 180 seconds
make collect
```

Record whether the replacement listener becomes Ready and whether the original job IDs receive runners and start. A successful listener restart and recovery of the original queue are separate results.

### Increase minRunners

Recreate the common starting state, then increase minimum capacity to 1. Because `make resume` always sets `minRunners` to 0, use Helm directly for this experiment:

```bash
# Replace <scale_set> and <context>; use versions.ARC_VERSION from .state/state.json for <arc_version>
helm --kubeconfig .state/kubeconfig --kube-context <context> upgrade <scale_set> \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
  -n arc-runners --version <arc_version> --reuse-values --set minRunners=1,maxRunners=3 --wait --timeout 300s
kubectl get autoscalingrunnersets -n arc-runners -o yaml
make status                      # Observe the original queue for 180 seconds
make collect
```

Verify `minRunners=1, maxRunners=3` in the actual scale set configuration. The saved environment capacity shown by `make status` is not a substitute for checking the actual configuration. Record the order of runner creation, session establishment, and recovery of the original jobs without assuming the trigger in advance.

### Record results and restore the environment

For each run, record environment versions, scale set, original workflow/job IDs, listener UIDs, start and end times of observation windows, intervention time, execution outcomes, and evidence directories before and after the intervention. Preserve a separate timeline for the events within each experiment.

After collecting evidence, run:

```bash
make cancel
make resume                      # Reset to minRunners=0, maxRunners=3
```

This reset ends the experimental condition with minimum capacity 1. Establish the common starting state again for the next run. Temporarily changing EphemeralRunnerSet replicas is an additional low-level intervention; this document records its historical observation but does not provide a general procedure for it.

## Existing observations and limits of interpretation

The following summarizes observations from existing experiments with official ARC 0.15.0 and runner 2.338.0, rather than expected outcomes for every run. The manual listener-restart and minRunners experiments did not retain separate evidence directories. Experiment dates, complete observation durations, and shareable evidence have not yet been individually cataloged in this README; record them as described above when repeating the experiments.

| Operation | Existing observation | Notes |
| --- | --- | --- |
| Restore capacity only (`make resume`) | No recovery within the observation window | Original jobs remained queued without assigned runners; no corresponding assignment messages were observed in the listener. |
| Restart the listener (`make restart`) | No recovery within the observation window | Listener UID changed, but original jobs remained unassigned. No separate evidence directory was retained for this manual observation. |
| Submit a new job (`make submit`) | Original queue recovered | Original job IDs were unchanged and jobs succeeded. Assignment messages were observed before runner pods were created. |
| Increase minRunners to 1 | Original queue recovered | Recovery was observed after increasing minimum capacity. No separate evidence directory was retained for this manual observation, so it does not establish the trigger. |
| Temporarily create a runner (replicas patch) | Original queue recovered | Recovery was observed after a runner established a Broker session; preserve this experiment's own timeline. |

Evidence for the temporary-runner investigation is stored locally under `.state/investigation-38538867417/` and is not committed to this repository. Other entries without shareable evidence are historical observations, not independently verifiable experiment reports.

The new-job and temporary-runner experiments have different timelines and cannot both be attributed to a new runner session triggering recovery. Failure to recover after only restarting the listener also does not imply that the listener is irrelevant to recovery in all scenarios. None of these intervention results replaces acceptance of capacity-only recovery or determines a GitHub service root cause.

## Reports, evidence, and recovery after interruption

### Inspect reports and evidence

Automated test reports are written under `.state/results/<timestamp-id>/`, using UTC timestamps:

- `report.md`: scenario results, workflow links, and a version overview.
- `results.json`: detailed results for executed scenarios, listener UIDs before pausing and after restoring capacity, diagnostic outcomes, and cleanup errors.
- Resource snapshots and logs: controller, listener, and runner logs; Kubernetes events and resource configuration; GitHub job states; and other evidence. Pod status includes actual imageID values.

Evidence is collected periodically during tests. The failure handler also collects a snapshot under `before-diagnostic-<scenario>/`. Logs redact credentials and broker/session URLs; Secrets are not collected. Manual `make collect` snapshots are stored under `.state/manual/<UTC-timestamp>/`. Keep the paths printed by the command to distinguish evidence before and after interventions.

### Check the test harness

`make check` runs unit tests for the test harness and a Python compilation check. The GitHub **Check test harness** workflow also checks the syntax of the tool installation script; it does not run actual ARC scenarios. Run `make test` on the test host for those scenarios. It does not automatically connect to a host or modify other environments.

### Recover after interruption and clean up

On ordinary failure or Ctrl-C, automated tests attempt to cancel unfinished probes and restore capacity. Automatic recovery is not guaranteed after power loss, kill -9, or network interruption. After reconnecting, run `make collect` first to preserve evidence, then proceed as needed:

- To keep the original queue for further diagnosis, inspect `make status` first and avoid canceling the original jobs.
- To end the experiment while keeping the environment, run `make cancel` and `make resume`.
- To remove the test environment, run `make down`. If the project KUBECONFIG was configured, also run `unset KUBECONFIG`.

`make test` starts a new test; it does not continue from an interrupted step. The retained `.state/state.json` identifies resources belonging to this environment. Do not delete it manually.

## Official references

[Install ARC](https://docs.github.com/en/actions/tutorials/use-actions-runner-controller/get-started), [runner scale set configuration and draining](https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/deploy-runner-scale-sets), and [ARC 0.15.0](https://github.com/actions/actions-runner-controller/releases/tag/gha-runner-scale-set-0.15.0).
