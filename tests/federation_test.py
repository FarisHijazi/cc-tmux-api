"""Federated owner routing, using isolated HQ modules and mocked HTTP transport."""
import copy
import importlib.util
import io
import json
from urllib import error as urlerror
from urllib import parse as urlparse

import pytest

from cc_tmux_api import fleet

IDENTITY = ['server|birth|socket', '%7', '100', '200', 101, 'Thu Oct 08 09:00:00 2026']


def session():
    return {'identity': IDENTITY, 'session': 'toy', 'pane': '%7', 'title': 'Toy',
            'workdir': '/workspace', 'status': 'waiting', 'updated_at': 42,
            'revision': 'observed-screen', 'managed': False, 'command': 'claude',
            'has_question': True, 'attention_kind': 'question'}


@pytest.fixture
def cluster(monkeypatch):
    modules, configs, calls, writes = {}, {}, [], []
    for name in ('a', 'b', 'c', 'd'):
        spec = importlib.util.spec_from_file_location(f'cc_tmux_api._federation_{name}', fleet.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[name] = module
        configs[name] = [{'id': 'local', 'label': name.upper()}]
        monkeypatch.setattr(module, 'node_id', lambda name=name: name)
        monkeypatch.setattr(module, 'hosts', lambda name=name: copy.deepcopy(configs[name]))
        monkeypatch.setattr(module, '_observe_local', lambda value: value)

        def probe(host, request, name=name):
            if request['op'] == 'snapshot':
                return {'sessions': [session()] if name == 'c' else []}
            if request['op'] == 'detail':
                assert request['identity'] == IDENTITY
                return dict(session(), tail='terminal', messages=[{'role': 'assistant', 'text': 'hello'}], menu=None)
            writes.append((name, copy.deepcopy(request)))
            return {'ok': True, 'request_id': request['request_id'], 'refresh_required': True}
        monkeypatch.setattr(module, '_probe', probe)

    def http(host, endpoint, path, payload=None):
        target = urlparse.urlsplit(host['url']).hostname.split('.')[0]
        owner = modules[target]
        calls.append((target, endpoint, tuple(path), copy.deepcopy(payload)))
        if endpoint == '/api/sessions':
            return owner.snapshot(path=path)
        suffix = endpoint.removeprefix('/api/sessions/')
        if payload is None:
            return owner.detail(urlparse.unquote(suffix), path=path)
        identifier = urlparse.unquote(suffix.removesuffix('/action'))
        return owner.act(identifier, path=path, **payload)
    for module in modules.values():
        # Translate an HTTP owner's typed error into the caller's error class.
        def transport(host, endpoint, path, payload=None, module=module):
            try:
                return http(host, endpoint, path, payload)
            except Exception as exc:
                if hasattr(exc, 'status'):
                    raise module.FleetError(str(exc), exc.status) from exc
                raise
        monkeypatch.setattr(module, '_peer', transport)
    return modules, configs, calls, writes


def peer(name):
    return {'id': name, 'label': name.upper(), 'url': f'http://{name}.test'}


def test_three_node_cascade_routes_reads_and_writes_to_owner(cluster):
    nodes, configs, calls, writes = cluster
    configs['a'].append(peer('b'))
    configs['b'].append(peer('c'))
    result = nodes['a'].snapshot()
    row, = result['sessions']
    assert row['host'] == 'b/c/local'
    assert row['host_path'] == ['b', 'c', 'local']
    assert row['host_label'] == 'B / C / C'
    assert row['owner_node_id'] == 'c'
    assert row['origin_id'] == nodes['c'].snapshot()['sessions'][0]['origin_id']
    assert row['lifecycle'] == 'alive' and row['alive'] is True
    assert row['activity'] == 'waiting' and row['has_question'] is True
    detail = nodes['a'].detail(row['id'])
    assert detail['messages'] == [{'role': 'assistant', 'text': 'hello'}]
    assert detail['id'] == row['id']
    action = nodes['a'].act(row['id'], 'select', 'observed-screen', 'same-request', index=1)
    assert action['ok'] and action['id'] == row['id']
    assert len(writes) == 1
    owner, payload = writes[0]
    assert owner == 'c'
    assert payload['revision'] == 'observed-screen' and payload['request_id'] == 'same-request'
    assert payload['index'] == 1
    assert any(target == 'c' and path == ('a', 'b') for target, _, path, _ in calls)


def test_cycles_and_depth_rejected_before_any_cache_or_target_lock(cluster):
    nodes, configs, calls, writes = cluster
    node = nodes['a']
    node._cache_lock.acquire()
    try:
        with pytest.raises(node.FleetError) as err:
            node.snapshot(path=('a',))
        assert err.value.status == 508
        with pytest.raises(node.FleetError) as err:
            node.snapshot(path=tuple(f'n{i}' for i in range(8)))
        assert err.value.status == 508
        with pytest.raises(node.FleetError):
            node.detail('invalid', path=('a',))
        with pytest.raises(node.FleetError):
            node.act('invalid', 'key', 'revision', 'request', key='Enter', path=('a',))
    finally:
        node._cache_lock.release()
    assert calls == writes == []


def test_real_cascade_cycle_returns_explicit_unavailable_branch(cluster):
    nodes, configs, calls, _ = cluster
    configs['a'].append(peer('b'))
    configs['b'].append(peer('a'))
    result = nodes['a'].snapshot()
    cycle = next(h for h in result['hosts'] if h['id'] == 'b/a')
    assert not cycle['available'] and cycle['error_status'] == 508
    assert len(calls) == 2


def test_path_aware_cache_cannot_bypass_cycle_guard(cluster):
    nodes, configs, calls, _ = cluster
    configs['a'].append(peer('b'))
    nodes['a'].snapshot()
    calls.clear()
    nodes['a'].snapshot(path=('c',))
    assert calls[0][2] == ('c', 'a')
    with pytest.raises(nodes['a'].FleetError):
        nodes['a'].snapshot(path=('a',))


def test_diamond_deduplicates_owner_origin(cluster):
    nodes, configs, _, _ = cluster
    configs['a'].extend([peer('b'), peer('c')])
    configs['b'].append(peer('c'))
    result = nodes['a'].snapshot()
    assert len(result['sessions']) == 1
    assert result['sessions'][0]['owner_node_id'] == 'c'
    assert {'b/c/local', 'c/local'} <= {h['id'] for h in result['hosts']}


def test_peer_outage_preserves_rows_as_unreachable_not_ended(cluster, monkeypatch):
    nodes, configs, _, _ = cluster
    configs['a'].append(peer('c'))
    first = nodes['a'].snapshot()['sessions'][0]
    def offline(*args, **kwargs):
        raise nodes['a'].FleetError('offline', 503)
    monkeypatch.setattr(nodes['a'], '_peer', offline)
    nodes['a']._cache.pop('entries', None)
    result = nodes['a'].snapshot()
    row, = result['sessions']
    assert row['id'] == first['id'] and row['origin_id'] == first['origin_id']
    assert row['lifecycle'] == 'unreachable' and row['alive'] is None
    assert row['updated_at'] == first['updated_at']
    assert all(not h['available'] for h in result['hosts'] if h['id'].startswith('c'))


def test_forwarded_history_is_not_observed_again(cluster, monkeypatch):
    nodes, configs, _, _ = cluster
    configs['a'].append(peer('c'))
    seen = []
    def observe_local(value):
        seen.append(value)
        return value
    def observe_owner(value):
        value['sessions'][0].update(lifecycle='ended', alive=False, ended_at=50, last_seen=42)
        return value
    monkeypatch.setattr(nodes['a'], '_observe_local', observe_local)
    monkeypatch.setattr(nodes['c'], '_observe_local', observe_owner)
    result = nodes['a'].snapshot()
    assert seen[0]['sessions'] == []
    row, = result['sessions']
    assert row['lifecycle'] == 'ended' and row['ended_at'] == 50
    detail = nodes['a'].detail(row['id'])
    assert detail['read_only'] and detail['menu'] is None
    with pytest.raises(nodes['a'].FleetError, match='ended'):
        nodes['a'].act(row['id'], 'key', 'revision', 'ended-action', key='Enter')


def test_unknown_peer_delivery_is_never_retried(cluster, monkeypatch):
    nodes, configs, _, writes = cluster
    configs['a'].append(peer('c'))
    node = nodes['a']
    row = node.snapshot()['sessions'][0]
    attempts = []
    def ambiguous(*args, **kwargs):
        attempts.append(1)
        raise node.FleetError('delivery unknown', 503)
    monkeypatch.setattr(node, '_peer', ambiguous)
    for _ in range(2):
        with pytest.raises(node.FleetError, match='delivery unknown'):
            node.act(row['id'], 'key', 'same-revision', 'same-request', key='Enter')
    assert len(attempts) == 1 and writes == []


@pytest.mark.parametrize('host', [
    {'id': 'b', 'label': 'B', 'url': 'http://b.test', 'ssh': 'b'},
    {'id': 'b', 'label': 'B', 'url': 'file:///tmp/anything'},
    {'id': 'b', 'label': 'B', 'url': 'http://user:password@b.test'},
    {'id': 'b', 'label': 'B', 'url': 'http://b.test/api/sessions'},
])
def test_peer_configuration_validation(monkeypatch, host):
    monkeypatch.setenv('CC_TMUX_HOSTS', json.dumps([host]))
    with pytest.raises(fleet.FleetError):
        fleet.hosts()


def test_http_transport_forwards_context_and_refuses_redirects(monkeypatch):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.get_header('X-hq-path') == 'a,b'
            assert request.method == 'POST'
            assert json.loads(request.data)['request_id'] == 'original-request'
            raise urlerror.HTTPError(request.full_url, 302, 'redirect', {'Location': 'http://other.test'}, io.BytesIO(b''))
    def build(handler):
        assert handler.redirect_request(None, None, 302, '', {}, 'http://other.test') is None
        return Opener()
    monkeypatch.setattr(fleet.urlrequest, 'build_opener', build)
    with pytest.raises(fleet.FleetError) as err:
        fleet._peer(peer('b'), '/api/sessions/opaque/action', ('a', 'b'), {'request_id': 'original-request'})
    assert err.value.status == 502
    assert len(calls) == 1


def test_http_transport_preserves_owner_conflict(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise urlerror.HTTPError(request.full_url, 409, 'conflict', {}, io.BytesIO(b'{"detail":"Screen changed"}'))
    monkeypatch.setattr(fleet.urlrequest, 'build_opener', lambda _: Opener())
    with pytest.raises(fleet.FleetError, match='Screen changed') as err:
        fleet._peer(peer('b'), '/api/sessions/opaque/action', ('a',), {'request_id': 'request'})
    assert err.value.status == 409


def test_actual_history_integration_and_ended_detail(cluster, monkeypatch, tmp_path):
    from cc_tmux_api.session_history import SessionHistory
    nodes, configs, _, writes = cluster
    node = nodes['c']
    observer = SessionHistory(tmp_path / 'history')
    monkeypatch.setattr(node, '_observe_local', observer.observe)
    first = node.snapshot()['sessions'][0]
    assert first['lifecycle'] == 'alive'
    monkeypatch.setattr(node, '_probe', lambda *_: {'sessions': []})
    node._cache.pop('entries', None)
    ended = node.snapshot()['sessions'][0]
    assert ended['lifecycle'] == 'ended' and ended['ended_at'] is not None
    detail = node.detail(first['id'])
    assert detail['read_only'] is True
    assert detail['messages'] == [] and detail['menu'] is None
    with pytest.raises(node.FleetError, match='ended'):
        node.act(first['id'], 'key', 'old-revision', 'new-request', key='Enter')
    assert writes == []


def test_peer_failure_keeps_already_ended_rows_ended(cluster, monkeypatch):
    nodes, configs, _, _ = cluster
    configs['a'].append(peer('c'))
    def ended(value):
        value['sessions'][0].update(lifecycle='ended', alive=False)
        return value
    monkeypatch.setattr(nodes['c'], '_observe_local', ended)
    nodes['a'].snapshot()
    def offline(*args, **kwargs):
        raise nodes['a'].FleetError('offline', 503)
    monkeypatch.setattr(nodes['a'], '_peer', offline)
    nodes['a']._cache.pop('entries', None)
    row = nodes['a'].snapshot()['sessions'][0]
    assert row['lifecycle'] == 'ended' and row['alive'] is False


def test_cyclic_write_rejected_before_reentrant_target_lock(cluster):
    nodes, configs, calls, writes = cluster
    configs['a'].append(peer('b'))
    configs['b'].append(peer('a'))
    # A -> B -> A: the second A must reject path before resolving/locking.
    route_b = nodes['b']._encode('a', {'peer': 'unused-owner-id'})
    route_a = nodes['a']._encode('b', {'peer': route_b})
    with pytest.raises(nodes['a'].FleetError) as err:
        nodes['a'].act(route_a, 'key', 'revision', 'cyclic-request', key='Enter')
    assert err.value.status == 508
    assert writes == [] and len(calls) == 2


def test_eight_node_path_is_allowed_and_ninth_refused(cluster):
    nodes, _, _, _ = cluster
    assert nodes['a'].snapshot(path=tuple(f'n{i}' for i in range(7)))['node_id'] == 'a'
    with pytest.raises(nodes['a'].FleetError) as err:
        nodes['a'].snapshot(path=tuple(f'n{i}' for i in range(8)))
    assert err.value.status == 508


def test_http_transport_network_error_has_one_attempt(monkeypatch):
    attempts = []
    class Opener:
        def open(self, request, timeout):
            attempts.append(request)
            raise urlerror.URLError('connection closed after sending')
    monkeypatch.setattr(fleet.urlrequest, 'build_opener', lambda _: Opener())
    with pytest.raises(fleet.FleetError, match='delivery may be unknown'):
        fleet._peer(peer('b'), '/api/sessions/opaque/action', ('a',), {'request_id': 'request'})
    assert len(attempts) == 1


def test_local_censuses_serialize_collection_history_and_cache_commit(cluster, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    from cc_tmux_api.session_history import SessionHistory

    nodes, configs, _, _ = cluster
    node = nodes['c']
    history = SessionHistory(tmp_path / 'ordered-history')
    initial = {'hosts': [{'id': 'local', 'available': True}],
               'sessions': [node._decorate(configs['c'][0], session())]}
    history.observe(initial)
    old_collecting, release_old = Event(), Event()
    second_attempt, second_collecting = Event(), Event()
    old_committing, release_commit = Event(), Event()
    commits = []

    class ObservedLock:
        def __init__(self):
            self.lock = Lock()
            self.attempts = 0

        def __enter__(self):
            self.attempts += 1
            if self.attempts == 2:
                second_attempt.set()
            self.lock.acquire()

        def __exit__(self, *args):
            self.lock.release()

    class ObservedCache(dict):
        def __setitem__(self, key, value):
            if key == 'history' and not old_committing.is_set():
                old_committing.set()
                assert release_commit.wait(5), 'Old cache commit was not released'
            super().__setitem__(key, value)

    def probe(host, request):
        if not old_collecting.is_set():
            old_collecting.set()
            assert release_old.wait(5), 'Old collection was not released'
            return {'sessions': []}  # Earlier census did not see the session.
        second_collecting.set()
        return {'sessions': [session()]}  # The newer census sees it alive.

    def observe(value):
        result = history.observe(value)
        commits.append(result['sessions'][0]['lifecycle'])
        return result

    monkeypatch.setattr(node, '_local_observation_lock', ObservedLock())
    monkeypatch.setattr(node, '_cache', ObservedCache())
    monkeypatch.setattr(node, '_probe', probe)
    monkeypatch.setattr(node, '_observe_local', observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(node._local_snapshot, configs['c'])
        try:
            assert old_collecting.wait(2)
            new = pool.submit(node._local_snapshot, configs['c'])
            assert second_attempt.wait(2)
            assert not second_collecting.is_set()
            release_old.set()
            assert old_committing.wait(2)
            assert commits == ['ended']
            assert not second_collecting.is_set()  # Lock also spans cache commit.
            release_commit.set()
            assert old.result(timeout=2)['sessions'][0]['lifecycle'] == 'ended'
            assert new.result(timeout=2)['sessions'][0]['lifecycle'] == 'alive'
        finally:
            release_old.set()
            release_commit.set()
    assert commits == ['ended', 'alive']
    assert node._cache['history']['sessions'][0]['lifecycle'] == 'alive'
    persisted = history.observe({'hosts': [], 'sessions': []})['sessions'][0]
    assert persisted['lifecycle'] == 'unreachable' and persisted['ended_at'] is None


def test_cycle_guard_precedes_local_observation_lock(cluster):
    nodes, _, _, _ = cluster
    node = nodes['a']
    with node._local_observation_lock:
        with pytest.raises(node.FleetError) as err:
            node.snapshot(path=('a',))
    assert err.value.status == 508
