"""Bounded read-only capture using existing remote Python and a stdin request.

This detects observed changes, not a filesystem transaction or hostile-host sandbox.
"""
import base64
import contextlib
import hashlib
import json
import os
from pathlib import PurePosixPath
import pwd
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 48 * 1024 * 1024
MAX_STDERR = 64 * 1024


def bounded_run(argv, cwd=None, data=None, timeout=30, env=None, check=True,
                capture_limits=(MAX_RESPONSE, MAX_STDERR)):
    """Bound both pipe buffers while draining them concurrently; kill on overflow."""
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as source:
        source.write(data or b'')
        source.seek(0)
        with subprocess.Popen(argv, cwd=cwd, stdin=source, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, start_new_session=True) as proc:
            buffers = [bytearray(), bytearray()]
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ, 0)
                    selector.register(proc.stderr, selectors.EVENT_READ, 1)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(argv, timeout)
                        for key, _ in selector.select(min(remaining, .25)):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            index = key.data
                            if len(buffers[index]) + len(chunk) > capture_limits[index]:
                                raise RuntimeError('Capture output limit exceeded')
                            buffers[index].extend(chunk)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                code = proc.wait(timeout=remaining)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise
    result = subprocess.CompletedProcess(argv, code, bytes(buffers[0]), bytes(buffers[1]))
    if check and code:
        raise RuntimeError('Capture command failed (%s): %s' % (
            code, result.stderr.decode(errors='replace')[-2000:]))
    return result


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate protocol key')
        result[key] = value
    return result


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {'version', 'nonce', 'repo', 'paths', 'contents'}:
        raise ValueError('Invalid capture request')
    if type(request['version']) is not int or request['version'] != 1 or type(request['contents']) is not bool:
        raise ValueError('Invalid capture request version/options')
    if not isinstance(request['nonce'], str) or not re.fullmatch(r'[0-9a-f]{32}', request['nonce']):
        raise ValueError('Invalid capture nonce')
    repo, paths = request['repo'], request['paths']
    if not isinstance(repo, str) or not repo.startswith('/') or any(c in repo for c in '\x00\n\r'):
        raise ValueError('Invalid capture root')
    if not isinstance(paths, list) or len(paths) > 100:
        raise ValueError('Capture supports at most 100 selected files')
    for name in paths:
        if not isinstance(name, str) or not name or len(name.encode()) > 4096:
            raise ValueError('Invalid capture path')
        path = PurePosixPath(name)
        if (not path.parts or path.is_absolute() or str(path) != name or
                any(p in ('.', '..', '.git') for p in path.parts) or any(c in name for c in '\x00\n\r')):
            raise ValueError('Invalid capture path: ' + repr(name))
    if len(set(paths)) != len(paths) or len(canonical(request)) > MAX_REQUEST:
        raise ValueError('Duplicate paths or oversized capture request')


def stamp(info, directory=False):
    common = (info.st_dev, info.st_ino, info.st_mode)
    return common if directory else common + (info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_file(root_fd, name):
    """Walk every selected component with no-follow descriptors; ENOENT only is absence."""
    trace = []
    with contextlib.ExitStack() as stack:
        parent = root_fd
        parts = PurePosixPath(name).parts
        for index, part in enumerate(parts):
            try:
                before = os.stat(part, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                trace.append(('missing', index))
                return {'exists': False}, None, trace
            if stat.S_ISLNK(before.st_mode):
                raise ValueError('Symlink is unsupported: ' + name)
            directory = index < len(parts) - 1
            if not (stat.S_ISDIR(before.st_mode) if directory else stat.S_ISREG(before.st_mode)):
                raise ValueError('Selected path is not a regular file/directory chain: ' + name)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory:
                flags |= os.O_DIRECTORY
            fd = os.open(part, flags, dir_fd=parent)
            stack.callback(os.close, fd)
            opened = os.fstat(fd)
            if stamp(before) != stamp(opened):
                raise RuntimeError('Source path changed while opening: ' + name)
            trace.append(stamp(opened, directory=directory))
            if directory:
                try:
                    os.stat('.git', dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError('Submodule or nested Git repository is unsupported: ' + name)
                parent = fd
                continue
            if opened.st_size > MAX_FILE:
                raise ValueError('File exceeds 4 MiB: ' + name)
            chunks, size = [], 0
            while True:
                chunk = os.read(fd, min(65536, MAX_FILE + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_FILE:
                    raise ValueError('File exceeds 4 MiB while reading: ' + name)
            if stamp(os.fstat(fd)) != stamp(opened) or size != opened.st_size:
                raise RuntimeError('Source changed while reading: ' + name)
            data = b''.join(chunks)
            # Match the prior test -x policy, including effective-user permissions.
            executable = os.access(part, os.X_OK, dir_fd=parent, effective_ids=True, follow_symlinks=False)
            mode = '100755' if executable else '100644'
            return {'exists': True, 'sha256': sha(data), 'mode': mode}, data, trace


def identity(deadline):
    def git(*args):
        remaining = min(5, deadline - time.monotonic())
        if remaining <= 0:
            raise RuntimeError('Capture deadline exceeded')
        return bounded_run(['git', '-c', 'core.fsmonitor=false', *args], timeout=remaining,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'),
                           capture_limits=(1024 * 1024, MAX_STDERR)).stdout.decode().strip()
    real = os.getcwd()
    if git('rev-parse', '--show-toplevel') != real:
        raise ValueError('Target must be the repository root')
    return {'host': socket.gethostname(), 'user': pwd.getpwuid(os.geteuid()).pw_name,
            'repo': real, 'head': git('rev-parse', 'HEAD'),
            'status': git('status', '--porcelain=v1', '--untracked-files=all')}


def collect(request):
    validate_request(request)
    deadline = time.monotonic() + 25
    real = os.getcwd()
    if os.path.realpath(request['repo']) != real:
        raise RuntimeError('Requested root differs from current directory')
    root_fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_stamp = stamp(os.fstat(root_fd), directory=True)

        def check_root():
            if (os.getcwd() != real or os.path.realpath(request['repo']) != real or
                    stamp(os.stat(real, follow_symlinks=False), directory=True) != root_stamp):
                raise RuntimeError('Source root path changed during capture')

        def sweep(retain):
            manifest, contents, traces, sizes, total = {}, {}, {}, {}, 0
            for name in request['paths']:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Capture deadline exceeded')
                state, data, trace = read_file(root_fd, name)
                manifest[name], traces[name] = state, trace
                if data is not None:
                    sizes[name] = len(data)
                    total += len(data)
                    if total > MAX_TOTAL:
                        raise ValueError('Selected files exceed 32 MiB total')
                    if retain:
                        contents[name] = base64.b64encode(data).decode('ascii')
            return manifest, contents, traces, sizes

        check_root()
        before = identity(deadline)
        first, contents, traces, sizes = sweep(request['contents'])
        second, _, after_traces, after_sizes = sweep(False)
        after = identity(deadline)
        check_root()
        if before != after or first != second or traces != after_traces or sizes != after_sizes:
            raise RuntimeError('Source changed while snapshotting')
        return {'version': 1, 'request_sha256': sha(canonical(request)), 'complete': True,
                'identity': before, 'manifest': first, 'contents': contents, 'sizes': sizes}
    finally:
        os.close(root_fd)


def decode_response(raw, request):
    validate_request(request)
    if len(raw) > MAX_RESPONSE:
        raise ValueError('Capture response too large')
    try:
        result = json.loads(raw, object_pairs_hook=unique_object)
        if (not isinstance(result, dict) or set(result) !=
                {'version', 'request_sha256', 'complete', 'identity', 'manifest', 'contents', 'sizes'}):
            raise ValueError('Invalid capture response envelope')
        if (type(result['version']) is not int or result['version'] != 1 or result['complete'] is not True or
                result['request_sha256'] != sha(canonical(request))):
            raise ValueError('Incomplete or mismatched capture response')
        who = result['identity']
        if (not isinstance(who, dict) or set(who) != {'host', 'user', 'repo', 'head', 'status'} or
                not all(isinstance(v, str) for v in who.values()) or not who['repo'].startswith('/') or
                not who['host'] or not who['user'] or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', who['head'])):
            raise ValueError('Invalid capture identity')
        manifest, contents, sizes = result['manifest'], result['contents'], result['sizes']
        if not all(isinstance(v, dict) for v in (manifest, contents, sizes)) or set(manifest) != set(request['paths']):
            raise ValueError('Missing or extra captured paths')
        existing = set()
        for name, state in manifest.items():
            if not isinstance(state, dict) or type(state.get('exists')) is not bool:
                raise ValueError('Invalid existence metadata')
            if not state['exists']:
                if state != {'exists': False}:
                    raise ValueError('Invalid absent-path metadata')
                continue
            existing.add(name)
            if (set(state) != {'exists', 'sha256', 'mode'} or state['mode'] not in ('100644', '100755') or
                    not isinstance(state['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', state['sha256'])):
                raise ValueError('Invalid file metadata')
        if set(sizes) != existing or set(contents) != (existing if request['contents'] else set()):
            raise ValueError('Missing or extra file payloads')
        if any(type(v) is not int or not 0 <= v <= MAX_FILE for v in sizes.values()) or sum(sizes.values()) > MAX_TOTAL:
            raise ValueError('Invalid captured sizes')
        files = {}
        for name, encoded in contents.items():
            if not isinstance(encoded, str) or len(encoded) > (MAX_FILE + 2) // 3 * 4:
                raise ValueError('Invalid file payload')
            data = base64.b64decode(encoded, validate=True)
            if len(data) != sizes[name] or sha(data) != manifest[name]['sha256']:
                raise ValueError('Captured content hash/size mismatch')
            files[name] = data
        return {'identity': who, 'manifest': manifest, 'files': files}
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError('Invalid/incomplete SSH capture response: ' + str(exc)) from exc


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError('Capture request too large')
        request = json.loads(raw, object_pairs_hook=unique_object)
        payload = canonical(collect(request))
        if len(payload) > MAX_RESPONSE:
            raise ValueError('Capture response too large')
        sys.stdout.buffer.write(payload + b'\n')
    except Exception as exc:
        print('SSH capture failed: %s: %s' % (type(exc).__name__, str(exc)[:1500]), file=sys.stderr)
        sys.exit(1)
