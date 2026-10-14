#!/usr/bin/env python3
"""Independent official ARC tests. Python standard library only."""

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
REPO = "kuring/arc-test"
URL = "https://github.com/" + REPO
SYSTEM = "arc-systems"
RUNNERS = "arc-runners"
CHART = "oci://ghcr.io/actions/actions-runner-controller-charts/"


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def redact(text):
    text = re.sub(r"\b(?:gh[psuor]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)\b", "[TOKEN]", text)
    text = re.sub(r'(?i)Bearer\s+[^\s"\x27]+', "Bearer [TOKEN]", text)
    # Broker/session/signed URLs are not useful public evidence; retain hosts only.
    return re.sub(r'(https?://[^/\s"\x27]+)[^\s"\x27]*', r"\1/[REDACTED]", text)


def scrub(value):
    if isinstance(value, dict):
        if re.search(r"(?i)token|password|private.key|authorization", str(value.get("name", ""))):
            value = {**value, "value": "[REDACTED]"} if "value" in value else value
        return {
            k: (
                "[REDACTED]"
                if re.search(r"(?i)token|password|private.key|authorization", k)
                else scrub(v)
            )
            for k, v in value.items()
            if k != "managedFields"
        }
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return redact(value) if isinstance(value, str) else value


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def versions():
    result = dict(
        line.split("=", 1)
        for line in (ROOT / "versions.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    if os.environ.get("ARC_VERSION"):
        result["ARC_VERSION"] = os.environ["ARC_VERSION"]
    return result


def run(args, payload=None, timeout=90, allow_failure=False):
    env = os.environ.copy()
    env["PATH"] = str(ROOT / ".bin") + os.pathsep + env.get("PATH", "")
    if (STATE / "github-token").exists():
        env["GH_TOKEN"] = (STATE / "github-token").read_text().strip()
    p = subprocess.run(
        [str(a) for a in args],
        input=payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=ROOT,
        timeout=timeout,
    )
    if p.returncode and not allow_failure:
        raise RuntimeError(redact(f"{args[0]} failed ({p.returncode}): {p.stderr[-3000:]}"))
    return p.stdout if not p.returncode else ""


def api(path, method="GET", obj=None):
    endpoint = f"repos/{REPO}" + ("/" + path if path else "")
    args = ["gh", "api", "--method", method, endpoint]
    if obj is not None:
        args += ["--input", "-"]
    output = run(args, json.dumps(obj) if obj is not None else None)
    return json.loads(output) if output.strip() else None


def validate_state(s):
    ident = s.get("id", "")
    if not re.fullmatch(r"[a-f0-9]{12}", ident):
        raise RuntimeError("Invalid environment ID")
    expected = {
        "repo": REPO,
        "cluster": "arc-test-" + ident,
        "context": "kind-arc-test-" + ident,
        "scale_set": "arc-test-" + ident,
    }
    if any(s.get(k) != v for k, v in expected.items()):
        raise RuntimeError("Environment ownership mismatch")


def load():
    s = json.loads((STATE / "state.json").read_text())
    validate_state(s)
    return s


def save(s):
    validate_state(s)
    write_json(STATE / "state.json", s)


def kube(s, *args, **kwargs):
    return run(
        ["kubectl", "--kubeconfig", STATE / "kubeconfig", "--context", s["context"], *args],
        **kwargs,
    )


def get(s, resource, namespace=None):
    ns = ["-n", namespace] if namespace else ["-A"]
    return json.loads(kube(s, "get", resource, *ns, "-o", "json"))["items"]


def guard(s, require_arc=True):
    validate_state(s)
    uid = json.loads(kube(s, "get", "namespace", "kube-system", "-o", "json"))["metadata"]["uid"]
    if uid != s.get("cluster_uid"):
        raise RuntimeError("Cluster UID mismatch; refusing to mutate this cluster")
    if not require_arc:
        return
    sets = get(s, "autoscalingrunnersets", RUNNERS)
    if sets and (
        len(sets) != 1
        or sets[0]["metadata"]["name"] != s["scale_set"]
        or sets[0]["spec"]["githubConfigUrl"] != URL
    ):
        raise RuntimeError("Unexpected runner scale set")


def helm(s, *args):
    return run(
        ["helm", "--kubeconfig", STATE / "kubeconfig", "--kube-context", s["context"], *args],
        timeout=420,
    )


def wait(label, fn, timeout=180, interval=3):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(label)
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def live(items):
    return [x for x in items if not x["metadata"].get("deletionTimestamp")]


def ready_pod(p):
    return p.get("status", {}).get("phase") == "Running" and any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in p.get("status", {}).get("conditions", [])
    )


def listener_state(s, capacity=None):
    sets = live(get(s, "autoscalingrunnersets", RUNNERS))
    listeners = live(get(s, "autoscalinglisteners"))
    if len(sets) != 1 or len(listeners) != 1:
        return None
    ars, listener = sets[0], listeners[0]
    if capacity is not None:
        if ars["spec"].get("maxRunners") != capacity or ars["spec"].get("minRunners") != 0:
            return None
        # The official listener CR serializes zero-valued integer fields with omitempty.
        if listener["spec"].get("maxRunners", 0) != capacity:
            return None
    pods = live(get(s, "pods", listener["metadata"]["namespace"]))
    pods = [p for p in pods if p["metadata"]["name"] == listener["metadata"]["name"]]
    if len(pods) != 1 or not ready_pod(pods[0]):
        return None
    return {
        "ars_max": ars["spec"].get("maxRunners"),
        "listener_max": listener["spec"].get("maxRunners", 0),
        "listener_uid": pods[0]["metadata"]["uid"],
        "listener_name": pods[0]["metadata"]["name"],
    }


def capacity(s, maximum):
    guard(s)
    print(f"{timestamp()} capacity -> {maximum}", flush=True)
    helm(
        s,
        "upgrade",
        s["scale_set"],
        CHART + "gha-runner-scale-set",
        "-n",
        RUNNERS,
        "--version",
        s["versions"]["ARC_VERSION"],
        "--reuse-values",
        "--set",
        f"minRunners=0,maxRunners={maximum}",
        "--wait",
        "--timeout",
        "300s",
    )
    observation = wait(
        "runner/listener configuration convergence", lambda: listener_state(s, maximum)
    )
    s["capacity"] = maximum
    s.setdefault("capacity_history", []).append({"at": timestamp(), **observation})
    save(s)
    return observation


def doctor():
    for tool in ("docker", "kind", "helm", "kubectl", "gh"):
        output = run(
            [tool, "version"]
            if tool in ("docker", "kind", "helm")
            else [tool, "--version"]
            if tool == "gh"
            else [tool, "version", "--client"]
        )
        print(tool + ": " + output.splitlines()[0])
    run(["docker", "info"])
    info = api("")
    print(f"Repository: {info['full_name']}; default branch: {info.get('default_branch')}")
    if info["full_name"] != REPO:
        raise RuntimeError("Unexpected repository")
    print("Authentication and Docker are available. ARC readiness is checked by make up/smoke.")


def up():
    if (STATE / "state.json").exists():
        s = load()
        if s["versions"] != versions():
            raise RuntimeError("Version change requires make down followed by make up")
        guard(s, require_arc=False)
    else:
        ident = uuid.uuid4().hex[:12]
        s = {
            "id": ident,
            "repo": REPO,
            "cluster": "arc-test-" + ident,
            "context": "kind-arc-test-" + ident,
            "scale_set": "arc-test-" + ident,
            "versions": versions(),
            "created": timestamp(),
            "runs": [],
            "capacity_history": [],
        }
        save(s)
        print(f"Creating isolated cluster {s['cluster']}", flush=True)
        run(
            [
                "kind",
                "create",
                "cluster",
                "--name",
                s["cluster"],
                "--config",
                ROOT / "config/kind.yaml",
                "--image",
                s["versions"]["KIND_NODE_IMAGE"],
                "--kubeconfig",
                STATE / "kubeconfig",
                "--wait",
                "180s",
            ],
            timeout=420,
        )
        (STATE / "kubeconfig").chmod(0o600)
        s["cluster_uid"] = json.loads(kube(s, "get", "namespace", "kube-system", "-o", "json"))[
            "metadata"
        ]["uid"]
        save(s)
    print("Installing official ARC controller", flush=True)
    helm(
        s,
        "upgrade",
        "--install",
        "arc",
        CHART + "gha-runner-scale-set-controller",
        "-n",
        SYSTEM,
        "--create-namespace",
        "--version",
        s["versions"]["ARC_VERSION"],
        "--wait",
        "--timeout",
        "300s",
    )
    kube(s, "create", "namespace", RUNNERS, "--dry-run=client", "-o", "json")
    manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": RUNNERS}}
    kube(s, "apply", "-f", "-", payload=json.dumps(manifest))
    token = os.environ.get("ARC_TOKEN") or os.environ.get("GH_TOKEN")
    if not token and (STATE / "github-token").exists():
        token = (STATE / "github-token").read_text().strip()
    if not token:
        token = run(["gh", "auth", "token"]).strip()
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "arc-auth", "namespace": RUNNERS},
        "type": "Opaque",
        "data": {"github_token": base64.b64encode(token.encode()).decode()},
    }
    # Secret travels on stdin, never in arguments or evidence.
    kube(s, "apply", "-f", "-", payload=json.dumps(secret))
    print("Installing official runner scale set", flush=True)
    # Helm replaces lists rather than merging container entries. Include the official
    # name and command when pinning its image, so the runner pod layout stays valid.
    overrides = STATE / "runner-image.json"
    write_json(
        overrides,
        {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "runner",
                            "image": s["versions"]["RUNNER_IMAGE"],
                            "command": ["/home/runner/run.sh"],
                        }
                    ]
                }
            }
        },
    )
    helm(
        s,
        "upgrade",
        "--install",
        s["scale_set"],
        CHART + "gha-runner-scale-set",
        "-n",
        RUNNERS,
        "--version",
        s["versions"]["ARC_VERSION"],
        "-f",
        ROOT / "config/runners.yaml",
        "--set-string",
        f"runnerScaleSetName={s['scale_set']}",
        "-f",
        overrides,
        "--wait",
        "--timeout",
        "300s",
    )
    observation = wait("official listener ready", lambda: listener_state(s, 3), timeout=300)
    s["capacity"] = 3
    s["capacity_history"].append({"at": timestamp(), **observation})
    save(s)
    print(
        json.dumps({"cluster": s["cluster"], "scale_set": s["scale_set"], **observation}, indent=2)
    )


def workflow_ready():
    info = api("")
    ref = info.get("default_branch") or "main"
    try:
        workflow = api("actions/workflows/probe.yml")
    except RuntimeError as exc:
        raise RuntimeError(
            "probe.yml is not published on the default branch of arc-test yet"
        ) from exc
    if workflow["state"] != "active":
        raise RuntimeError("Probe workflow is disabled")
    return ref


def jobs(s, entry):
    value = api(f"actions/runs/{entry['run_id']}/jobs?per_page=100")
    result = value["jobs"]
    if value["total_count"] != len(result):
        raise RuntimeError("Incomplete GitHub job snapshot")
    if len(result) > entry["count"]:
        raise RuntimeError("Unexpected jobs in probe run")
    if len(result) == entry["count"]:
        ids = sorted(j["id"] for j in result)
        if entry.get("job_ids") and entry["job_ids"] != ids:
            raise RuntimeError("Original job IDs changed")
        if not entry.get("job_ids"):
            entry["job_ids"] = ids
            save(s)
    return result


def queue_ok(items, count):
    return len(items) == count and all(
        j["status"] == "queued" and not j.get("runner_id") and not j.get("runner_name")
        for j in items
    )


def success_ok(items, count, scale_set):
    return len(items) == count and all(
        j["status"] == "completed"
        and j.get("conclusion") == "success"
        and j.get("runner_name", "").startswith(scale_set + "-")
        for j in items
    )


def submit(s, count, seconds):
    guard(s)
    ref = workflow_ready()
    entry = {"batch": uuid.uuid4().hex, "count": count, "seconds": seconds, "created": timestamp()}
    s["runs"].append(entry)
    save(s)
    api(
        "actions/workflows/probe.yml/dispatches",
        "POST",
        {
            "ref": ref,
            "inputs": {
                "batch": entry["batch"],
                "scale_set": s["scale_set"],
                "count": str(count),
                "seconds": str(seconds),
            },
        },
    )

    def locate():
        runs = api("actions/workflows/probe.yml/runs?event=workflow_dispatch&per_page=100")[
            "workflow_runs"
        ]
        hits = [r for r in runs if r["display_title"] == "arc-test-" + entry["batch"]]
        if len(hits) > 1:
            raise RuntimeError("Duplicate probe batch")
        return hits[0] if hits else None

    item = wait("workflow dispatch registration", locate, timeout=120)
    entry["run_id"] = item["id"]
    entry["url"] = item["html_url"]
    save(s)
    wait("probe jobs visible", lambda: len(jobs(s, entry)) == count, timeout=120)
    print(entry["url"], flush=True)
    return entry


def complete(s, entry, timeout):
    def check():
        items = jobs(s, entry)
        bad = [j for j in items if j["status"] == "completed" and j.get("conclusion") != "success"]
        if bad:
            raise RuntimeError(
                "Probe job did not succeed: " + str([(j["id"], j.get("conclusion")) for j in bad])
            )
        return items if success_ok(items, entry["count"], s["scale_set"]) else None

    return wait("original probe jobs did not succeed before deadline", check, timeout=timeout)


def hold(s, entry, seconds):
    wait(
        "all held jobs queued without runners",
        lambda: queue_ok(jobs(s, entry), entry["count"]),
        timeout=120,
    )
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not queue_ok(jobs(s, entry), entry["count"]):
            raise RuntimeError("A held job was scheduled while capacity was zero")
        if listener_state(s, 0) is None:
            raise RuntimeError("Drain configuration/listener was not stable")
        time.sleep(min(3, max(0, deadline - time.monotonic())))


def cancel(s):
    # Only owned probe runs, never other workflows in this repository.
    for entry in s["runs"]:
        if not entry.get("run_id"):
            continue
        item = api(f"actions/runs/{entry['run_id']}")
        if (
            item["display_title"] != "arc-test-" + entry["batch"]
            or item["path"] != ".github/workflows/probe.yml"
        ):
            raise RuntimeError("Probe run ownership mismatch")
        if item["status"] != "completed":
            api(f"actions/runs/{entry['run_id']}/cancel", "POST")
            wait(
                "probe cancellation",
                lambda: api(f"actions/runs/{entry['run_id']}")["status"] == "completed",
                timeout=120,
            )


def restart(s):
    guard(s)
    before = wait("listener ready before restart", lambda: listener_state(s))
    listeners = live(get(s, "autoscalinglisteners"))
    kube(
        s,
        "delete",
        "pod",
        before["listener_name"],
        "-n",
        listeners[0]["metadata"]["namespace"],
        "--wait=true",
        "--timeout=90s",
        timeout=120,
    )
    after = wait(
        "replacement listener ready",
        lambda: (
            p if (p := listener_state(s)) and p["listener_uid"] != before["listener_uid"] else None
        ),
    )
    print(json.dumps({"before": before, "after": after}, indent=2))


def collect(s, destination):
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(destination / "environment.json", scrub(s))
    for resource in (
        "nodes",
        "pods",
        "events",
        "autoscalingrunnersets",
        "autoscalinglisteners",
        "ephemeralrunnersets",
        "ephemeralrunners",
    ):
        try:
            write_json(destination / (resource + ".json"), scrub(get(s, resource)))
        except Exception as exc:
            (destination / (resource + ".error")).write_text(redact(str(exc)))
    try:
        for pod in get(s, "pods"):
            ns, name = pod["metadata"]["namespace"], pod["metadata"]["name"]
            if ns not in (SYSTEM, RUNNERS):
                continue
            uid = pod["metadata"]["uid"][:8]
            logs = kube(
                s,
                "logs",
                name,
                "-n",
                ns,
                "--all-containers=true",
                "--timestamps=true",
                allow_failure=True,
            )
            (destination / f"{name}-{uid}.log").write_text(redact(logs))
    except Exception as exc:
        (destination / "logs.error").write_text(redact(str(exc)))
    # Snapshot data and logs are scrubbed; no Secrets are collected.
    for entry in s["runs"]:
        if entry.get("run_id"):
            try:
                write_json(
                    destination / f"jobs-{entry['run_id']}.json",
                    scrub(api(f"actions/runs/{entry['run_id']}/jobs?per_page=100")),
                )
            except Exception as exc:
                (destination / f"jobs-{entry['run_id']}.error").write_text(redact(str(exc)))


@contextlib.contextmanager
def observe(s, destination):
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            try:
                collect(s, destination)
            except Exception as exc:
                (destination / "observer.error").write_text(redact(str(exc)))
            stop.wait(10)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=120)
        if thread.is_alive():
            raise RuntimeError("Evidence collector did not stop")


def test(s, scenario, timeout, hold_seconds):
    workflow_ready()  # Fail before any mutation if the workflow has not been published.
    guard(s)
    cases = ["smoke", "startup", "resume", "restart"] if scenario == "all" else [scenario]
    destination = (
        STATE
        / "results"
        / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6])
    )
    destination.mkdir(parents=True, mode=0o700)
    results = {"started": timestamp(), "versions": s["versions"], "cases": [], "cleanup_errors": []}
    write_json(destination / "results.json", results)
    try:
        with observe(s, destination):
            for case in cases:
                result = {"scenario": case, "started": timestamp(), "status": "running"}
                results["cases"].append(result)
                held = None
                try:
                    capacity(s, 3)
                    complete(
                        s, submit(s, 1, 1), timeout
                    )  # Real runner, not just Pod/metric readiness.
                    if case == "smoke":
                        complete(s, submit(s, 3, 10), timeout)
                    else:
                        inflight = None
                        if case == "resume":
                            inflight = submit(s, 1, 240)
                            wait(
                                "inflight job running",
                                lambda: any(
                                    j["status"] == "in_progress" for j in jobs(s, inflight)
                                ),
                                timeout=timeout,
                            )
                        result["before"] = listener_state(s)
                        capacity(s, 0)
                        expected_active = 1 if inflight else 0
                        wait(
                            "drained runner pods converge",
                            lambda: (
                                len(
                                    [
                                        p
                                        for p in live(get(s, "pods", RUNNERS))
                                        if p.get("status", {}).get("phase")
                                        in ("Pending", "Running", "Unknown")
                                    ]
                                )
                                == expected_active
                            ),
                            timeout=180,
                        )
                        held = submit(s, 3, 10)
                        result["held_run"] = held["run_id"]
                        hold(s, held, hold_seconds)
                        if inflight:

                            def inflight_done():
                                if not queue_ok(jobs(s, held), 3) or listener_state(s, 0) is None:
                                    raise RuntimeError(
                                        "Held jobs escaped drain while inflight completed"
                                    )
                                items = jobs(s, inflight)
                                if any(
                                    j["status"] == "completed" and j.get("conclusion") != "success"
                                    for j in items
                                ):
                                    raise RuntimeError("Inflight job failed during drain")
                                return success_ok(items, 1, s["scale_set"])

                            wait("inflight completion during drain", inflight_done, timeout=timeout)
                        result["restored"] = capacity(s, 3)
                        if case == "restart":
                            restart(s)
                        complete(s, held, timeout)
                    result["status"] = "passed"
                except Exception as exc:
                    result.update(status="failed", error=redact(str(exc)))
                    collect(s, destination / ("before-diagnostic-" + case))
                    # Wakeup is a separately reported intervention; original failure remains failed.
                    if held and isinstance(exc, TimeoutError) and s.get("capacity") == 3:
                        try:
                            before_jobs = jobs(s, held)
                            if queue_ok(before_jobs, 3):
                                diagnostic = submit(s, 1, 1)
                                result["wakeup"] = {
                                    "fresh_run": diagnostic["run_id"],
                                    "submitted": timestamp(),
                                }
                                complete(s, held, min(timeout, 180))
                                result["wakeup"]["old_jobs_completed"] = True
                        except Exception as diagnostic_error:
                            result.setdefault("wakeup", {})["error"] = redact(str(diagnostic_error))
                finally:
                    result["finished"] = timestamp()
                    write_json(destination / "results.json", results)
                    # Pending jobs from one case must not leak into the next case.
                    cancel(s)
                if result["status"] == "failed":
                    break
    except (Exception, KeyboardInterrupt) as exc:
        results["control_error"] = redact(str(exc) or "Interrupted")
        for result in results["cases"]:
            if result["status"] == "running":
                result.update(status="failed", error=results["control_error"])
        raise
    finally:
        try:
            cancel(s)
        except Exception as exc:
            results["cleanup_errors"].append(redact(str(exc)))
        try:
            capacity(s, 3)
        except Exception as exc:
            results["cleanup_errors"].append(redact(str(exc)))
        results["finished"] = timestamp()
        write_json(destination / "results.json", results)
        collect(s, destination)
        lines = [
            "# Official ARC test",
            "",
            f"ARC: {s['versions']['ARC_VERSION']}",
            f"Runner: {s['versions']['RUNNER_IMAGE']}",
            f"Cluster: {s['cluster']}",
            "",
            "| Scenario | Result | Detail |",
            "|---|---|---|",
        ]
        lines += [
            f"| {c['scenario']} | {c['status']} | {c.get('error', '').replace('|', '/')} |"
            for c in results["cases"]
        ]
        lines += ["", "## Probe runs", ""] + [f"- {e['url']}" for e in s["runs"] if "url" in e]
        if results["cleanup_errors"]:
            lines += ["", "Cleanup errors: " + str(results["cleanup_errors"])]
        if results.get("control_error"):
            lines += ["", "Control error: " + results["control_error"]]
        (destination / "report.md").write_text("\n".join(lines) + "\n")
        print(f"Report: {destination / 'report.md'}", flush=True)
    if any(c["status"] != "passed" for c in results["cases"]) or results["cleanup_errors"]:
        raise RuntimeError("Test failed; inspect report and retained environment")


def down(s):
    guard(s)
    cancel(s)
    releases = json.loads(helm(s, "list", "-A", "-o", "json"))
    if any(r["name"] == s["scale_set"] and r["namespace"] == RUNNERS for r in releases):
        helm(s, "uninstall", s["scale_set"], "-n", RUNNERS, "--wait", "--timeout", "300s")
    wait(
        "runner/listener resources deleted",
        lambda: not get(s, "autoscalingrunnersets") and not get(s, "autoscalinglisteners"),
        timeout=300,
    )
    collect(s, STATE / "retired" / s["id"])
    if any(r["name"] == "arc" and r["namespace"] == SYSTEM for r in releases):
        helm(s, "uninstall", "arc", "-n", SYSTEM, "--wait", "--timeout", "180s")
    run(
        ["kind", "delete", "cluster", "--name", s["cluster"], "--kubeconfig", STATE / "kubeconfig"],
        timeout=180,
    )
    if s["cluster"] in run(["kind", "get", "clusters"]).splitlines():
        raise RuntimeError("Cluster deletion incomplete")
    (STATE / "state.json").unlink()
    (STATE / "kubeconfig").unlink(missing_ok=True)
    print("Owned ARC resources and kind cluster removed; evidence retained.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "command",
        choices=[
            "doctor",
            "up",
            "smoke",
            "test",
            "drain",
            "resume",
            "submit",
            "status",
            "collect",
            "cancel",
            "restart",
            "down",
        ],
    )
    p.add_argument(
        "--scenario", choices=["all", "smoke", "startup", "resume", "restart"], default="all"
    )
    p.add_argument("--count", type=int, choices=[1, 2, 3], default=3)
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--hold-seconds", type=int, default=60)
    args = p.parse_args()
    if not 1 <= args.seconds <= 600 or args.timeout <= 0 or args.hold_seconds <= 0:
        p.error("Invalid duration")
    STATE.mkdir(exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    lock = (STATE / "control.lock").open("a")
    if args.command not in ("doctor", "status", "collect"):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another mutating operation is running") from None
    if args.command == "doctor":
        doctor()
    elif args.command == "up":
        up()
    elif args.command == "down" and not (STATE / "state.json").exists():
        print("No owned environment to delete; evidence retained.")
    else:
        s = load()
        guard(s)
        if args.command in ("smoke", "test"):
            test(
                s,
                "smoke" if args.command == "smoke" else args.scenario,
                args.timeout,
                args.hold_seconds,
            )
        elif args.command in ("drain", "resume"):
            capacity(s, 0 if args.command == "drain" else 3)
        elif args.command == "submit":
            submit(s, args.count, args.seconds)
        elif args.command == "restart":
            restart(s)
        elif args.command == "cancel":
            cancel(s)
        elif args.command == "down":
            down(s)
        elif args.command == "status":
            print(json.dumps({"environment": scrub(s), "listener": listener_state(s)}, indent=2))
            for entry in s["runs"]:
                if "run_id" in entry:
                    print(
                        json.dumps({"run": entry["url"], "jobs": scrub(jobs(s, entry))}, indent=2)
                    )
        elif args.command == "collect":
            destination = (
                STATE / "manual" / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            )
            collect(s, destination)
            print(destination)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(
            redact(
                str(error)
                or "Interrupted; make cancel and make resume recover the retained environment"
            ),
            file=sys.stderr,
        )
        sys.exit(1)
