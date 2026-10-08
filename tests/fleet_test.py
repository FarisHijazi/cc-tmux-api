"""Fleet contracts tested without connecting to, or writing into, real panes."""
import base64
import json
import shlex
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cc_tmux_api import fleet
from cc_tmux_api import fleet_probe as probe

REAL_PROBE = fleet._probe

SID = '11111111-2222-4333-8444-555555555555'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
IDENTITY = ['123|456|/tmp/test-tmux', '%121', '100', '456', 101, 'Thu Oct 08 09:00:00 2026']
SINGLE = '''────────────────
 ☐ Colour

Pick a colour?
❯ 1. Green
     Fresh leaves
  2. Blue
     Clear sky
  3. Type something.
Enter to select · Esc to cancel
'''
MULTI = '''────────────────
← ☒ Items  ✔ Submit →

Pack which items?
❯ 1. [ ] Map
     Paper map
  2. [x] Rope
     Strong rope
Space to select · Enter to confirm · Esc to cancel
'''
REVIEW = '''Review your answers
Ready to submit your answers?
❯ 1. Submit answers
  2. Cancel
Enter to submit · Esc to cancel
'''


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv('CC_TMUX_HOSTS', raising=False)
    monkeypatch.setattr(fleet, '_receipts', {})
    monkeypatch.setattr(fleet, '_locks', {})
    monkeypatch.setattr(fleet, '_cache', {'at': 0, 'config': None, 'value': None})
    monkeypatch.setattr(fleet, '_observe_local', lambda value: value)
    monkeypatch.setattr(probe, 'DEADLINE', None)
    # A missed mock fails closed instead of talking to the developer's tmux.
    def forbidden(*args, **kwargs):
        raise AssertionError('Unexpected real host command')
    monkeypatch.setattr(probe, 'run', forbidden)
    monkeypatch.setattr(fleet, '_probe', forbidden)
    monkeypatch.setattr(probe, 'notify_library', lambda: None)
    monkeypatch.setattr(fleet, 'notify_library', lambda: None)
    monkeypatch.setattr(probe.shutil, 'which', lambda _: None)


def item(tail=SINGLE):
    return dict(identity=IDENTITY, revision='r1', pane='%121', session='toy',
                tail=tail, menu=probe.parse_menu(tail), _pane={'pane_in_mode': '0'},
                _proc={'state': 'S+'})


def test_identity_never_resolves_session_prefix(monkeypatch):
    identifier = fleet._encode('local', IDENTITY)
    assert fleet._resolve(identifier)[1] == IDENTITY
    with pytest.raises(fleet.FleetError):
        fleet._resolve('toy')
    with pytest.raises(fleet.FleetError, match='not configured'):
        fleet._resolve(fleet._encode('other', IDENTITY))
    invalid = IDENTITY.copy()
    invalid[1] = 'toy; run-shell bad'
    with pytest.raises(fleet.FleetError):
        fleet._resolve(fleet._encode('local', invalid))


def test_offline_explicit_and_cached(monkeypatch):
    monkeypatch.setenv('CC_TMUX_HOSTS', '[{"id":"work","label":"Work","ssh":"work"},{"id":"local","label":"Local"}]')
    calls = []
    def fake(host, request):
        calls.append(host['id'])
        if host['id'] == 'work':
            raise fleet.FleetError('offline', 503)
        return {'sessions': []}
    monkeypatch.setattr(fleet, '_probe', fake)
    first = fleet.snapshot()
    assert first['hosts'][0]['available'] is False
    assert first['hosts'][0]['error'] == 'offline'
    assert first['hosts'][1]['available'] is True
    first['hosts'].clear()
    assert len(fleet.snapshot()['hosts']) == 2
    assert len(calls) == 2


def test_hosts_collected_concurrently(monkeypatch):
    monkeypatch.setenv('CC_TMUX_HOSTS', '[{"id":"a","label":"A"},{"id":"b","label":"B"}]')
    barrier = threading.Barrier(2)
    def fake(*args):
        barrier.wait(timeout=2)
        return {'sessions': []}
    monkeypatch.setattr(fleet, '_probe', fake)
    assert all(h['available'] for h in fleet.snapshot()['hosts'])


@pytest.mark.parametrize('alias', ['-oProxyCommand=bad', 'work;touch /tmp/no', '$(bad)', 'work\nother'])
def test_host_command_injection_rejected(monkeypatch, alias):
    monkeypatch.setenv('CC_TMUX_HOSTS', json.dumps([{'id': 'work', 'label': 'Work', 'ssh': alias}]))
    with pytest.raises(fleet.FleetError):
        fleet.hosts()


def test_ssh_source_and_payload_are_quoted(monkeypatch):
    seen = []
    def fake(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '{"result":{"ok":true}}', '')
    monkeypatch.setattr(fleet.subprocess, 'run', fake)
    request = {'text': "hi ' $(touch bad)\n`bad`", 'op': 'act'}
    assert REAL_PROBE({'id': 'work', 'ssh': 'work'}, request)['ok']
    args, kwargs = seen[0]
    assert 'BatchMode=yes' in args and 'ConnectTimeout=4' in args
    remote = shlex.split(args[-1])
    assert remote[:2] == ['python3', '-']
    assert json.loads(base64.urlsafe_b64decode(remote[2])) == request
    assert 'def perform' in kwargs['input']
    compile(kwargs['input'], '<remote-probe>', 'exec')
    assert kwargs['timeout'] == 20


def test_process_parent_uuid_not_nested_child():
    pane = {'pane_pid': '100', 'pane_tty': '/dev/ttys001', 'pane_current_command': '2.1.294'}
    shell = dict(pid=100, parent=1, tty='ttys001', state='S', born='birth', comm='zsh', args='zsh')
    parent = dict(shell, pid=101, parent=100, comm='2.1.294', args=f'claude --session-id {SID}')
    child = dict(parent, pid=102, parent=101, args=f'claude -r {OTHER}')
    chosen = probe.target_process(pane, {100: shell, 101: parent, 102: child})
    assert chosen['pid'] == 101
    assert chosen['nested'] is True
    assert probe.argv_sid(chosen) == SID
    pane['pane_current_command'] = 'zsh'
    assert probe.target_process(pane, {100: shell, 101: parent}) is None


def test_menu_single_descriptions_and_signature():
    menu = probe.parse_menu(SINGLE)
    assert menu['question'] == 'Pick a colour?'
    assert menu['options'][0]['description'] == 'Fresh leaves'
    assert menu['focused_index'] == 0
    changed = SINGLE.replace('❯ 1.', '  1.').replace('  2.', '❯ 2.')
    assert probe.parse_menu(changed)['signature'] == menu['signature']
    assert probe.parse_menu('historical 1. choice\n❯ hello') is None


def test_multi_toggle_preserves_question_signature():
    menu = probe.parse_menu(MULTI)
    assert menu['multi_select']
    assert menu['options'][1]['selected']
    assert probe.parse_menu(MULTI.replace('[ ]', '[x]'))['signature'] == menu['signature']
    assert probe.action_keys(item(MULTI), {'action': 'select', 'index': 1}) == ['Down', 'Space']
    assert probe.action_keys(item(), {'action': 'select', 'index': 1}) == ['Down', 'Enter']


def test_review_explicit_submit():
    assert probe.parse_menu(REVIEW)['kind'] == 'review'
    assert probe.action_keys(item(REVIEW), {'action': 'submit'}) == ['Enter']
    with pytest.raises(probe.ProbeError):
        probe.action_keys(item(), {'action': 'submit'})


def test_free_text_never_ordinary_send():
    free = SINGLE.replace('❯ 1.', '  1.').replace('  3.', '❯ 3.')
    assert probe.action_keys(item(free), {'action': 'type', 'text': 'oat biscuit'}) == []
    with pytest.raises(probe.ProbeError):
        probe.action_keys(item(free), {'action': 'send', 'text': 'oat biscuit'})
    with pytest.raises(probe.ProbeError):
        probe.action_keys(item(), {'action': 'type', 'text': 'oat biscuit'})


@pytest.mark.parametrize('text', ['hello\x1b[31m', 'hello\r', 'hello\t', 'hello\x00', 'hello\x7f'])
def test_control_characters_rejected(text):
    with pytest.raises(probe.ProbeError):
        probe.validate_text(text)


def test_empty_prompt_strict():
    empty = item('────────\n❯ \n────────\n')
    assert probe.empty_prompt(empty)
    assert not probe.empty_prompt(item('────────\n❯ /compact\n────────\n'))
    assert not probe.empty_prompt(item('❯ '))


def test_stale_revision_never_writes(monkeypatch):
    monkeypatch.setattr(probe, 'discover', lambda _: [item()])
    with pytest.raises(probe.ProbeError, match='Screen changed'):
        probe.perform(dict(identity=IDENTITY, revision='old', action='select', index=0))


def test_changed_during_validation_never_writes(monkeypatch):
    old, new = item(), dict(item(), revision='r2')
    results = iter([[old], [new]])
    monkeypatch.setattr(probe, 'discover', lambda _: next(results))
    with pytest.raises(probe.ProbeError, match='during validation'):
        probe.perform(dict(identity=IDENTITY, revision='r1', action='select', index=0))


def test_generation_replacement_rejected(monkeypatch):
    monkeypatch.setattr(probe, 'inventory', lambda: [{'pane_id': '%121'}])
    monkeypatch.setattr(probe, 'processes', lambda: {})
    monkeypatch.setattr(probe, 'target_process', lambda *_: {})
    with pytest.raises(probe.ProbeError, match='generation changed'):
        probe.discover(IDENTITY)


def test_literal_send_and_bracketed_multiline(monkeypatch):
    empty = item('────────\n❯ \n────────\n')
    monkeypatch.setattr(probe, 'discover', lambda _: [empty])
    writes = []
    monkeypatch.setattr(probe, 'tmux', lambda *args, **kwargs: writes.append((args, kwargs)))
    request = dict(identity=IDENTITY, revision='r1', action='send', text="$(bad) 'quoted'", request_id='one')
    assert probe.perform(request)['ok']
    assert writes[0][0] == ('send-keys', '-t', '%121', '-l', '--', request['text'])
    assert writes[1][0][-1] == 'Enter'
    writes.clear()
    probe.perform(dict(request, text='first\nsecond', action='type'))
    assert writes[0][0][0] == 'load-buffer'
    assert writes[0][1]['input'] == 'first\nsecond'
    assert '-p' in writes[1][0] and '-d' in writes[1][0]
    assert not any('Enter' in args for args, _ in writes)


def test_background_target_refused(monkeypatch):
    target = item()
    target['_proc']['state'] = 'S'
    monkeypatch.setattr(probe, 'discover', lambda _: [target])
    with pytest.raises(probe.ProbeError, match='foreground'):
        probe.perform(dict(identity=IDENTITY, revision='r1', action='key', key='Enter'))


def test_duplicate_concurrent_action_sent_once_and_conflict(monkeypatch):
    calls = []
    def fake(host, request):
        calls.append(request)
        return {'ok': True}
    monkeypatch.setattr(fleet, '_probe', fake)
    identifier = fleet._encode('local', IDENTITY)
    def act():
        return fleet.act(identifier, 'select', 'r1', 'same', index=0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: act(), range(4)))
    assert all(r['ok'] for r in results)
    assert len(calls) == 1
    with pytest.raises(fleet.FleetError, match='different payload'):
        fleet.act(identifier, 'select', 'r1', 'same', index=1)


def test_ambiguous_failure_is_never_retried(monkeypatch):
    calls = []
    def fake(*args):
        calls.append(args)
        raise fleet.FleetError('delivery unknown', 503)
    monkeypatch.setattr(fleet, '_probe', fake)
    identifier = fleet._encode('local', IDENTITY)
    for _ in range(2):
        with pytest.raises(fleet.FleetError, match='delivery unknown'):
            fleet.act(identifier, 'key', 'r1', 'retry', key='Enter')
    assert len(calls) == 1


def test_transcript_exact_uuid_and_text_only(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.Path, 'home', lambda: tmp_path)
    folder = tmp_path / '.claude/projects/-workspace'
    folder.mkdir(parents=True)
    entries = [
        {'type': 'system', 'message': {'content': 'system dump'}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'secret dump'}]}},
        {'type': 'user', 'message': {'content': 'hello'}},
        {'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': 'hidden'}, {'type': 'text', 'text': 'answer'}]}},
    ]
    (folder / f'{SID}.jsonl').write_text('\n'.join(json.dumps(e) for e in entries))
    (folder / f'{OTHER}.jsonl').write_text(json.dumps({'type': 'assistant', 'message': {'content': 'WRONG'}}))
    assert probe.transcript_messages({'sid': SID}, '/workspace') == [
        {'role': 'user', 'text': 'hello'}, {'role': 'assistant', 'text': 'answer'}]
    assert probe.transcript_messages({}, '/workspace') == []


def test_transcript_tail_bounded(tmp_path):
    path = tmp_path / 'large.jsonl'
    path.write_bytes(b'x' * (probe.LIMIT + 100) + b'\n{"type":"user"}\n')
    assert probe.read_tail(path) == '{"type":"user"}\n'


@pytest.mark.parametrize('metadata', [
    {'origin': {'kind': 'task-notification'}},
    {'promptSource': 'system'},
    {'queueTranscriptOnly': True},
])
def test_injected_notices_are_not_human_messages(tmp_path, monkeypatch, metadata):
    path = tmp_path / f'{SID}.jsonl'
    monkeypatch.setattr(probe, 'transcript_path', lambda *_: path)
    notice = '<task-notification><task-id>task-example</task-id><status>stopped</status></task-notification>'
    entries = [
        {'type': 'user', 'message': {'content': notice}, **metadata},
        # A person quoting the same XML is still a genuine conversation message.
        {'type': 'user', 'message': {'content': notice}},
        {'type': 'assistant', 'message': {'content': 'That was a background task notice.'}},
    ]
    path.write_text('\n'.join(json.dumps(entry) for entry in entries))
    assert probe.transcript_messages({'sid': SID}, '/workspace') == [
        {'role': 'user', 'text': notice},
        {'role': 'assistant', 'text': 'That was a background task notice.'},
    ]


def test_waiting_attention_and_policy_unavailable():
    assert probe.presentation({}, probe.parse_menu(SINGLE))['notification_eligible'] is True
    assert probe.presentation({'event': 'stop', 'token': 'opaque'}, None) == {
        'status_emoji': 'opaque', 'status_label': None, 'notification_eligible': False}
    assert probe.presentation({'event': 'prompt'}, None)['notification_eligible'] is False


def test_metadata_exact_event_mapping_and_nested_rejection(monkeypatch):
    pane = {'session_name': 'toy', 'window_index': '0', 'pane_index': '0',
            'pane_id': '%121', 'pane_pid': '100', '@hq_owner': ''}
    proc = dict(pid=101, args='claude', born='Thu Oct 08 09:00:00 2026', nested=False)
    events = [dict(sid=SID, tmux='toy:0.0', ts=2000000000, event='prompt'),
              dict(sid=OTHER, tmux='toy-other:0.0', ts=2000000001, event='stop')]
    monkeypatch.setattr(probe, 'json_lines', lambda _: iter(events))
    monkeypatch.setattr(probe, 'read_tail', lambda *a: '')
    assert probe.metadata(pane, proc)['sid'] == SID
    assert probe.metadata(pane, dict(proc, nested=True)) == {}
    # Explicit argv remains authoritative even when a child publishes last.
    events.append(dict(sid=OTHER, tmux='toy:0.0', ts=2000000002, event='stop'))
    assert probe.metadata(pane, dict(proc, args=f'claude -r {SID}', nested=True))['event'] == 'prompt'


def test_inventory_skips_mirrors_and_hubs(monkeypatch):
    def row(name, mirror, pane):
        values = {field: '' for field in probe.FIELDS}
        values.update(session_name=name, pane_id=pane, pane_dead='0')
        values['@tw-src'] = mirror
        return '\t'.join(values[f] for f in probe.FIELDS)
    rows = '\n'.join([row('toy', '0', '%121'), row('hub/view', '0', '%122'), row('mirror', '1', '%123')])
    def fake(*args):
        return subprocess.CompletedProcess(args, 0, '1|2|/tmp/test' if args[0] == 'display-message' else rows, '')
    monkeypatch.setattr(probe, 'tmux', fake)
    assert [p['pane_id'] for p in probe.inventory()] == ['%121']


def test_prompt_supersedes_old_outcome_and_stable_event(monkeypatch):
    pane = {f: '' for f in probe.FIELDS}
    pane.update(generation='server', pane_id='%121', pane_pid='100',
                session_created='123', pane_current_path='/workspace', session_name='toy')
    proc = dict(pid=101, born='birth')
    monkeypatch.setattr(probe, 'capture', lambda _: '────────\n❯ \n────────\n')
    monkeypatch.setattr(probe, 'metadata', lambda *_: dict(sid=SID, event='prompt', ts=42,
                                                        token='old-token', tab_name='old-token title', tab_time=41))
    first = probe.inspect(pane, proc)
    second = probe.inspect(pane, proc)
    assert first['status'] == 'working'
    assert first['status_emoji'] is None
    assert first['event_id'] == second['event_id'] == '42'
    assert first['notification_eligible'] is False


def test_notification_policy_delegates_to_owner(monkeypatch):
    monkeypatch.setattr(probe, 'notify_library', lambda: Path('/configured/cc-lib.sh'))
    seen = []
    def owner(args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, '\0'.join(['producer-emoji', 'Producer quiet label', '1']), '')
    monkeypatch.setattr(probe, 'run', owner)
    assert probe.presentation({'event': 'stop', 'token': 'opaque-poll-token'}, None)['notification_eligible'] is False
    assert 'cc_present' in seen[0][2]
    assert 'opaque-poll-token' in seen[0]


def test_ps_version_process_parsing(monkeypatch):
    output = f' 101 100 ttys001 S+ Thu Oct  8 09:00:00 2026 claude --session-id {SID}\n'
    monkeypatch.setattr(probe, 'run', lambda args: subprocess.CompletedProcess(
        args, 0, '101 2.1.294\n' if args[-1] == 'pid=,comm=' else output, ''))
    proc = probe.processes()[101]
    assert proc['born'] == 'Thu Oct 8 09:00:00 2026'
    assert probe.is_claude(proc)
    assert probe.argv_sid(proc) == SID


def test_native_version_path_at_pane_root_is_discovered(monkeypatch):
    # The combined Darwin ps format used to truncate this to /Users/example/.
    # No intermediate shell: tmux's pane_pid is the Claude executable itself.
    executable = '/Users/example/.local/share/claude/versions/2.1.294'
    output = f' 101 50 ttys019 Ss+ Thu Oct  8 09:00:00 2026 {executable} --safe-mode --session-id {SID}\n'
    def fake(args):
        assert args[-1] in ('pid=,comm=', 'pid=,ppid=,tty=,stat=,lstart=,args=')
        return subprocess.CompletedProcess(args, 0,
            f'101 {executable}\n' if args[-1] == 'pid=,comm=' else output, '')
    monkeypatch.setattr(probe, 'run', fake)
    pane = {'pane_pid': '101', 'pane_tty': '/dev/ttys019',
            'pane_id': '%121', 'pane_current_command': '2.1.294'}
    monkeypatch.setattr(probe, 'inventory', lambda: [pane])
    monkeypatch.setattr(probe, 'inspect', lambda pane, proc: {
        'pane': pane['pane_id'], 'claude_session_id': probe.argv_sid(proc),
        'command': proc['comm']})
    sessions = probe.discover()
    assert sessions == [{'pane': '%121', 'claude_session_id': SID, 'command': executable}]


def test_real_claude_menu_shapes():
    # Claude 2.1's header checkbox is not a multiselect option. Selected
    # checkboxes use the heavy checkmark, and review omits the key footer.
    single = '────────────────\n ☐ Color\n\n' + SINGLE
    assert probe.parse_menu(single)['multi_select'] is False
    multi = MULTI.replace('[x]', '[✔]').replace('Space to select · Enter to confirm', 'Enter to select')
    parsed = probe.parse_menu(multi)
    assert parsed['multi_select'] is True
    assert parsed['options'][1]['selected'] is True
    review = 'Review your answers\n\n ● Which items?\n   → Map, Rope\n\nReady to submit your answers?\n\n❯ 1. Submit answers\n  2. Cancel\n'
    assert probe.parse_menu(review)['kind'] == 'review'
    assert probe.action_keys(item(review), {'action': 'submit'}) == ['Enter']
    prompt = '──────── toy ─\n❯\u00a0\n────────────────\n  auto mode on\n'
    assert probe.empty_prompt(item(prompt)) is True


def test_other_in_ordinary_option_does_not_authorize_typing():
    ordinary = SINGLE.replace('Red', 'Another choice').replace('Green', 'Other animals')
    with pytest.raises(probe.ProbeError):
        probe.action_keys(item(ordinary), {'action': 'type', 'text': 'unsafe'})


def test_activity_time_never_uses_polling_clock(tmp_path, monkeypatch):
    path = tmp_path / 'exact-session.jsonl'
    path.write_text('{}\n')
    import os
    os.utime(path, (1234, 1234))
    monkeypatch.setattr(probe, 'transcript_path', lambda *_: path)
    monkeypatch.setattr(probe.time, 'time', lambda: 9999999999)
    assert probe.activity_time({'ts': 1200, 'tab_time': 1220}, '/workspace') == 1234
    assert probe.activity_time({'ts': 1300, 'tab_time': 1290}, '/workspace') == 1300
    monkeypatch.setattr(probe, 'transcript_path', lambda *_: None)
    assert probe.activity_time({}, '/workspace') is None


def test_session_polling_preserves_activity_and_event_id(monkeypatch):
    pane = {f: '' for f in probe.FIELDS}
    pane.update(generation='server', pane_id='%121', pane_pid='100',
                session_created='123', pane_current_path='/workspace', session_name='toy')
    monkeypatch.setattr(probe, 'capture', lambda _: '────────\n❯ \n────────\n')
    monkeypatch.setattr(probe, 'metadata', lambda *_: {'sid': SID, 'event': 'stop', 'ts': 42})
    monkeypatch.setattr(probe, 'transcript_path', lambda *_: None)
    first = probe.inspect(pane, dict(pid=101, born='birth'))
    monkeypatch.setattr(probe.time, 'time', lambda: 9999999999)
    second = probe.inspect(pane, dict(pid=101, born='birth'))
    assert first['updated_at'] == second['updated_at'] == 42
    assert first['event_id'] == second['event_id'] == '42'


@pytest.mark.parametrize('label,tab_only', [('Task failed', ''), ('Blocked', ''),
                                          ('Waiting for instructions', ''),
                                          ('Task complete', ''), ('Quiet result', '1')])
def test_inspect_uses_one_authoritative_presentation_call(monkeypatch, label, tab_only):
    pane = {f: '' for f in probe.FIELDS}
    pane.update(generation='server', pane_id='%121', pane_pid='100',
                session_created='123', pane_current_path='/workspace', session_name='toy')
    monkeypatch.setattr(probe, 'capture', lambda _: '────────\n❯ \n────────\n')
    monkeypatch.setattr(probe, 'metadata', lambda *_: {'sid': SID, 'event': 'stop', 'ts': 42, 'token': 'opaque-source'})
    monkeypatch.setattr(probe, 'transcript_path', lambda *_: None)
    monkeypatch.setattr(probe, 'notify_library', lambda: Path('/configured/cc-lib.sh'))
    calls = []
    def owner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '\0'.join(['canonical-display', label, tab_only]), '')
    monkeypatch.setattr(probe, 'run', owner)
    session = probe.inspect(pane, dict(pid=101, born='birth'))
    assert session['status_label'] == label
    assert session['status_emoji'] == 'canonical-display'
    assert session['notification_eligible'] is (not bool(tab_only))
    assert session['status'] == 'idle'
    assert session['event_id'] == '42'
    assert len(calls) == 1


def test_idle_tab_label_does_not_fabricate_notification(monkeypatch):
    monkeypatch.setattr(probe, 'notify_library', lambda: Path('/configured/cc-lib.sh'))
    monkeypatch.setattr(probe, 'run', lambda args, **kwargs: subprocess.CompletedProcess(
        args, 0, '\0'.join(['canonical-display', 'Producer label', '']), ''))
    result = probe.presentation({}, None, token='opaque-tab', status='idle')
    assert result == {'status_emoji': 'canonical-display', 'status_label': 'Producer label',
                      'notification_eligible': False}


def test_menu_excludes_numbered_history_and_counts_submit():
    text = '''Earlier prose with misleading instructions:
❯ 1. Historical choice
  2. Historical action
────────────────
← ☒ Features  ✔ Submit →

Which features?

  1. [✔] Alpha
         First
  2. [ ] Beta
         Second
❯ 3. [✔] Gamma
         Third
  4. [ ] Type something
     Submit
────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
'''
    menu = probe.parse_menu(text)
    assert [o['label'] for o in menu['options']] == ['Alpha', 'Beta', 'Gamma', 'Type something', 'Submit', 'Chat about this']
    assert menu['focused_index'] == 2
    assert menu['kind'] == 'question'
    assert probe.action_keys(item(text), {'action': 'select', 'index': 5}) == ['Down', 'Down', 'Down', 'Enter']
    assert probe.action_keys(item(text), {'action': 'select', 'index': 4}) == ['Down', 'Down', 'Enter']
    focused_submit = text.replace('❯ 3.', '  3.').replace('     Submit', '❯    Submit')
    assert probe.parse_menu(focused_submit)['focused_index'] == 4


def test_free_text_row_signature_survives_label_edit():
    text = SINGLE.replace('Enter to select', '────────────────\n  4. Chat about this\nEnter to select')
    filled = text.replace('❯ 1.', '  1.').replace('  3. Type something.', '❯ 3. Oat biscuit, lightly salted')
    first, second = probe.parse_menu(text), probe.parse_menu(filled)
    assert first['signature'] == second['signature']
    assert second['options'][2]['kind'] == 'free_text'
    assert probe.action_keys(item(filled), {'action': 'type', 'text': ' plus tea'}) == []


def test_unanchored_numbered_prose_never_becomes_menu():
    assert probe.parse_menu('Prose example\n❯ 1. Delete everything\nEnter to select') is None
    assert probe.parse_menu(SINGLE + '\n────────\n❯ hello\n────────') is None


def test_prompt_helper_uses_default_plus_raw_slash_guard(monkeypatch):
    target = item('not parsed by fallback')
    target['_pane']['cursor_x'] = '2'
    monkeypatch.setattr(probe, 'PROMPT_STATE_SOURCE', '# canonical owner helper\n', raising=False)
    calls = []
    def fake(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(probe, 'run', fake)
    assert probe.empty_prompt(target)
    assert calls[0][0] == ['bash', '-s', '--', '%121']
    assert 'RAW=1' in calls[0][1]['input']
    assert '/*) exit 1' in calls[0][1]['input']
    target['_pane']['cursor_x'] = '8'
    assert not probe.empty_prompt(target)
    assert len(calls) == 1


def test_select_other_only_navigates_never_submits():
    assert probe.action_keys(item(SINGLE), {'action': 'select', 'index': 2}) == ['Down', 'Down']
    focused = SINGLE.replace('❯ 1.', '  1.').replace('  3.', '❯ 3.')
    assert probe.action_keys(item(focused), {'action': 'select', 'index': 2}) == []
    multi = MULTI.replace('Space to select', '  3. [ ] Type something\nSpace to select')
    assert probe.action_keys(item(multi), {'action': 'select', 'index': 2}) == ['Down', 'Down']


def test_batched_question_next_is_a_physical_navigation_row():
    text = MULTI.replace('Space to select', '  3. [ ] Type something\n     Next\n────────────────\n  4. Chat about this\nSpace to select')
    menu = probe.parse_menu(text)
    assert [o['kind'] for o in menu['options']] == ['option', 'option', 'free_text', 'next', 'chat']
    assert probe.action_keys(item(text), {'action': 'select', 'index': 3}) == ['Down', 'Down', 'Down', 'Enter']


@pytest.mark.parametrize('tail,expected_status,eligible', [
    (SINGLE, 'waiting', True),
    (MULTI, 'waiting', True),
    ('✻ Working…\n', 'working', False),
    ('Do you want to proceed?\n', 'waiting', True),
])
def test_current_terminal_supersedes_previous_stop(monkeypatch, tail, expected_status, eligible):
    pane = {f: '' for f in probe.FIELDS}
    pane.update(generation='server', pane_id='%121', pane_pid='100',
                session_created='123', pane_current_path='/workspace', session_name='toy')
    monkeypatch.setattr(probe, 'capture', lambda _: tail)
    monkeypatch.setattr(probe, 'metadata', lambda *_: {
        'sid': SID, 'event': 'stop', 'ts': 42, 'token': 'previous-outcome',
        'tab_name': 'previous-outcome title', 'tab_time': 42})
    monkeypatch.setattr(probe, 'transcript_path', lambda *_: None)
    monkeypatch.setattr(probe, 'notify_library', lambda: Path('/configured/cc-lib.sh'))
    calls = []
    def owner(args, **kwargs):
        calls.append(args)
        assert 'cc_present' not in args[2]
        assert 'previous-outcome' not in args
        return subprocess.CompletedProcess(args, 0, 'live-owner-emoji', '')
    monkeypatch.setattr(probe, 'run', owner)
    session = probe.inspect(pane, dict(pid=101, born='birth'))
    assert session['status'] == expected_status
    assert session['status_emoji'] == 'live-owner-emoji'
    assert session['status_label'] is None
    assert session['notification_eligible'] is eligible
    assert session['event_id'].startswith('42')
    assert len(calls) == 1


def test_current_work_suppresses_opaque_tab_outcome():
    result = probe.presentation({}, None, token='opaque-old-tab', status='working')
    assert result == {'status_emoji': None, 'status_label': None, 'notification_eligible': False}


def test_missing_exact_transcript_never_adopts_nested_same_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.Path, 'home', lambda: tmp_path)
    folder = tmp_path / '.claude/projects/-workspace'
    folder.mkdir(parents=True)
    # Parent argv identifies SID; a nested Claude wrote a different UUID in
    # the same directory. Neither its presence nor recency proves identity.
    nested = {'type': 'assistant', 'sessionId': OTHER,
              'message': {'content': 'Nested conversation must not be adopted'}}
    (folder / f'{OTHER}.jsonl').write_text(json.dumps(nested) + '\n')
    assert probe.transcript_path({'sid': SID}, '/workspace') is None
    assert probe.transcript_messages({'sid': SID}, '/workspace') == []
    assert probe.transcript_messages({'sid': OTHER}, '/workspace') == [
        {'role': 'assistant', 'text': 'Nested conversation must not be adopted'}]


@pytest.mark.parametrize('tail,question,kind', [
    (SINGLE, True, 'question'),
    ('Do you want to proceed?\n', False, 'permission'),
    ('────────\n❯ \n────────\n', False, None),
])
def test_attention_fields_survive_snapshot_without_menu(monkeypatch, tail, question, kind):
    pane = {f: '' for f in probe.FIELDS}
    pane.update(generation='server', pane_id='%121', pane_pid='100',
                session_created='123', pane_current_path='/workspace', session_name='toy')
    monkeypatch.setattr(probe, 'capture', lambda _: tail)
    monkeypatch.setattr(probe, 'metadata', lambda *_: {})
    monkeypatch.setattr(probe, 'transcript_path', lambda *_: None)
    row = probe.public(probe.inspect(pane, dict(pid=101, born='birth')))
    assert 'menu' not in row
    assert row['has_question'] is question
    assert row['attention_kind'] == kind


@pytest.fixture
def routed_runtime(tmp_path, monkeypatch):
    import time
    state = tmp_path / 'notify'
    state.mkdir()
    monkeypatch.setattr(probe, 'NOTIFY_STATE', state)
    monkeypatch.setattr(probe, 'json_lines', lambda _: iter(()))
    born = 'Thu Oct 08 09:00:00 2026'
    epoch = time.mktime(time.strptime(born, '%a %b %d %H:%M:%S %Y'))
    shell = dict(pid=100, parent=1, tty='ttys001', state='S', born=born, comm='zsh', args='zsh')
    claude = dict(shell, pid=101, parent=100, comm='claude', args='claude -r')
    procs = {100: shell, 101: claude}
    pane = {'session_name': 'toy', 'pane_id': '%121', 'pane_pid': '100',
            'pane_tty': '/dev/ttys001', 'pane_current_command': '2.1.294',
            'window_index': '0', 'pane_index': '0', 'pane_current_path': '/workspace',
            'generation': '50|123|/tmp/test-tmux', '@hq_owner': ''}
    def write(sid=SID, **overrides):
        import os
        values = dict(tmux_target='toy:0.0', tmux_socket='/tmp/test-tmux', cwd='/workspace',
                      shell_pids='999,101,100')
        values.update(overrides)
        path = state / f'{sid}.route'
        path.write_text('\n'.join(f'{k}={shlex.quote(v)}' for k, v in values.items()))
        os.utime(path, (epoch + 60, epoch + 60))
        return path
    return pane, procs, write, epoch


def test_route_recovers_missing_argv_uuid_from_exact_runtime(routed_runtime):
    pane, procs, write, _ = routed_runtime
    write()
    proc = probe.target_process(pane, procs)
    assert probe.argv_sid(proc) is None
    assert probe.metadata(pane, proc)['sid'] == SID


@pytest.mark.parametrize('fields', [
    {'tmux_target': 'other:0.0'}, {'tmux_socket': '/tmp/other-socket'},
    {'cwd': '/other'}, {'shell_pids': '999,100'}, {'shell_pids': '999,101'},
    {'remote_host': 'another-host'}, {'tmux_target': ''},
])
def test_route_requires_all_exact_mapping_fields(routed_runtime, fields):
    pane, procs, write, _ = routed_runtime
    write(**fields)
    assert probe.metadata(pane, probe.target_process(pane, procs)) == {}


def test_route_rejects_prior_lifetime_and_ambiguity(routed_runtime):
    import os
    pane, procs, write, epoch = routed_runtime
    path = write()
    os.utime(path, (epoch - 1, epoch - 1))
    assert probe.metadata(pane, probe.target_process(pane, procs)) == {}
    write()
    write(OTHER)
    assert probe.metadata(pane, probe.target_process(pane, procs)) == {}


def test_route_rejects_nearer_nested_claude_even_on_other_tty(routed_runtime):
    pane, procs, write, _ = routed_runtime
    procs[201] = dict(procs[101], pid=201, parent=101, tty='ttys099', args=f'claude --session-id {OTHER}')
    write(OTHER, shell_pids='999,201,101,100')
    proc = probe.target_process(pane, procs)
    assert proc['pid'] == 101
    assert probe.metadata(pane, proc) == {}
    # Explicit outer argv remains the identity even when the only persisted
    # conversation belongs to a nested descendant on a different TTY.
    proc['args'] = f'claude --session-id {SID}'
    assert probe.metadata(pane, proc)['sid'] == SID


def test_route_rejects_reused_intermediate_pid(routed_runtime):
    pane, procs, write, _ = routed_runtime
    procs[999] = dict(procs[100], pid=999, parent=101, born='Thu Oct 08 10:00:00 2026')
    write()
    assert probe.metadata(pane, probe.target_process(pane, procs)) == {}


@pytest.mark.parametrize('sid,exists,content,expected', [
    (None, False, '', 'identity_missing'),
    (SID, False, '', 'file_missing'),
    (SID, True, '{"type":"user","message":{"content":[{"type":"tool_result","content":"hidden"}]}}\n', 'parser_empty'),
    (SID, True, '{"type":"assistant","message":{"content":"real answer"}}\n', 'available'),
])
def test_transcript_status_is_explicit_without_fabricated_messages(tmp_path, monkeypatch, sid, exists, content, expected):
    monkeypatch.setattr(probe.Path, 'home', lambda: tmp_path)
    path = tmp_path / '.claude/projects/-workspace' / f'{SID}.jsonl'
    if exists:
        path.parent.mkdir(parents=True)
        path.write_text(content)
    data = dict(item(), _meta={'sid': sid}, workdir='/workspace')
    result = probe.public(data, full=True)
    assert result['transcript_status'] == expected
    assert result['messages'] == ([{'role': 'assistant', 'text': 'real answer'}] if expected == 'available' else [])
