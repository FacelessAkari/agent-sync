"""Sync independent direct text files and reviewed skill packages while other files wait."""
from pathlib import Path

from . import core, gitops


def _plain_text(data):
    if data is None:
        return True
    if b'\0' in data:
        return False
    try:
        data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return False
    return True


def advance(repo, groups, bindings, snapshots, mirror_groups, guard, facts, counters, *, skill_scope=None):
    from .local_bridge import git_gate, stop
    from .records import validate_shared

    def check_git():
        fresh = git_gate(repo)
        if any(fresh[key] != facts[key] for key in ('head', 'branch', 'upstream', 'remote', 'ref', 'config_sha256')):
            raise core.Error("部分同步期间 Git 目标已变化")
        if gitops._git(repo, ['diff', '--quiet', 'HEAD', '--', 'manifest.json'],
                       '检查部分同步的清单', (0, 1)).returncode:
            raise core.Error("manifest 有本机修改，不能部分同步")

    check_git()
    validate_shared(repo)
    dirty = set(gitops._paths(repo, ['diff', '--no-renames', '--name-only', '-z', 'HEAD', '--'],
                              '找出部分提交的候选文件'))
    dirty.update(gitops._paths(repo, ['ls-files', '--others', '--exclude-standard', '-z'],
                               '检查部分同步的新文件'))
    skill_scope = {} if skill_scope is None else skill_scope
    scope, upload, skill_paths, devices = set(), {}, {}, {}
    pull_count = 0
    for group in groups:
        name = group['name']
        skill = name in mirror_groups and group['category'] == 'skills'
        direct = name not in mirror_groups and group['category'] in {'rules', 'memory', 'personas'}
        if name not in snapshots or not (direct or skill):
            continue
        remote, local, base = snapshots[name]
        for relative in sorted(remote.keys() | local.keys()):
            repo_path = core._target(Path(group['path']), relative).as_posix()
            action = core._action(remote.get(relative), local.get(relative), base.get(relative))
            key = (name, relative)
            if skill:
                if key not in skill_scope:
                    continue
                if action not in {'push', 'unchanged'} or skill_scope[key] != local.get(relative):
                    raise core.Error("已审阅技能包的镜像或仓库状态已变化")
                skill_paths[key] = repo_path
            else:
                if Path(repo_path).suffix.lower() not in {'.md', '.txt'} or action not in {'unchanged', 'push', 'pull'}:
                    continue
                if action == 'unchanged' and repo_path not in dirty and base.get(relative) == local.get(relative):
                    continue
            device = core._target(Path(bindings[name]), relative)
            left, right = core._bytes(device), core._bytes(repo / repo_path)
            if core._digest(left) != local.get(relative) or core._digest(right) != remote.get(relative):
                raise core.Error("部分同步计划中的文件发生变化，请重试")
            if direct and not (_plain_text(left) and _plain_text(right)):
                continue
            scope.add(key)
            if action == 'pull':
                pull_count += 1
            if action == 'push' or repo_path in dirty:
                upload[repo_path] = local.get(relative) if action == 'push' else remote[relative]
                devices[repo_path] = device
    if skill_scope.keys() != skill_paths.keys():
        raise core.Error("已审阅技能包存在未绑定或缺失的成员")
    if not scope:
        return
    gitops._stageable(repo, upload)
    core.apply(repo, 'both', scope=scope, expected=snapshots, identity=(groups, bindings))
    counters['direct_pull_files'] += pull_count
    if not upload:
        return
    check_git()
    if any(core._digest(core._bytes(devices[path])) != expected for path, expected in upload.items()):
        raise core.Error("部分上传前本机副本发生变化")
    guard.update({key: facts[key] for key in ('head', 'remote', 'ref', 'upstream')})
    try:
        result = gitops.git_sync(repo, paths=upload, expected_head=facts['head'])
    except gitops.UploadedPendingLocal as exc:
        counters['uploaded'] = True
        counters['partial_push_files'] = len(exc.paths)
        counters['partial_skill_files'] = len(set(skill_paths.values()).intersection(exc.paths))
        stop('git_review_required', '远端已接收部分文件；本机分支或索引发生变化，请核对后续接。')
    finally:
        guard.clear()
    if result['status'] == 'pushed':
        counters['uploaded'] = True
        counters['partial_push_files'] = result['files']
        if skill_paths:
            counters['partial_skill_files'] = len(set(skill_paths.values()).intersection(result['paths']))
