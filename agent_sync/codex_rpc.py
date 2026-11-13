"""Bounded, serial, newline-delimited JSON-RPC for a local Codex app-server."""
import json
import math
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from .core import Error, _overlap, _safe


DEFAULT_LIMIT = 16 * 1024 * 1024


def json_bytes(value):
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Error('无法编码有限 UTF-8 JSON') from exc


def json_loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    def bad_constant(value):
        raise ValueError('non-finite number')

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Error('无效 JSON：必须为 UTF-8、无重复键且无非有限数字') from exc


def bounded_read(path, limit=DEFAULT_LIMIT):
    if type(limit) is not int or limit < 1:
        raise Error('字节上限必须是正整数')
    path = _safe(path)
    try:
        with path.open('rb') as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise Error('无法读取指定本地文件') from exc
    if len(raw) > limit:
        raise Error('文件超过字节上限；拒绝读取，不会截断')
    return raw


def validate_argv(argv):
    if (not isinstance(argv, list) or not argv or len(argv) > 128
            or any(not isinstance(x, str) or not x or '\x00' in x for x in argv)):
        raise Error('Codex 命令必须是非空 JSON argv 字符串数组')
    if Path(argv[0]).suffix.lower() in {'.cmd', '.bat', '.ps1'}:
        raise Error('不能直接执行 .cmd/.bat/.ps1 包装；请提供原生 exe 或 node + codex.js argv')
    return list(argv)


def command_argv(repo, command_file=None):
    if command_file is not None:
        path = _safe(command_file)
        if _overlap(path, _safe(repo)):
            raise Error('命令配置必须位于同步内容仓库之外，禁止执行仓库内命令配置')
        return validate_argv(json_loads(bounded_read(path, 64 * 1024)))
    executable = shutil.which('codex')
    if not executable:
        raise Error('找不到 Codex；请指定仓库外的 --codex-command-file')
    return validate_argv([executable, 'app-server', '--stdio'])


class RPC:
    """One request at a time; no permission requests are approved.

    stdout has per-frame and aggregate byte limits. stderr is discarded, never
    included in exceptions. Both writes and reads share the request deadline.
    The direct child is terminated/reaped on every exit, including initialization
    failure. Use a direct app-server executable, not a detached launcher.
    """

    def __init__(self, argv, timeout=30, max_bytes=DEFAULT_LIMIT):
        self.argv = validate_argv(argv)
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout <= 0):
            raise Error('RPC timeout 必须是有限正数')
        if type(max_bytes) is not int or max_bytes < 1:
            raise Error('RPC 字节上限必须是正整数')
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.process = None
        self.reader = None
        self.writer = None
        self.inbox = queue.Queue(maxsize=64)
        self.stop = threading.Event()
        self.serial = 0
        self.received = 0
        self.sent = 0

    def __enter__(self):
        try:
            self.process = subprocess.Popen(
                self.argv, shell=False, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self.reader = threading.Thread(target=self._read_loop, daemon=True)
            self.reader.start()
            self.call('initialize', {'clientInfo': {
                'name': 'agent_sync_history', 'title': 'AgentSync History',
                'version': '1.0.0'}})
            self._send({'jsonrpc': '2.0', 'method': 'initialized'},
                       time.monotonic() + self.timeout)
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, OSError):
                raise Error('无法启动指定 Codex app-server；未回显命令或 stderr') from None
            raise

    def __exit__(self, *args):
        self.close()

    def _put(self, value):
        while not self.stop.is_set():
            try:
                self.inbox.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def _read_loop(self):
        try:
            while not self.stop.is_set():
                line = self.process.stdout.readline(self.max_bytes + 1)
                if not line:
                    self._put(Error('Codex RPC EOF：请求结果未知或进程已退出'))
                    return
                self.received += len(line)
                if len(line) > self.max_bytes or self.received > self.max_bytes:
                    self._put(Error('Codex RPC 输出超过字节上限；拒绝部分结果'))
                    return
                if not line.endswith(b'\n'):
                    self._put(Error('Codex RPC 帧缺少换行结束符'))
                    return
                self._put(json_loads(line))
        except (OSError, ValueError, Error):
            self._put(Error('Codex RPC 输出无效或读取失败'))

    def _send(self, message, deadline):
        raw = json_bytes(message)
        self.sent += len(raw)
        if len(raw) > self.max_bytes or self.sent > self.max_bytes:
            raise Error('Codex RPC 输入超过字节上限；未发送该请求')
        finished = threading.Event()
        failed = []

        def write():
            try:
                view = memoryview(raw)
                while view:
                    n = self.process.stdin.write(view)
                    if not n:
                        raise OSError('short write')
                    view = view[n:]
                self.process.stdin.flush()
            except (OSError, ValueError):
                failed.append(True)
            finally:
                finished.set()

        self.writer = threading.Thread(target=write, daemon=True)
        self.writer.start()
        if not finished.wait(max(0, deadline - time.monotonic())):
            raise Error('Codex RPC 写入超时；请求结果可能未知')
        self.writer.join()
        if failed:
            raise Error('Codex RPC 写入失败；请求结果可能未知')

    def call(self, method, params):
        if not self.process or self.process.poll() is not None:
            raise Error('Codex RPC 进程不可用')
        self.serial += 1
        if self.serial > 4096:
            raise Error('Codex RPC 请求数超过上限')
        request_id = self.serial
        deadline = time.monotonic() + self.timeout
        self._send({'jsonrpc': '2.0', 'id': request_id,
                    'method': method, 'params': params}, deadline)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Error('Codex RPC 超时：' + method + '；请求结果可能未知')
            try:
                message = self.inbox.get(timeout=remaining)
            except queue.Empty:
                raise Error('Codex RPC 超时：' + method + '；请求结果可能未知') from None
            if isinstance(message, Error):
                raise message
            if not isinstance(message, dict):
                raise Error('Codex RPC 响应必须为对象')
            if 'jsonrpc' in message and message['jsonrpc'] != '2.0':
                raise Error('Codex RPC 协议版本错误')
            if 'method' in message:
                if not isinstance(message['method'], str):
                    raise Error('Codex RPC method 类型错误')
                if 'id' in message:
                    # Reject all server requests, especially approval/permission requests.
                    self._send({'jsonrpc': '2.0', 'id': message['id'], 'error': {
                        'code': -32601, 'message': 'AgentSync does not handle server requests'}}, deadline)
                    raise Error('已拒绝 Codex 服务端请求；不会自动批准权限或执行工具')
                # Notifications are data only. Never interpret their content.
                continue
            if type(message.get('id')) is not int or message['id'] != request_id:
                raise Error('Codex RPC 响应 ID 不匹配')
            if ('error' in message) == ('result' in message):
                raise Error('Codex RPC result/error 结构错误')
            if 'error' in message:
                error = message['error']
                code = error.get('code') if isinstance(error, dict) else None
                code_text = str(code) if type(code) is int else 'unknown'
                raise Error('Codex RPC ' + method + ' 失败，code=' + code_text
                            + '；可能无权限、ID 不存在或方法不支持；未读取其他会话')
            return message['result']

    def close(self):
        self.stop.set()
        process = self.process
        if process is None:
            return
        # Terminate before closing pipes: a blocked writer must not hold cleanup hostage.
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=2)
        for thread in (self.writer, self.reader):
            if thread is not None:
                thread.join(timeout=1)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
