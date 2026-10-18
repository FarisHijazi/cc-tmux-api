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
