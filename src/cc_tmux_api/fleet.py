"""Configured-host fleet coordinator; independent of the worker registry.

Public synchronous API: snapshot(path=()), detail(id, path=()), act(id, action,
revision, request_id, text=None, key=None, index=None, path=()). Paths carry
X-HQ-Path's comma-separated node IDs; configure CC_TMUX_NODE_ID stably per HQ.
Option indexes are zero-based. HTTP peer IDs wrap the owner's opaque ID;
origin_id remains unchanged across relays and supports diamond deduplication.
The server should run these blocking calls in its thread pool. Controls never
retry a transport failure: delivery may already have happened. Request receipts
are process-local and retained until restart (bounded capacity, fail closed).
See docs/mobile-control-research.md for the trust boundary and adoption design.
"""
import base64
import copy
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from .fleet_probe import notify_library
from .vendor_amux import _detect_claude_status


class FleetError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


_cache_lock = threading.Lock()
_local_observation_lock = threading.Lock()
_guard = threading.Lock()
_locks = {}
_receipts = {}
_cache = {}


def native_backend():
    from .native_remote import get_backend
    return get_backend()


NATIVE_HOST = {'id': '@native', 'label': 'Remote Control', 'native': True}


def _native_row(item):
    row = dict(item, identity={'native': item['id']})
    result = _decorate(NATIVE_HOST, row)
    result.update(lifecycle=item['lifecycle'], alive=item['alive'])
    return result


def node_id():
    value = os.environ.get('CC_TMUX_NODE_ID') or socket.gethostname()
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        raise FleetError('CC_TMUX_NODE_ID must be a stable node identifier', 500)
    return value


def parse_path(value=None):
    """X-HQ-Path is a comma-separated list, never a client-selected URL."""
    if value is None or value == '':
        return ()
    if isinstance(value, str):
        value = tuple(value.split(','))
    if (not isinstance(value, (tuple, list)) or len(value) > 8 or
            any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', v) for v in value)):
        raise FleetError('Invalid or excessive HQ path', 508)
    return tuple(value)


def _context(path):
    path = parse_path(path)
    own = node_id()
    if len(path) >= 8 or own in path or len(set(path)) != len(path):
        raise FleetError('HQ federation cycle or eight-hop limit reached', 508)
    return path + (own,)


def hosts():
    try:
        value = json.loads(os.environ.get('CC_TMUX_HOSTS', '[{"id":"local","label":"Local"}]'))
    except ValueError as exc:
        raise FleetError('CC_TMUX_HOSTS must be a JSON array', 500) from exc
    if not isinstance(value, list) or not value or len(value) > 32:
        raise FleetError('Configure 1–32 fleet hosts', 500)
    seen = set()
    result = []
    for host in value:
        if (not isinstance(host, dict) or set(host) - {'id', 'label', 'ssh', 'url'} or
                not isinstance(host.get('id'), str) or
                not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', host['id']) or host['id'] in seen or
                not isinstance(host.get('label'), str) or not host['label']):
            raise FleetError('Invalid or duplicate fleet host configuration', 500)
        alias = host.get('ssh')
        if 'ssh' in host and 'url' in host:
            raise FleetError('A fleet host cannot configure both ssh and url', 500)
        if alias is not None and (not isinstance(alias, str) or
                                  not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.@-]{0,253}', alias)):
            raise FleetError('ssh must be a configured SSH alias, not options or a command', 500)
        if 'url' in host:
            value = host['url']
            try:
                parsed = urlparse.urlsplit(value) if isinstance(value, str) else None
                valid = (parsed and parsed.scheme in ('http', 'https') and parsed.hostname and
                         not parsed.username and not parsed.password and not parsed.query and
                         not parsed.fragment and parsed.path in ('', '/') and
                         not any(c.isspace() or ord(c) < 32 for c in value))
                if parsed:
                    parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise FleetError('Peer url must be an HTTP(S) origin without credentials', 500)
        seen.add(host['id'])
        result.append(dict(host))
    return result


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _peer(host, endpoint, path, payload=None):
    """Exactly one HTTP attempt. Owner applies terminal checks and dedup."""
    request = urlrequest.Request(host['url'].rstrip('/') + endpoint,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'X-HQ-Path': ','.join(path), 'Accept': 'application/json',
                 'Content-Type': 'application/json'}, method='POST' if payload is not None else 'GET')
    try:
        with urlrequest.build_opener(_NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise FleetError('Peer response exceeded size limit; delivery may be unknown', 502)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except urlerror.HTTPError as exc:
        message = f'Peer returned HTTP {exc.code}; redirects are not followed'
        try:
            body = json.loads(exc.read(16384))
            if isinstance(body.get('detail'), str):
                message = body['detail']
        except (ValueError, AttributeError):
            pass
        raise FleetError(message, exc.code if 400 <= exc.code <= 599 else 502) from exc
    except (urlerror.URLError, OSError, TimeoutError) as exc:
        raise FleetError('Peer unavailable; action delivery may be unknown. Do not retry.', 503) from exc
    except (ValueError, TypeError) as exc:
        raise FleetError('Invalid peer response; action delivery may be unknown', 502) from exc


def _probe(host, request):
    payload = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
    source = Path(__file__).with_name('fleet_probe.py')
    # Reuse the installed cc-notify reader verbatim on hosts without its CLI.
    # This is coordinator-owned code, never a path/source supplied by a request.
    library = notify_library()
    helper = library.parent.parent / 'bin' / 'cc-prompt-state' if library else None
    prompt_source = helper.read_text(encoding='utf-8') if helper and helper.is_file() else ''
    stdin = (source.with_name('vendor_amux.py').read_text(encoding='utf-8') + '\n' +
             'PROMPT_STATE_SOURCE = ' + repr(prompt_source) + '\n' + source.read_text(encoding='utf-8'))
    if host.get('ssh'):
        args = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', '-o',
                'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1', '--', host['ssh'],
                shlex.join(['python3', '-', payload])]
    else:
        args = [sys.executable, '-', payload]
    try:
        output = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FleetError('Host unavailable or timed out; action delivery may be unknown', 503) from exc
    if output.returncode:
        raise FleetError('Host probe failed: ' + output.stderr.strip()[:300], 503)
    try:
        response = json.loads(output.stdout)
        if 'error' in response:
            raise FleetError(response['error'], response.get('status', 503))
        return response['result']
    except (ValueError, KeyError, TypeError) as exc:
        raise FleetError('Host returned an invalid probe response; delivery may be unknown', 503) from exc


def _encode(host, identity):
    return base64.urlsafe_b64encode(json.dumps([host, identity], separators=(',', ':')).encode()).decode().rstrip('=')


def _resolve(identifier):
    if not isinstance(identifier, str) or len(identifier) > 32768:
        raise FleetError('Invalid session ID', 400)
    try:
        decoded = json.loads(base64.b64decode(identifier + '=' * (-len(identifier) % 4), altchars=b'-_', validate=True))
        if not isinstance(decoded, list) or len(decoded) != 2 or not isinstance(decoded[0], str):
            raise ValueError()
        host_id, identity = decoded
    except (ValueError, TypeError) as exc:
        raise FleetError('Invalid session ID', 400) from exc
    if host_id == NATIVE_HOST['id']:
        if (not isinstance(identity, dict) or set(identity) != {'native'} or
                not isinstance(identity['native'], str) or
                not re.fullmatch(r'cse_[A-Za-z0-9_-]{1,128}', identity['native'])):
            raise FleetError('Invalid native session route', 400)
        if native_backend() is None:
            raise FleetError('Native Remote Control is unavailable', 503)
        return NATIVE_HOST, identity
    host = next((h for h in hosts() if h['id'] == host_id), None)
    if not host:
        raise FleetError('Host is not configured', 404)
    if host.get('url'):
        if (not isinstance(identity, dict) or set(identity) != {'peer'} or
                not isinstance(identity['peer'], str) or not 1 <= len(identity['peer']) <= 24576):
            raise FleetError('Invalid peer session route', 400)
        return host, identity
    if (not isinstance(identity, list) or len(identity) != 6 or
            any(not isinstance(identity[i], str) for i in (0, 1, 2, 3, 5)) or
            type(identity[4]) is not int or not re.fullmatch(r'%\d+', identity[1])):
        raise FleetError('Invalid session ID', 400)
    return host, identity


def _decorate(host, item):
    result = dict(item)
    identity = result.pop('identity')
    identifier = _encode(host['id'], identity)
    result.update(id=identifier, host=host['id'], host_label=host['label'],
                  host_path=[host['id']], owner_node_id=node_id(), owner_session_id=identifier,
                  origin_id=node_id() + ':' + identifier, federated=False,
                  lifecycle='alive', alive=True)
    if result.get('status') == 'unknown' and 'tail' in result:
        result['status'] = {'active': 'working', 'waiting': 'waiting', 'idle': 'idle'}.get(
            _detect_claude_status(result['tail']), 'unknown')
    result['activity'] = {'working': 'running'}.get(result.get('status'), result.get('status', 'unknown'))
    return result


def _forward_row(host, item, peer_node=None):
    result = copy.deepcopy(item)
    original = result['id']
    result['id'] = _encode(host['id'], {'peer': original})
    result['host'] = host['id'] + '/' + str(result.get('host') or 'local')
    result['host_path'] = [host['id'], *result.get('host_path', [item.get('host') or 'local'])]
    result['host_label'] = host['label'] + ' / ' + str(result.get('host_label') or item.get('host') or 'Local')
    result['federated'] = True
    result.setdefault('owner_node_id', peer_node)
    result.setdefault('owner_session_id', original)
    result.setdefault('origin_id', str(peer_node or host['url']) + ':' + original)
    return result


def _host_snapshot(host):
    status = dict(id=host['id'], label=host['label'], available=False, updated_at=time.time(),
                  lifecycle='unreachable', host_path=[host['id']], federated=False)
    try:
        response = _probe(host, {'op': 'snapshot'})
        sessions = [_decorate(host, item) for item in response['sessions']]
        status.update(available=True, lifecycle='alive')
        return status, sessions
    except FleetError as exc:
        status.update(error=str(exc), error_status=exc.status)
        return status, []


_histories = {}


def _observe_local(value):
    # History is the sole persistence/lifecycle owner. Peer results never enter
    # this observer; their owning HQ has already annotated their lifecycle.
    from .session_history import SessionHistory
    directory = Path(os.environ.get('CC_TMUX_STATE_DIR', str(Path.home() / '.local/state/cc-tmux-api')))
    key = (node_id(), str(directory))
    with _guard:
        if key not in _histories:
            _histories[key] = SessionHistory(directory)
        history = _histories[key]
    return history.observe(value)


def _local_snapshot(config):
    # Serialize the whole census, not just its SQLite write: an older, slow
    # enumeration must never commit after a newer one and falsely end sessions.
    # Peer HTTP requests do not use this lock; cycle checks still precede it.
    with _local_observation_lock:
        local = [h for h in config if not h.get('url')]
        results = []
        if local:
            with ThreadPoolExecutor(max_workers=min(8, len(local))) as pool:
                results = list(pool.map(_host_snapshot, local))
        native = native_backend()
        if native is not None:
            results.append((dict(NATIVE_HOST, available=True, lifecycle='alive',
                                 host_path=[NATIVE_HOST['id']], federated=False),
                            [_native_row(row) for row in native.rows()]))
        result = _observe_local(dict(hosts=[r[0] for r in results],
                                    sessions=[s for r in results for s in r[1]], updated_at=time.time()))
        with _cache_lock:
            _cache['history'] = copy.deepcopy(result)
        return result


def _peer_snapshot(host, context):
    root = dict(id=host['id'], label=host['label'], host_path=[host['id']],
                available=False, lifecycle='unreachable', updated_at=time.time(), federated=True)
    key = (node_id(), host['id'], host['url'])
    try:
        response = _peer(host, '/api/sessions', context)
        if not isinstance(response.get('hosts'), list) or not isinstance(response.get('sessions'), list):
            raise FleetError('Invalid peer snapshot', 502)
        child_hosts = []
        for child in response['hosts']:
            row = dict(child)
            row.update(id=host['id'] + '/' + child['id'],
                       label=host['label'] + ' / ' + child['label'],
                       host_path=[host['id'], *child.get('host_path', [child['id']])], federated=True)
            child_hosts.append(row)
        sessions = [_forward_row(host, s, response.get('node_id')) for s in response['sessions']]
        root.update(available=True, lifecycle='alive', node_id=response.get('node_id'))
        value = ([root, *child_hosts], sessions)
        with _cache_lock:
            _cache.setdefault('peers', {})[key] = copy.deepcopy(value)
        return value
    except (FleetError, KeyError, TypeError) as exc:
        root.update(error=str(exc), error_status=exc.status if isinstance(exc, FleetError) else 502)
        # A reachability cache, not a second history store. Retain known peer
        # rows through outages without ever declaring their sessions ended.
        with _cache_lock:
            old = copy.deepcopy(_cache.get('peers', {}).get(key))
        if not old:
            return [root], []
        children = old[0][1:]
        for child in children:
            child.update(available=False, lifecycle='unreachable', error=root['error'])
        for session in old[1]:
            if session.get('lifecycle') != 'ended':
                session.update(lifecycle='unreachable', alive=None)
            session.update(stale=True, online=False)
        return [root, *children], old[1]


def snapshot(path=()):
    context = _context(path)  # Check BEFORE any cache/target lock or network IO.
    config = [] if os.environ.get('CC_TMUX_NATIVE_ONLY') == '1' else hosts()
    cache_key = (context, json.dumps(config, sort_keys=True))
    with _cache_lock:
        entry = _cache.get('entries', {}).get(cache_key)
        if entry and time.monotonic() - entry['at'] < 3:
            return copy.deepcopy(entry['value'])
    # Never hold a shared cache lock across a network request. A remote HQ can
    # route back here; its context must fail immediately rather than deadlock.
    peers = [h for h in config if h.get('url')]
    with ThreadPoolExecutor(max_workers=min(8, len(peers) + 1)) as pool:
        local_future = pool.submit(_local_snapshot, config)
        futures = [pool.submit(_peer_snapshot, h, context) for h in peers]
        local = local_future.result()
        results = [f.result() for f in futures]
    sessions = local['sessions'] + [s for r in results for s in r[1]]
    unique = {}
    for session in sessions:
        key = session.get('origin_id') or session['id']
        previous = unique.get(key)
        if previous is None or (previous.get('lifecycle') == 'unreachable' and session.get('lifecycle') != 'unreachable'):
            unique[key] = session
    result = dict(hosts=local['hosts'] + [h for r in results for h in r[0]],
                  sessions=list(unique.values()), updated_at=time.time(), node_id=node_id())
    with _cache_lock:
        entries = _cache.setdefault('entries', {})
        if len(entries) >= 128:
            entries.pop(next(iter(entries)))
        entries[cache_key] = {'at': time.monotonic(), 'value': copy.deepcopy(result)}
    return result


def _ended_summary(identifier, refresh=False):
    if refresh:
        value = _local_snapshot(hosts())
    else:
        with _cache_lock:
            value = copy.deepcopy(_cache.get('history', {}))
    session = next((s for s in value.get('sessions', []) if s['id'] == identifier and s.get('lifecycle') == 'ended'), None)
    if session is None:
        return None
    return dict(session, read_only=True, tail=session.get('tail') or '',
                messages=session.get('messages') or [], menu=None)


def detail(id, path=()):
    context = _context(path)
    host, identity = _resolve(id)
    if host.get('native'):
        return _native_row(native_backend().detail(identity['native']))
    if host.get('url'):
        result = _peer(host, '/api/sessions/' + urlparse.quote(identity['peer'], safe=''), context)
        if result.get('id') != identity['peer']:
            raise FleetError('Peer returned a different session identity', 502)
        return _forward_row(host, result, result.get('owner_node_id'))
    ended = _ended_summary(id)
    if ended:
        return ended
    try:
        return _decorate(host, _probe(host, {'op': 'detail', 'identity': identity}))
    except FleetError as exc:
        if exc.status == 409:
            ended = _ended_summary(id, refresh=True)
            if ended:
                return ended
        raise


def act(id, action, revision, request_id, text=None, key=None, index=None, path=(), response=None):
    context = _context(path)
    host, identity = _resolve(id)
    if not host.get("url") and _ended_summary(id):
        raise FleetError("Session has ended and is read-only", 409)
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise FleetError('request_id must contain 1–128 characters', 400)
    if not isinstance(revision, str) or not revision:
        raise FleetError('revision is required', 400)
    request = dict(op='act', identity=identity, action=action, revision=revision,
                   request_id=request_id, text=text, key=key, index=index)
    if response is not None:
        request['response'] = response
    fingerprint = json.dumps(request, sort_keys=True)
    # Lock the physical target, not user-supplied ID spelling or generation.
    target = (('@native', identity['native']) if host.get('native') else
              (host['url'], identity['peer']) if host.get('url') else
              (host.get('ssh', 'local'), identity[0], identity[1]))
    receipt_key = (host['id'], request_id)
    with _guard:
        lock = _locks.setdefault(target, threading.Lock())
    with lock:
        with _guard:
            previous = _receipts.get(receipt_key)
            if previous:
                if previous['payload'] != fingerprint:
                    raise FleetError('request_id was already used with a different payload', 409)
                if 'error' in previous:
                    message, status = previous['error']
                    raise FleetError(message, status)
                if 'result' in previous:
                    return copy.deepcopy(previous['result'])
                raise FleetError('Request is already in progress; do not resend with a new ID', 409)
            if len(_receipts) >= 10000:
                raise FleetError('Control receipt capacity reached; restart required', 503)
            receipt = {'payload': fingerprint}
            _receipts[receipt_key] = receipt
        try:
            if host.get('native'):
                result = native_backend().act(identity['native'], action, revision,
                                              request_id, text=text, response=response)
            elif host.get('url'):
                payload = {k: request[k] for k in ('action', 'revision', 'request_id', 'text', 'key', 'index')}
                if response is not None:
                    payload['response'] = response
                result = _peer(host, '/api/sessions/' + urlparse.quote(identity['peer'], safe='') + '/action',
                               context, payload=payload)
            else:
                result = _probe(host, request)
        except FleetError as exc:
            receipt['error'] = (str(exc), exc.status)
            raise
        except Exception as exc:
            receipt['error'] = ('Action delivery unknown; do not retry', 503)
            raise FleetError(*receipt['error']) from exc
        finally:
            with _cache_lock:
                _cache.pop('entries', None)
        result = dict(result, id=id)
        receipt['result'] = result
        return copy.deepcopy(result)
