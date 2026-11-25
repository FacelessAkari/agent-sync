"""Discover new packages in explicitly authorized, machine-local skill roots."""
from pathlib import Path
import stat

from . import core


CONFIG_NAME = 'skill-discovery.json'
NAMESPACES = {'codex', 'shared', 'antigravity'}
GENERATED = {'output', 'outputs', 'artifacts', 'temp', 'tmp'}


def entry_type(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        return None
    if stat.S_ISDIR(info.st_mode):
        return 'directory'
    if stat.S_ISREG(info.st_mode):
        return 'file'
    return None


def allowed_name(name, *, file=False):
    try:
        core._names(name, file=file)
    except core.Error:
        return False
    return name.casefold() not in GENERATED


def package_files(directory):
    core._safe(directory)
    for entry in sorted(directory.iterdir()):
        if not allowed_name(entry.name):
            continue
        kind = entry_type(entry)
        if kind == 'directory':
            yield from package_files(entry)
        elif kind == 'file' and allowed_name(entry.name, file=True):
            yield entry


def discover(document, selected, protected, mirror, bound_trees, read_source):
    if (not isinstance(document, dict) or set(document) != {'version', 'roots'}
            or type(document['version']) is not int or document['version'] != 1
            or not isinstance(document['roots'], list)):
        raise ValueError('Invalid skill discovery configuration')
    registered = {
        '/'.join(item['mirror'].split('/')[:2]).casefold() for item in selected
        if isinstance(item, dict) and isinstance(item.get('mirror'), str)
    }
    discovered = []
    for rule in document['roots']:
        if (not isinstance(rule, dict) or set(rule) not in (
                {'directory', 'namespace'}, {'directory', 'namespace', 'include'})
                or not isinstance(rule['directory'], str)
                or not isinstance(rule['namespace'], str)
                or rule['namespace'] not in NAMESPACES):
            raise ValueError('Invalid skill discovery root')
        directory = Path(rule['directory'])
        if (not directory.is_absolute() or '..' in directory.parts or directory == directory.parent
                or any(core._overlap(directory, target) for target in protected)):
            raise ValueError('Invalid or overlapping skill discovery directory')
        core._safe(directory)
        if not directory.is_dir():
            raise ValueError('Skill discovery directory must exist')
        include = rule.get('include')
        if 'include' in rule and (
                not isinstance(include, list)
                or any(not isinstance(name, str) or '/' in name or not allowed_name(name)
                       for name in include)
                or len({name.casefold() for name in include}) != len(include)):
            raise ValueError('Invalid skill discovery include list')
        namespace = rule['namespace']
        for package in sorted(directory.iterdir()):
            if include is not None and package.name not in include:
                continue
            if not allowed_name(package.name) or entry_type(package) != 'directory':
                continue
            prefix = namespace + '/' + package.name
            if prefix.casefold() in registered:
                continue
            guide = package / 'SKILL.md'
            try:
                if entry_type(guide) != 'file':
                    continue
            except FileNotFoundError:
                continue
            entries = []
            for path in package_files(package):
                parts = path.relative_to(package).parts
                target = mirror.joinpath(namespace, package.name, *parts)
                if not any(target.is_relative_to(tree) for tree in bound_trees):
                    raise ValueError('Discovered package has no matching skill binding')
                entries.append({
                    'source': str(path),
                    'mirror': target.relative_to(mirror).as_posix(),
                    'reviewed_sha256': core._digest(read_source(path, source=True)),
                })
            if not any(item['mirror'] == prefix + '/SKILL.md' for item in entries):
                raise ValueError('Skill entrypoint changed during discovery')
            discovered.extend(entries)
            registered.add(prefix.casefold())
    return discovered
