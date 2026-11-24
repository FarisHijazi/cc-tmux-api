"""Native sessions use the same fleet guards, without terminal probes."""
import copy

import pytest

from cc_tmux_api import fleet


@pytest.fixture
def backend(monkeypatch, tmp_path):
    class Backend:
        calls = []

        def rows(self):
            return [self.detail('cse_toy')]

        def detail(self, sid):
            return dict(id=sid, session=sid, title='Toy', transport='native',
                        lifecycle='alive', alive=True, revision='r1', status='idle')

        def act(self, sid, action, revision, request_id, **values):
            self.calls.append((sid, action, values))
            if revision != 'r1':
                raise fleet.FleetError('Stale revision', 409)
            return {'ok': True}

    value = Backend()
    monkeypatch.setattr(fleet, 'native_backend', lambda: value)
    monkeypatch.setattr(fleet, '_cache', {})
    monkeypatch.setattr(fleet, '_receipts', {})
    monkeypatch.setattr(fleet, '_locks', {})
    monkeypatch.setattr(fleet, '_histories', {})
    monkeypatch.setenv('CC_TMUX_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('CC_TMUX_NODE_ID', 'toy')
    monkeypatch.setenv('CC_TMUX_NATIVE_ONLY', '1')
    monkeypatch.setattr(fleet, '_probe', lambda *a: pytest.fail('Native path invoked tmux'))
    return value


def test_native_snapshot_detail_action_share_guards(backend):
    row = fleet.snapshot()['sessions'][0]
    assert row['transport'] == 'native' and row['owner_node_id'] == 'toy'
    assert fleet.detail(row['id'])['session'] == 'cse_toy'
    result = fleet.act(row['id'], 'send', 'r1', 'unique-request', text='hello')
    assert result['ok']
    assert fleet.act(row['id'], 'send', 'r1', 'unique-request', text='hello') == result
    assert len(backend.calls) == 1
    with pytest.raises(fleet.FleetError, match='different payload'):
        fleet.act(row['id'], 'send', 'r1', 'unique-request', text='different')


def test_native_response_is_targeted_and_forwarded(backend, monkeypatch):
    row = fleet.snapshot()['sessions'][0]
    answer = {'request_id': 'permission-1', 'behavior': 'deny', 'message': 'No'}
    fleet.act(row['id'], 'respond', 'r1', 'response-1', response=answer)
    assert backend.calls[0] == ('cse_toy', 'respond', {'text': None, 'response': answer})
    monkeypatch.setenv('CC_TMUX_HOSTS', '[{"id":"peer","label":"Peer","url":"http://peer"}]')
    calls = []
    monkeypatch.setattr(fleet, '_peer', lambda host, endpoint, path, payload=None:
                        calls.append(copy.deepcopy(payload)) or {'ok': True})
    peer_id = fleet._encode('peer', {'peer': row['id']})
    fleet.act(peer_id, 'respond', 'r1', 'response-2', response=answer)
    assert calls[0]['response'] == answer


def test_native_identity_cannot_be_confused_with_terminal(backend):
    for identity in ({'native': '../../other'}, {'native': 'cse_x', 'peer': 'x'}, ['cse_x']):
        with pytest.raises(fleet.FleetError, match='native session route'):
            fleet.detail(fleet._encode('@native', identity))


def test_observer_reuses_exact_native_and_peer_routes(backend, monkeypatch):
    calls = []
    cursor = 'opaque-cursor'
    def observe(sid, after, wait):
        calls.append((sid, after, wait))
        return {'session_id': sid, 'events': [], 'cursor': cursor, 'reset': False}
    backend.observe = observe
    native_id = fleet._encode('@native', {'native': 'cse_toy'})
    assert fleet.observe(native_id, cursor, 10)['session_id'] == native_id
    assert calls == [('cse_toy', cursor, 10)]
    monkeypatch.setenv('CC_TMUX_HOSTS', '[{"id":"peer","label":"Peer","url":"http://peer"}]')
    forwarded = []
    def peer(host, endpoint, path, payload=None, **options):
        forwarded.append((endpoint, path, payload, options))
        return {'session_id': native_id, 'events': [], 'cursor': cursor, 'reset': False}
    monkeypatch.setattr(fleet, '_peer', peer)
    peer_id = fleet._encode('peer', {'peer': native_id})
    assert fleet.observe(peer_id, cursor, 10)['session_id'] == peer_id
    assert forwarded == [(f'/api/sessions/{native_id}/events?wait=10&after=opaque-cursor',
                          ('toy',), None, {'timeout': 30})]
    monkeypatch.setattr(fleet, '_peer', lambda *a, **k: {'session_id': 'another-session'})
    with pytest.raises(fleet.FleetError, match='different session identity'):
        fleet.observe(peer_id)


def test_observer_rejects_other_transports_and_cascade_cycles(backend, monkeypatch):
    monkeypatch.setattr(fleet, 'codex_backend', lambda: object())
    codex_id = fleet._encode('@codex', {'codex': '00000000-0000-0000-0000-000000000001'})
    with pytest.raises(fleet.FleetError, match='does not support'):
        fleet.observe(codex_id)
    native_id = fleet._encode('@native', {'native': 'cse_toy'})
    with pytest.raises(fleet.FleetError, match='cycle'):
        fleet.observe(native_id, path=('toy',))


def test_native_rows_take_the_machine_label_of_the_pane_running_their_bridge():
    rows = [
        dict(session='cse_a', transport='native', host='@native', host_label='Remote Control'),
        dict(session='cse_b', transport='native', host='@native', host_label='Remote Control'),
        dict(session='cse_c', transport='native', host='@native', host_label='Remote Control'),
        dict(session='work', transport='tmux', host='dev/local', host_label='Dev / Dev',
             bridge_session_id='cse_a', alive=True),
        dict(session='gone', transport='tmux', host='client/local', host_label='Client / Client',
             bridge_session_id='cse_b', alive=False),
        dict(session='x', transport='tmux', host='dev/local', host_label='Dev / Dev',
             bridge_session_id='cse_c', alive=True),
        dict(session='y', transport='tmux', host='work/local', host_label='Work / Work',
             bridge_session_id='cse_c', alive=True),
    ]
    labelled = fleet._label_native_machines(copy.deepcopy(rows))
    assert [r['host_label'] for r in labelled[:3]] == ['Dev / Dev', 'Remote Control', 'Remote Control']
    # Routing identity is unchanged; only the presentation label moves.
    assert all(r['host'] == '@native' for r in labelled[:3])
