"""Existing Codex daemon sessions share fleet routing, receipts and lifecycle ownership."""
import copy

import pytest

from cc_tmux_api import fleet

SID = '01900123-4567-789a-bcde-0123456789ab'


@pytest.fixture
def backend(monkeypatch, tmp_path):
    class Backend:
        def __init__(self):
            self.calls = []
            self.present = True
            self.read_error = None
            self.write_error = None

        def rows(self):
            if self.read_error:
                raise self.read_error
            return [self.detail(SID)] if self.present else []

        def detail(self, sid):
            if self.read_error:
                raise self.read_error
            if sid != SID or not self.present:
                raise fleet.FleetError('Codex thread is not loaded', 409)
            return dict(id=sid, session=sid, title='Existing Codex task', transport='codex',
                        provider='codex', lifecycle='alive', alive=True, online=True,
                        revision='daemon-generation:turn-one', status='working',
                        native_control=None, messages=[{'id': 'native-item-one', 'role': 'assistant',
                                                       'text': 'Existing response'}])

        def act(self, sid, action, revision, request_id, **values):
            if revision != self.detail(sid)['revision']:
                raise fleet.FleetError('Codex session changed; refresh before acting', 409)
            self.calls.append((sid, action, revision, request_id, values))
            if self.write_error:
                raise self.write_error
            return {'ok': True, 'request_id': request_id}

    value = Backend()
    monkeypatch.setattr(fleet, 'codex_backend', lambda: value)
    monkeypatch.setattr(fleet, 'native_backend', lambda: None)
    for name in ('_cache', '_receipts', '_locks', '_histories'):
        monkeypatch.setattr(fleet, name, {})
    monkeypatch.setenv('CC_TMUX_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('CC_TMUX_NODE_ID', 'toy-node')
    monkeypatch.setenv('CC_TMUX_NATIVE_ONLY', '1')
    monkeypatch.setattr(fleet, '_probe', lambda *a: pytest.fail('Codex invoked terminal probe'))
    return value


def test_codex_discovery_preserves_native_identity_without_starting_a_session(backend):
    snapshot = fleet.snapshot()
    row = snapshot['sessions'][0]
    assert fleet._resolve(row['id']) == (fleet.CODEX_HOST, {'codex': SID})
    assert (row['host'], row['provider'], row['transport']) == ('@codex', 'codex', 'codex')
    assert row['activity'] == 'running' and row['owner_node_id'] == 'toy-node'
    assert fleet.detail(row['id'])['messages'] == backend.detail(SID)['messages']
    assert backend.calls == []


def test_codex_unknown_state_is_not_reclassified_using_claude_terminal_patterns(backend):
    row = fleet._codex_row(dict(backend.detail(SID), status='unknown', tail='esc to interrupt'))
    assert row['status'] == row['activity'] == 'unknown'


@pytest.mark.parametrize('action, fields', [
    ('send', {'text': 'Continue this task'}),
    ('interrupt', {}),
    ('respond', {'response': {'request_id': 'approval-one', 'behavior': 'deny'}}),
])
def test_codex_actions_share_targeted_receipts_without_duplicate_delivery(backend, action, fields):
    row = fleet.snapshot()['sessions'][0]
    result = fleet.act(row['id'], action, row['revision'], 'request-one', **fields)
    assert fleet.act(row['id'], action, row['revision'], 'request-one', **fields) == result
    assert len(backend.calls) == 1
    assert backend.calls[0][:4] == (SID, action, row['revision'], 'request-one')
    assert backend.calls[0][4] == {'text': fields.get('text'), 'response': fields.get('response')}
    with pytest.raises(fleet.FleetError, match='different payload'):
        fleet.act(row['id'], 'send', row['revision'], 'request-one', text='Different request')


def test_codex_unknown_delivery_receipt_cannot_reissue_the_write(backend):
    row = fleet.snapshot()['sessions'][0]
    backend.write_error = fleet.FleetError('Action delivery unknown; do not retry', 503)
    for _ in range(2):
        with pytest.raises(fleet.FleetError, match='delivery unknown'):
            fleet.act(row['id'], 'interrupt', row['revision'], 'unknown-write')
    assert len(backend.calls) == 1


def test_codex_stale_revision_is_rejected_before_write(backend):
    row = fleet.snapshot()['sessions'][0]
    with pytest.raises(fleet.FleetError, match='refresh before acting'):
        fleet.act(row['id'], 'send', 'previous-daemon', 'stale-write', text='Continue')
    assert backend.calls == []


@pytest.mark.parametrize('identity', [
    {'codex': '../../socket'}, {'codex': SID, 'peer': 'elsewhere'},
    {'native': SID}, [SID], {'codex': 'cse_session'}, {'codex': SID.upper()},
])
def test_codex_routes_reject_cross_transport_or_noncanonical_identity(backend, identity):
    with pytest.raises(fleet.FleetError, match='Invalid Codex session route'):
        fleet.detail(fleet._encode('@codex', identity))
    assert backend.calls == []


@pytest.mark.parametrize('failure', ['unloaded', 'unavailable'])
def test_codex_missing_loaded_thread_or_socket_outage_never_proves_end(backend, failure):
    original = fleet.snapshot()['sessions'][0]
    if failure == 'unloaded':
        backend.present = False
    else:
        backend.read_error = fleet.FleetError('Codex app server unavailable', 503)
    fleet._cache.clear()
    snapshot = fleet.snapshot()
    row = next(s for s in snapshot['sessions'] if s['id'] == original['id'])
    assert row['lifecycle'] == 'unreachable' and row['ended_at'] is None
    assert row['alive'] is None and row['revision'] is None and row['native_control'] is None
    assert 'messages' not in row
    if failure == 'unavailable':
        assert snapshot['hosts'][0]['available'] is False
        assert snapshot['hosts'][0]['error_status'] == 503
    with pytest.raises(fleet.FleetError):
        fleet.act(row['id'], 'send', original['revision'], 'offline-write', text='Continue')
    assert backend.calls == []


def test_codex_peer_routing_keeps_the_exact_owner_id_and_guarded_payload(backend, monkeypatch):
    local = fleet.snapshot()['sessions'][0]
    monkeypatch.setenv('CC_TMUX_HOSTS', '[{"id":"peer","label":"Peer","url":"http://peer"}]')
    calls = []

    def peer(host, endpoint, path, payload=None):
        calls.append((endpoint, copy.deepcopy(payload)))
        return {'ok': True} if payload is not None else local

    monkeypatch.setattr(fleet, '_peer', peer)
    peer_id = fleet._encode('peer', {'peer': local['id']})
    forwarded = fleet.detail(peer_id)
    assert forwarded['id'] == peer_id and forwarded['owner_session_id'] == local['id']
    assert forwarded['provider'] == 'codex' and forwarded['host_path'] == ['peer', '@codex']
    answer = {'request_id': 'approval-one', 'behavior': 'deny'}
    fleet.act(peer_id, 'respond', local['revision'], 'response-one', response=answer)
    assert calls[-1][1]['response'] == answer
    assert calls[-1][1]['request_id'] == 'response-one'
    assert calls[-1][1]['revision'] == local['revision']
    assert backend.calls == []
