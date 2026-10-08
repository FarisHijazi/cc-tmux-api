"""Stdlib host probe, shipped verbatim over SSH; no daemon or worker ownership.

See docs/mobile-control-research.md for the adoption and cc-notify contracts.
All target resolution uses pane IDs plus server/process birth identity. Terminal
checks reduce races but cannot lock out a person typing on the same terminal.
"""
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

if '_detect_claude_status' not in globals():
    try:
        from .vendor_amux import _detect_claude_status
    except ImportError:
        from vendor_amux import _detect_claude_status

LIMIT = 1024 * 1024
DEADLINE = None
NOTIFY_STATE = Path('/tmp/cc-notify')


class ProbeError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def run(args, input=None, check=True):
    remaining = 4 if DEADLINE is None else min(4, DEADLINE - time.monotonic())
    if remaining <= 0:
        raise ProbeError('Host probe deadline exceeded', 504)
    try:
        result = subprocess.run(args, input=input, text=True, capture_output=True,
                                timeout=remaining)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f'Host command unavailable or timed out: {args[0]}', 503) from exc
    if check and result.returncode:
        raise ProbeError(f'{args[0]} failed: {result.stderr.strip()[:300]}', 503)
    return result


def tmux(*args, **kwargs):
    return run(['tmux', *args], **kwargs)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def uuid_value(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def read_tail(path, limit=LIMIT):
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            data = stream.read(limit)
        if size > limit:
            data = data.partition(b'\n')[2]
        return data.decode('utf-8', errors='replace')
    except OSError:
        return ''


def json_lines(path):
    for line in read_tail(path).splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                yield value
        except ValueError:
            continue


def processes():
    result = {}
    # Darwin truncates comm to 16 characters when another column follows it,
    # even with -ww. Put each variable-width field LAST in its own query.
    # This also preserves executable paths containing spaces on Linux/macOS.
    names = {}
    for line in run(['ps', '-axo', 'pid=,comm=']).stdout.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and fields[0].isdigit():
            names[int(fields[0])] = fields[1]
    output = run(['ps', '-ww', '-axo', 'pid=,ppid=,tty=,stat=,lstart=,args=']).stdout
    for line in output.splitlines():
        fields = line.split(None, 9)
        if len(fields) != 10:
            continue
        pid, parent, tty, state = fields[:4]
        if not pid.isdigit() or not parent.isdigit() or int(pid) not in names:
            continue
        result[int(pid)] = dict(pid=int(pid), parent=int(parent), tty=tty,
                                state=state, born=' '.join(fields[4:9]),
                                comm=names[int(pid)], args=fields[9])
    return result


def is_claude(proc):
    name = Path(proc['comm']).name
    # Native Claude changes its process title to its version. Never match a
    # shell command which merely mentions claude somewhere in its arguments.
    return name == 'claude' or bool(re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][\w.-]+)?', name))


def target_process(pane, procs):
    if pane['pane_current_command'] in ('bash', 'zsh', 'fish', 'sh', 'dash', 'ksh'):
        return None
    root = int(pane['pane_pid'])
    candidates = []
    for proc in procs.values():
        if not is_claude(proc) or proc['tty'].removeprefix('/dev/') != pane['pane_tty'].removeprefix('/dev/'):
            continue
        current, seen, nested = proc['pid'], set(), False
        while current in procs and current not in seen:
            seen.add(current)
            if current == root:
                if not nested:
                    candidates.append(proc)
                break
            current = procs[current]['parent']
            if current in procs and current != proc['pid'] and is_claude(procs[current]):
                nested = True
    # Two top-level Claude processes on one tty are ambiguous. Never select a
    # child CLI invocation belonging to an agent tool or its inherited route.
    if len(candidates) != 1:
        return None
    result = dict(candidates[0])
    # Any nested Claude invalidates event-only UUID inference: those children
    # inherit the same TMUX_PANE and can publish their own session IDs.
    result['nested'] = any(is_claude(p) and p['pid'] != result['pid'] and
                           p['tty'] == result['tty'] for p in procs.values())
    result['_processes'] = procs
    return result


def argv_sid(proc):
    try:
        args = shlex.split(proc['args'])
    except ValueError:
        return None
    for i, arg in enumerate(args):
        if arg in ('--session-id', '--resume', '-r') and i + 1 < len(args):
            sid = uuid_value(args[i + 1])
            if sid:
                return sid
        if arg.startswith(('--session-id=', '--resume=')):
            sid = uuid_value(arg.split('=', 1)[1])
            if sid:
                return sid
    return None


FIELDS = ('session_name', 'session_created', 'pane_id', 'pane_pid', 'pane_tty',
          'pane_current_command', 'pane_current_path', 'window_index', 'pane_index',
          '@tw-src', '@hq_owner', '@hq_sid', '@hq_workdir', '@hq_title',
          'pane_dead', 'cursor_x', 'cursor_y', 'pane_in_mode', 'pane_title')


def inventory():
    generation = tmux('display-message', '-p', '#{pid}|#{start_time}|#{socket_path}').stdout.strip()
    if not generation or generation.startswith('|'):
        raise ProbeError('Cannot establish tmux server identity', 503)
    output = tmux('list-panes', '-a', '-F', '\t'.join(
        '#{?@tw-src,1,0}' if f == '@tw-src' else '#{' + f + '}' for f in FIELDS)).stdout
    panes = []
    for line in output.splitlines():
        values = line.split('\t', len(FIELDS) - 1)
        if len(values) != len(FIELDS):
            continue
        pane = dict(zip(FIELDS, values))
        if pane['@tw-src'] == '1' or pane['session_name'].startswith('hub/') or pane['pane_dead'] == '1':
            continue
        pane['generation'] = generation
        panes.append(pane)
    return panes


def route_data(path):
    result = {}
    for line in read_tail(path, 16384).splitlines():
        key, sep, value = line.partition('=')
        if sep:
            try:
                words = shlex.split(value)
                if len(words) == 1:
                    result[key] = words[0]
            except ValueError:
                pass
    return result


def route_sid(pane, proc, born, exact):
    """Recover a UUID only from a unique current, exact cc-notify route.

    The route's first surviving process must lead to this observed Claude as
    its nearest Claude ancestor. A descendant session can include the outer
    PID in shell_pids; mere membership would misattribute that child transcript.
    No transcript enumeration, mtime ordering, or cwd-only inference is used.
    """
    procs = proc.get('_processes', {})
    if not procs or not pane.get('generation'):
        return None
    socket = pane['generation'].split('|', 2)[-1]
    candidates, routes = set(), 0
    try:
        with os.scandir(NOTIFY_STATE) as entries:
            for count, entry in enumerate(entries):
                if count >= 4096:
                    return None  # An incomplete scan cannot establish uniqueness.
                if not entry.name.endswith('.route') or not uuid_value(entry.name[:-6]):
                    continue
                routes += 1
                if routes > 512:
                    return None
                try:
                    written = entry.stat(follow_symlinks=False).st_mtime
                    if entry.is_symlink() or written < born:
                        continue
                    route = route_data(entry.path)
                    if (route.get('remote_host') or route.get('tmux_target') != exact or
                            not route.get('tmux_socket') or not route.get('cwd') or
                            os.path.realpath(route['tmux_socket'].split(',')[0]) != os.path.realpath(socket) or
                            os.path.realpath(route['cwd']) != os.path.realpath(pane['pane_current_path'])):
                        continue
                    pids = [int(p) for p in route.get('shell_pids', '').split(',') if p.isdigit()]
                    if proc['pid'] not in pids or int(pane['pane_pid']) not in pids:
                        continue
                    current = next((pid for pid in pids if pid in procs), None)
                    seen, nearest = set(), None
                    while current in procs and current not in seen:
                        seen.add(current)
                        ancestor = procs[current]
                        # A reused PID is not evidence about this old route.
                        if time.mktime(time.strptime(ancestor['born'], '%a %b %d %H:%M:%S %Y')) > written:
                            break
                        if is_claude(ancestor):
                            nearest = current
                            break
                        current = ancestor['parent']
                    if nearest == proc['pid']:
                        candidates.add(uuid_value(entry.name[:-6]))
                except (OSError, ValueError, KeyError):
                    continue
    except OSError:
        return None
    return next(iter(candidates)) if len(candidates) == 1 else None


def metadata(pane, proc):
    sid = argv_sid(proc)
    exact = f"{pane['session_name']}:{pane['window_index']}.{pane['pane_index']}"
    events = []
    for name in ('cc-events.jsonl.1', 'cc-events.jsonl'):
        for event in json_lines(Path.home() / '.claude' / name):
            if event.get('tmux') in (exact, pane['pane_id']) and uuid_value(event.get('sid')):
                events.append(event)
    # A child's inherited TMUX_PANE is NOT evidence of the parent's UUID.
    # If argv is unavailable accept only an unambiguous exact route. A stale
    # event predating this process is not an identity witness.
    try:
        born = time.mktime(time.strptime(proc['born'], '%a %b %d %H:%M:%S %Y'))
    except ValueError:
        born = float('inf')
    current = [e for e in events if isinstance(e.get('ts'), (int, float)) and e['ts'] >= born]
    if not sid and not proc.get('nested'):
        candidates = {uuid_value(e['sid']) for e in current}
        if len(candidates) == 1:
            sid = candidates.pop()
    if not sid:
        sid = route_sid(pane, proc, born, exact)
    # @hq_sid describes launch intent, not the current runtime UUID. It must
    # never override argv/exact event evidence after resume or replacement.
    if not sid:
        return {}
    matching = [e for e in current if e['sid'] == sid]
    event = max(matching, key=lambda e: e['ts'], default={})
    result = dict(event, sid=sid)
    state = NOTIFY_STATE
    route = route_data(state / f'{sid}.route')
    if not route.get('remote_host') and route.get('tmux_target') in (exact, pane['pane_id']):
        result['cwd'] = result.get('cwd') or route.get('cwd')
        result['transcript_path'] = result.get('transcript_path') or route.get('transcript_path')
    try:
        tab_path = state / f'{sid}.tab'
        tab = json.loads(read_tail(tab_path, 16384))
        # Tab pids include ancestor shells. Require this pane's shell or Claude
        # and a tab written during this process lifetime.
        if (tab_path.stat().st_mtime >= born and
                {int(pane['pane_pid']), proc['pid']}.intersection(tab.get('pids', []))):
            result['tab_name'] = tab.get('name', '')
            result['tab_time'] = tab_path.stat().st_mtime
    except (OSError, ValueError, TypeError):
        pass
    return result


def transcript_path(meta, workdir):
    sid = uuid_value(meta.get('sid'))
    if not sid:
        return None
    root = Path.home() / '.claude' / 'projects'
    paths = []
    supplied = meta.get('transcript_path')
    if supplied and Path(supplied).name == f'{sid}.jsonl':
        paths.append(Path(supplied))
    paths.append(root / re.sub(r'[^a-zA-Z0-9-]', '-', workdir) / f'{sid}.jsonl')
    # Bounded one-level scan for the exact UUID (alternate config encodings),
    # never choose by newest file, cwd alone, or a nested subagent transcript.
    if not any(p.is_file() for p in paths) and root.is_dir():
        for i, folder in enumerate(root.iterdir()):
            if i >= 512:
                break
            candidate = folder / f'{sid}.jsonl'
            if candidate.is_file():
                paths.append(candidate)
                break
    return next((p for p in paths if p.is_file()), None)


def transcript_messages(meta, workdir):
    sid = uuid_value(meta.get('sid'))
    path = transcript_path(meta, workdir)
    if path is None:
        return []
    messages = []
    for entry in json_lines(path):
        role = entry.get('type')
        if role not in ('user', 'assistant') or entry.get('isMeta'):
            continue
        if entry.get('sessionId') and entry['sessionId'] != sid:
            continue
        message = entry.get('message') or {}
        if not isinstance(message, dict):
            continue
        content = message.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(b['text'] for b in content if isinstance(b, dict)
                                and b.get('type') == 'text' and isinstance(b.get('text'), str))
        if isinstance(content, str) and content.strip():
            messages.append({'role': role, 'text': content})
    return messages[-100:]


def activity_time(meta, workdir):
    """Known activity, never the probe's polling timestamp. No UUID guessing."""
    times = [meta.get('ts'), meta.get('tab_time')]
    path = transcript_path(meta, workdir)
    if path is not None:
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            pass
    return max((t for t in times if isinstance(t, (float, int)) and t > 0), default=None)


def parse_menu(text):
    """Parse only an anchored active dialog, preserving every navigation row."""
    lines = text.splitlines()
    footers = [i for i, line in enumerate(lines) if any(s in line.lower() for s in
               ('enter to select', 'enter to submit', 'space to select', 'space to toggle', 'enter to confirm'))]
    review_starts = [i for i, line in enumerate(lines) if line.strip() == 'Review your answers']
    review = bool(review_starts and any(line.strip() == 'Ready to submit your answers?'
                                       for line in lines[review_starts[-1]:]))
    end = footers[-1] if footers else len(lines)
    if review:
        start = review_starts[-1]
        end = next((i for i in reversed(footers) if i > start), len(lines))
    else:
        headers = [i for i, line in enumerate(lines[:end])
                   if re.match(r'^\s*(?:←\s*)?[☐☒☑]\s+\S', line)
                   and any(prior.strip().startswith('───') for prior in lines[max(0, i - 2):i])]
        if not footers or not headers:
            return None
        start = headers[-1]
        review = False
    # A historical footer above a newly drawn prompt is not an active menu.
    if any(re.match(r'^\s*❯(?:\s|$)', line) for line in lines[end + 1:]):
        return None
    options, focused, first, checkbox_options = [], None, None, False
    for row in range(start + 1, end):
        line = lines[row]
        match = re.match(r'^\s*([❯>])?\s*(\d+)\.\s*(?:([☐☑☒]|\[[ xX✓✔]\])\s*)?(.+?)\s*$', line)
        submit = re.fullmatch(r'\s*([❯>])?\s*(Submit|Next)\s*', line)
        if match or (submit and checkbox_options):
            cursor, number, check, label = match.groups() if match else (submit[1], None, None, submit[2])
            checkbox_options = checkbox_options or bool(check)
            if first is None:
                first = row
            index = len(options)
            if cursor:
                if focused is not None:
                    return None
                focused = index
            kind = 'option'
            if label in ('Submit', 'Submit answers'):
                kind = 'submit'
            elif label == 'Next':
                kind = 'next'
            elif label == 'Chat about this':
                kind = 'chat'
            elif re.fullmatch(r'Type something[.… ]*', label):
                kind = 'free_text'
            options.append(dict(label=label, description='', selected=check in ('☑', '☒', '[x]', '[X]', '[✓]', '[✔]'),
                                index=index, kind=kind))
        elif options and line.strip() and not line.strip().startswith('─'):
            options[-1]['description'] += ('\n' if options[-1]['description'] else '') + line.strip()
    if not options or focused is None:
        return None
    # Claude's built-in free-text row is the last answer before Submit/Chat.
    # Its label becomes the user's draft, so never use that mutable label as
    # the question identity. Require the built-in Chat row as structural proof.
    if not review and any(o['kind'] == 'chat' for o in options):
        answers = [o for o in options if o['kind'] not in ('submit', 'next', 'chat')]
        if answers:
            answers[-1]['kind'] = 'free_text'
    before = [line.strip() for line in lines[start + 1:first] if line.strip() and not line.strip().startswith('─')]
    result = dict(question=before[-1] if before else '', options=options,
                  multi_select=checkbox_options, focused_index=focused,
                  kind='review' if review else 'question')
    result['signature'] = digest([result['question'], result['kind'], result['multi_select'],
                                  [(o['kind'], '' if o['kind'] == 'free_text' else o['label'],
                                    '' if o['kind'] == 'free_text' else o['description']) for o in options]])
    return result


def notify_library():
    helper = shutil.which('cc-prompt-state')
    if helper:
        candidate = Path(helper).resolve().parent.parent / 'hooks' / 'cc-lib.sh'
        if candidate.is_file():
            return candidate
    # Installed plugin registries are local configuration, never request input.
    registry = Path.home() / '.claude' / 'plugins' / 'installed_plugins.json'
    try:
        data = json.loads(read_tail(registry))
        for name, installs in data.get('plugins', {}).items():
            if name.split('@')[0] == 'cc-notify':
                for install in installs:
                    candidate = Path(install['installPath']) / 'hooks' / 'cc-lib.sh'
                    if candidate.is_file():
                        return candidate
    except (ValueError, TypeError, KeyError):
        pass
    candidate = Path.home() / '.claude' / 'hooks' / 'cc-lib.sh'
    return candidate if candidate.is_file() else None


def presentation(meta, menu, token=None, status='unknown', attention_kind=None):
    """Obtain display and eligibility together from the presentation owner.

    An idle tab without an event can supply a display token, but cannot create
    a new notification event. No token vocabulary is interpreted here.
    """
    result = dict(status_emoji=token or meta.get('token') or None, status_label=None,
                  notification_eligible=bool(menu or meta.get('event') == 'menu'))
    kind = meta.get('event')
    # The current dialog/spinner supersedes a previous outcome. Do not ask
    # cc_present to reinterpret that old stop as if it were current activity.
    if menu or status == 'working' or (status == 'waiting' and kind == 'stop'):
        active = dict(status_emoji=None, status_label=None,
                      notification_eligible=bool(menu or status == 'waiting'))
        library = notify_library()
        if library:
            state = ('options' if menu else 'running' if status == 'working' else
                     'permission' if attention_kind == 'permission' else 'needs_input')
            try:
                response = run(['bash', '-c', '. "$1" || exit; cc_status_emoji "$2"',
                                'cc-fleet', str(library), state], check=False)
                if response.returncode == 0:
                    active['status_emoji'] = response.stdout.strip() or None
            except ProbeError:
                pass
        return active
    if kind not in ('notification', 'stop'):
        if not kind and token and status == 'idle':
            kind = 'stop'
        else:
            return result
    library = notify_library()
    if library is None:
        return result
    # Invoke the existing presentation owner, including its quiet polling
    # policy. Source only the library; never invoke hooks or notification UIs.
    script = ('. "$1" || exit; shift; cc_present "$@"; '
              'printf "%s\\0%s\\0%s" "$CC_STATUS" "$CC_SUBTITLE" "$CC_TAB_ONLY"')
    facts = dict(meta, event=kind, token=token if token is not None else meta.get('token'))
    try:
        response = run(['bash', '-c', script, 'cc-fleet', str(library),
                      *[str(facts.get(k) or '') for k in
                        ('event', 'ntype', 'msg', 'token', 'color', 'title', 'base', 'branch')]], check=False)
    except ProbeError:
        return result
    fields = response.stdout.split('\0')
    if response.returncode == 0 and len(fields) == 3:
        emoji, label, tab_only = fields
        result.update(status_emoji=emoji or result['status_emoji'], status_label=label or None)
        if meta.get('event') in ('notification', 'stop'):
            result['notification_eligible'] = result['notification_eligible'] or not bool(tab_only)
    return result


def capture(pane):
    return tmux('capture-pane', '-p', '-t', pane).stdout[-LIMIT:]


def inspect(pane, proc):
    tail = capture(pane['pane_id'])
    meta = metadata(pane, proc)
    identity = [pane['generation'], pane['pane_id'], pane['pane_pid'],
                pane['session_created'], proc['pid'], proc['born']]
    revision = digest([identity, pane['pane_current_command'], pane['cursor_x'],
                       pane['cursor_y'], pane['pane_in_mode'], tail])
    managed = bool(pane['@hq_owner'] and pane['session_name'].startswith(pane['@hq_owner']))
    workdir = (pane['@hq_workdir'] if managed else '') or pane['pane_current_path']
    menu = parse_menu(tail)
    status = 'waiting' if menu else {'active': 'working', 'waiting': 'waiting',
                                    'idle': 'idle'}.get(_detect_claude_status(tail), 'unknown')
    # Live menu/working/waiting signals outrank earlier hook metadata. Idle
    # or unknown terminal output can still use the last lifecycle event.
    if status not in ('working', 'waiting'):
        if meta.get('event') in ('prompt', 'tool', 'compact'):
            status = 'working'
        elif meta.get('event') == 'menu':
            status = 'waiting'
        elif meta.get('event') == 'stop':
            status = 'idle'
    # Keep producer tokens opaque. A new prompt suppresses the preceding
    # outcome; never reimplement cc_present's token semantics here.
    token = meta.get('token', '')
    tab_name = meta.get('tab_name', '')
    if meta.get('event') in ('prompt', 'tool', 'compact'):
        token = ''
    elif not token and tab_name and meta.get('tab_time', 0) >= meta.get('ts', 0):
        leading = tab_name.split(' ', 1)[0]
        if any(ord(char) > 0x2000 for char in leading):
            token = leading
    event_id = str(meta.get('ts') or meta.get('tab_time') or '')
    if menu:
        event_id += ':' + menu['signature']
    has_question = status == 'waiting' and bool(menu or meta.get('event') == 'menu')
    permission = status == 'waiting' and ('permission' in str(meta.get('ntype', '')).lower()
                                          or 'do you want to proceed' in tail.lower())
    attention_kind = 'question' if has_question else 'permission' if permission else None
    display = presentation(meta, menu, token=token, status=status, attention_kind=attention_kind)
    return dict(identity=identity, session=pane['session_name'], pane=pane['pane_id'],
                title=meta.get('title') or (pane['@hq_title'] if managed else '') or tab_name or pane['pane_title'] or pane['session_name'],
                workdir=workdir, status=status, status_emoji=display['status_emoji'],
                status_label=display['status_label'],
                has_question=has_question,
                attention_kind=attention_kind,
                color=meta.get('color') or None, updated_at=activity_time(meta, workdir), revision=revision,
                claude_session_id=meta.get('sid'), managed=managed,
                command=pane['pane_current_command'], tail=tail, menu=menu,
                event=meta.get('event'), event_id=event_id or None,
                menu_signature=menu['signature'] if menu else None,
                notification_eligible=(status == 'waiting' or display['notification_eligible']),
                _meta=meta, _pane=pane, _proc=proc)


def discover(identity=None):
    panes, procs = inventory(), processes()
    result = []
    for pane in panes:
        if identity and pane['pane_id'] != identity[1]:
            continue
        proc = target_process(pane, procs)
        if proc:
            item = inspect(pane, proc)
            if identity is None or item['identity'] == identity:
                result.append(item)
    if identity and not result:
        raise ProbeError('Claude target exited or pane generation changed', 409)
    return result


def public(item, full=False):
    result = {k: v for k, v in item.items() if not k.startswith('_')}
    if full:
        result['messages'] = transcript_messages(item['_meta'], item['workdir'])
        if not uuid_value(item['_meta'].get('sid')):
            result['transcript_status'] = 'identity_missing'
        elif transcript_path(item['_meta'], item['workdir']) is None:
            result['transcript_status'] = 'file_missing'
        else:
            result['transcript_status'] = 'available' if result['messages'] else 'parser_empty'
    else:
        result.pop('tail', None)
        result.pop('menu', None)
    return result


def empty_prompt(item):
    if item['menu'] or item['_pane']['pane_in_mode'] != '0':
        return False
    helper = shutil.which('cc-prompt-state')
    if not helper:
        library = notify_library()
        candidate = library.parent.parent / 'bin' / 'cc-prompt-state' if library else None
        if candidate and candidate.is_file():
            helper = str(candidate)
    source = globals().get('PROMPT_STATE_SOURCE', '')
    if helper:
        source = read_tail(helper, 65536)
    if source:
        if item['_pane'].get('cursor_x') != '2':
            return False
        # Execute the canonical decoration-aware reader unchanged. Its default
        # check excludes dim placeholders. Add a raw slash-command guard since
        # Claude can colour an entire typed /command as if it were decoration.
        guard = ('\ncode=$?\n[ "$code" -eq 0 ] || exit "$code"\n'
                 'RAW=1\nraw=$(box_text "$target") || exit 2\n'
                 'case "$(normalize "$raw")" in /*) exit 1;; esac\n')
        result = run(['bash', '-s', '--', item['pane']], input=source + guard, check=False)
        return result.returncode == 0 and not result.stdout.strip()
    # Conservative subset of cc-prompt-state's documented box contract. No
    # coloured placeholder handling: uncertainty rejects, never clears input.
    lines = item['tail'].splitlines()[-12:]
    borders = [i for i, line in enumerate(lines) if line.startswith('───')]
    if len(borders) < 2:
        return False
    content = lines[borders[-2] + 1:borders[-1]]
    return bool(content and content[0].startswith('❯') and
                not (content[0][1:] + ''.join(content[1:])).strip())


KEYS = {'Up', 'Down', 'Left', 'Right', 'Space', 'Enter', 'Escape', 'Tab', 'BTab', 'C-c'}


def validate_text(text):
    if not isinstance(text, str) or not text or len(text.encode()) > 16384:
        raise ProbeError('Text must contain 1–16384 UTF-8 bytes', 400)
    if any((ord(c) < 32 and c != '\n') or 127 <= ord(c) < 160 for c in text):
        raise ProbeError('Control characters are not allowed', 400)


def action_keys(item, request):
    action, menu = request.get('action'), item['menu']
    if action in ('send', 'type'):
        validate_text(request.get('text'))
        free = bool(menu and menu['options'][menu['focused_index']].get('kind') == 'free_text')
        if not empty_prompt(item) and not (action == 'type' and free):
            raise ProbeError('Text requires a verified empty Claude prompt or focused free-text option')
        return ['Enter'] if action == 'send' else []
    if action == 'select':
        index = request.get('index')
        if not menu or type(index) is not int or not 0 <= index < len(menu['options']):
            raise ProbeError('Option is not in the current recognized menu', 400)
        if menu['kind'] == 'review':
            raise ProbeError('Use submit for a review screen')
        delta = index - menu['focused_index']
        option = menu['options'][index]
        navigation = ['Down' if delta > 0 else 'Up'] * abs(delta)
        if option.get('kind') == 'free_text':
            return navigation
        key = 'Space' if menu['multi_select'] and option.get('kind') == 'option' else 'Enter'
        return navigation + [key]
    if action == 'submit':
        if not menu or menu['kind'] != 'review':
            raise ProbeError('Submit requires a recognized review screen')
        choices = [o['index'] for o in menu['options'] if re.match(r'^submit\b', o['label'], re.I)]
        if len(choices) != 1:
            raise ProbeError('Review has no unambiguous submit option')
        delta = choices[0] - menu['focused_index']
        return ['Down' if delta > 0 else 'Up'] * abs(delta) + ['Enter']
    if action == 'key':
        key = request.get('key')
        if key not in KEYS:
            raise ProbeError('Unsupported key', 400)
        return [key]
    raise ProbeError('Unsupported action', 400)


def perform(request):
    item = discover(request['identity'])[0]
    if item['revision'] != request.get('revision'):
        raise ProbeError('Screen changed; refresh before acting')
    if item['_pane']['pane_in_mode'] != '0' or '+' not in item['_proc']['state']:
        raise ProbeError('Claude is not the verified foreground target')
    keys = action_keys(item, request)
    # Re-observe after prompt helper/validation; refuse changes before writing.
    latest = discover(request['identity'])[0]
    if latest['revision'] != item['revision']:
        raise ProbeError('Screen changed during validation')
    pane = item['pane']
    if request['action'] in ('send', 'type'):
        text = request['text']
        if '\n' in text:
            name = 'cc-mobile-' + uuid.uuid4().hex
            tmux('load-buffer', '-b', name, '-', input=text)
            try:
                tmux('paste-buffer', '-p', '-d', '-b', name, '-t', pane)
            finally:
                tmux('delete-buffer', '-b', name, check=False)
        else:
            tmux('send-keys', '-t', pane, '-l', '--', text)
    if keys:
        tmux('send-keys', '-t', pane, *keys)
    return {'ok': True, 'request_id': request['request_id'], 'revision': item['revision'],
            'refresh_required': True}


def main(request):
    global DEADLINE
    DEADLINE = time.monotonic() + 15
    op = request.get('op')
    if op == 'snapshot':
        return {'sessions': [public(item) for item in discover()]}
    if op == 'detail':
        return public(discover(request['identity'])[0], full=True)
    if op == 'act':
        return perform(request)
    raise ProbeError('Unknown probe operation', 400)


if __name__ == '__main__':
    try:
        request = json.loads(base64.urlsafe_b64decode(sys.argv[1]))
        print(json.dumps({'result': main(request)}))
    except ProbeError as exc:
        print(json.dumps({'error': str(exc), 'status': exc.status}))
    except Exception:
        print(json.dumps({'error': 'Host probe failed', 'status': 503}))
