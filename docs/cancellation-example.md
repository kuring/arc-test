# Reusable workflow cancellation experiment

This example checks whether a reusable workflow caller needs `always()` to let
an internally protected job finish recovery after ordinary workflow cancellation.
It uses GitHub-hosted `ubuntu-24.04` runners and temporary files only. No ARC,
Kubernetes, AWS, repository secrets, or staging environment is accessed.

## Cases

| Caller job | Caller condition | Child job condition |
| --- | --- | --- |
| `caller-default` | Business condition (implicit `success()`) | `always() && inputs.protect_job` |
| `caller-always` | `always()` and the same business condition | `always() && inputs.protect_job` |
| `caller-control` | Business condition (implicit `success()`) | Ordinary condition (implicit `success()`) |

All callers depend on successful simulated `changes` and `deploy-fast` jobs.
The test step uses `success() && !cancelled()`. Each recovery step uses
`always()` and waits 330 seconds, deliberately longer than the documented
five-minute forced termination window for jobs marked for cancellation.

## Run

Publishing branch `codex/reusable-cancellation-example` starts the experiment
through its branch-specific push trigger. This allows testing without modifying
the default branch. `workflow_dispatch` also works once this workflow exists on
the default branch.

1. Find the **Reusable cancellation example** run for the exact pushed commit.
2. Use the run's jobs API to wait until all three `Hold simulated drained state`
   steps are `in_progress`. Do not cancel during setup or while any case is queued.
3. Send **one ordinary cancellation** to that exact run, using the UI or
   `gh run cancel RUN_ID --repo kuring/arc-test`. Do not force-cancel.
4. Wait for every original job to finish. Record cancellation time, original job
   IDs, step start/end timestamps, conclusions, and logs.
5. Check both the recovery step's conclusion and the `RECOVERY_FINISHED` marker.
   A canceled overall workflow is expected; its conclusion alone does not tell
   whether recovery completed.

If `caller-default / protected` completes its recovery beyond five minutes after
cancellation, outer `always()` was not necessary in this tested structure. If only
`caller-always / protected` completes, the experiment supports protecting the
caller. Report `caller-control` separately; do not assume its outcome in advance.

This tests cancellation during the main step only. It does not establish behavior
for cancellation during queueing/setup/recovery, repeated cancellation, runner
loss, real cluster operations, or a differently shaped dependency graph.

Reference: [GitHub workflow cancellation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation).
