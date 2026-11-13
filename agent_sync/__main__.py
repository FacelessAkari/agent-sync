import argparse
import json
import sys

from . import core, projects, records
from .gitops import git_sync


def main(argv=None):
    core.configure_stdio()
    # Parse only the global prefix; the positional remainder is forwarded verbatim.
    # History and schedule own their subcommands and trailing options.
    route = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    route.add_argument("--repo", default=None)
    route.add_argument("tail", nargs=argparse.REMAINDER)
    prefix, unknown = route.parse_known_args(argv)
    if prefix.tail and prefix.tail[0] == "history" and not unknown:
        from .history import main as history_main
        return history_main(["--repo", "." if prefix.repo is None else prefix.repo,
                             *prefix.tail[1:]])
    if prefix.tail and prefix.tail[0] == "schedule" and not unknown:
        from .scheduler import main as scheduler_main
        tail = list(prefix.tail[1:])
        if prefix.repo is not None and not any(
                token == '--repo' or token.startswith('--repo=') for token in tail):
            tail.extend(['--repo', prefix.repo])
        return scheduler_main(tail)
    parser = argparse.ArgumentParser(
        prog="agent_sync",
        description="显式同步知识、项目代码、任务和交接；不发现个人资料或迁移原生会话。",
        allow_abbrev=False,
    )
    parser.add_argument("--repo", default=".", help="独立私人内容仓库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(parent, name, **kwargs):
        return parent.add_parser(name, allow_abbrev=False, **kwargs)

    command(sub, "init")
    add = command(sub, "add")
    add.add_argument("name")
    add.add_argument("--category", required=True, choices=sorted(core.CATEGORIES))
    add.add_argument("--kind", required=True, choices=["file", "tree"])
    add.add_argument("--path", required=True)
    bind = command(sub, "bind")
    bind.add_argument("name")
    bind.add_argument("path")
    for name in ("unbind", "remove"):
        command(sub, name).add_argument("name")
    command(sub, "plan")
    apply = command(sub, "apply")
    apply.add_argument("--direction", required=True, choices=["push", "pull"])
    command(sub, "git-sync")
    quick = command(sub, "quick-sync")
    quick.add_argument('--workspace', required=True)
    quick.add_argument('--check', action='store_true')
    shortcut = command(sub, "shortcut")
    shortcut.add_argument('--workspace', required=True)
    shortcut.add_argument('--platform', choices=['auto', 'windows', 'macos'], default='auto')
    shortcut.add_argument('--output')
    command(sub, "history", help="可见历史分页归档与文本上下文恢复（history --help）")
    command(sub, "schedule", help="安装、查看或移除助手计划任务（schedule --help）")

    project = command(sub, "project").add_subparsers(dest="action", required=True)
    add = command(project, "add")
    add.add_argument("id")
    add.add_argument("--remote", required=True)
    add.add_argument("--branch", required=True)
    bind = command(project, "bind")
    bind.add_argument("id")
    bind.add_argument("path")
    command(project, "list")
    for name in ("status", "sync"):
        command(project, name).add_argument("id")

    task = command(sub, "task").add_subparsers(dest="action", required=True)
    add = command(task, "add")
    add.add_argument("--id")
    add.add_argument("--project", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--note-file")
    listing = command(task, "list")
    listing.add_argument("--project")
    command(task, "show").add_argument("id")
    update = command(task, "set")
    update.add_argument("id")
    update.add_argument("--title")
    update.add_argument("--status", choices=sorted(records.STATUSES))
    update.add_argument("--note-file")

    handoff = command(sub, "handoff").add_subparsers(dest="action", required=True)
    create = command(handoff, "create")
    create.add_argument("--task", required=True)
    for name in ("summary", "next", "blockers", "verification"):
        create.add_argument("--" + name + "-file", required=True)
    listing = command(handoff, "list")
    listing.add_argument("--task")
    command(handoff, "show").add_argument("id")

    args = parser.parse_args(argv)
    if args.command in {'quick-sync', 'shortcut'}:
        tokens = list(sys.argv[1:] if argv is None else argv)
        before_command = tokens[:tokens.index(args.command)]
        if not any(token == '--repo' or token.startswith('--repo=') for token in before_command):
            parser.error('quick-sync/shortcut 必须明确指定 --repo')
    try:
        if args.command == "init":
            core.init_repo(args.repo)
            result = {"status": "initialized", "next": "add / bind / plan / project"}
        elif args.command == "add":
            core.add_group(args.repo, args.name, args.category, args.kind, args.path)
            result = {"status": "registered", "next": "bind，然后 plan"}
        elif args.command == "bind":
            core.bind(args.repo, args.name, args.path)
            result = {"status": "bound", "next": "plan；尚未复制内容或建立同步基线"}
        elif args.command in {"unbind", "remove"}:
            result = getattr(core, args.command)(args.repo, args.name)
        elif args.command == "plan":
            result = core.plan(args.repo)
        elif args.command == "apply":
            result = core.apply(args.repo, args.direction)
        elif args.command == "git-sync":
            result = git_sync(args.repo)
        elif args.command == 'quick-sync':
            from .local_bridge import run
            result, code = run(args.workspace, args.repo, check=args.check)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return code
        elif args.command == 'shortcut':
            from .shortcut import create
            result = create(args.workspace, args.repo, args.platform, args.output)
        elif args.command == "project":
            if args.action == "add":
                result = projects.add(args.repo, args.id, args.remote, args.branch)
            elif args.action == "bind":
                result = projects.bind(args.repo, args.id, args.path)
            elif args.action == "list":
                result = projects.list_projects(args.repo)
            elif args.action == "status":
                result = projects.status(args.repo, args.id)
            else:
                result = projects.sync(args.repo, args.id)
        elif args.command == "task":
            if args.action == "add":
                result = records.task_add(args.repo, args.project, args.title, args.note_file, args.id)
            elif args.action == "list":
                result = records.task_list(args.repo, args.project)
            elif args.action == "show":
                result = records.task_show(args.repo, args.id)
            else:
                result = records.task_set(args.repo, args.id, args.status, args.title, args.note_file)
        elif args.action == "create":
            result = records.handoff_create(
                args.repo, args.task, args.summary_file, args.next_file,
                args.blockers_file, args.verification_file,
            )
        elif args.action == "list":
            result = records.handoff_list(args.repo, args.task)
        else:
            result = records.handoff_show(args.repo, args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (core.Error, OSError, ValueError) as exc:
        print("AgentSync: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
