from __future__ import annotations

import argparse
import traceback

from runtime_env import configure_runtime_environment


configure_runtime_environment()

from config import DEFAULT_BROWSER
from flow import FlowContext, FlowRunError, HybridFlow
from task_queue import claim_next_task, load_tasks, update_task


def run_one(roxy_profile_id: str = "", publish: bool = True, sync_monitor: bool = True) -> bool:
    task = claim_next_task(roxy_profile_id=roxy_profile_id)
    if not task:
        print("没有待处理任务。")
        return False

    print(f"[task] running {task.task_id} brand={task.brand} old_app_id={task.old_app_id}")
    try:
        account = task.roxy_profile_id or roxy_profile_id or None
        flow = HybridFlow(FlowContext(browser=DEFAULT_BROWSER, account=account))
        new_app_id = flow.run_brand(
            brand_name=task.brand,
            contact_email=None,
            activity_class_name=None,
            publish=publish,
            monitor_secret=None,
            sync_monitor=sync_monitor,
            adjust_brand_name=task.adjust_brand_name or task.brand,
        )
        update_task(task.task_id, status="success", new_app_id=new_app_id, last_error="")
        print(f"[task] success {task.task_id} new_app_id={new_app_id}")
        return True
    except Exception as exc:
        detail = traceback.format_exc()
        next_status = "failed" if task.attempts >= task.max_attempts else "pending"
        created_app_id = exc.created_app_id if isinstance(exc, FlowRunError) else ""
        update_task(task.task_id, status=next_status, new_app_id=created_app_id, last_error=detail)
        print(f"[task] {next_status} {task.task_id}\n{detail}")
        return False


def list_tasks() -> None:
    tasks = load_tasks()
    if not tasks:
        print("没有任务。")
        return
    for task in tasks:
        print(
            f"{task.status:10} attempts={task.attempts}/{task.max_attempts} "
            f"brand={task.brand} old={task.old_app_id} new={task.new_app_id} id={task.task_id}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Facebook repair task worker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List repair tasks")

    run = subparsers.add_parser("run-one", help="Run one pending repair task")
    run.add_argument("--roxy-profile-id", default="")
    run.add_argument("--no-publish", action="store_true")
    run.add_argument("--no-monitor", action="store_true")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "list":
        list_tasks()
        return
    if args.command == "run-one":
        run_one(
            roxy_profile_id=args.roxy_profile_id,
            publish=not args.no_publish,
            sync_monitor=not args.no_monitor,
        )
        return
    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
