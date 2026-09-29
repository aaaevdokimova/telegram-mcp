"""Registration edits must preserve other plugins and user customizations."""
import json
from pathlib import Path
import subprocess
import sys
import pytest
from telegram_search_mcp import windows_install as installer


def test_new_marketplace_and_existing_settings_are_preserved():
    created = json.loads(installer.merge_marketplace(None))
    assert created['name'] == 'personal'
    entry = installer.marketplace_entry()
    assert created['plugins'] == [entry]
    before = json.dumps({'name': 'my_plugins', 'interface': {'displayName': 'Keep me'},
                         'plugins': [{'name': 'unrelated', 'source': './plugins/other'}]}).encode()
    after = json.loads(installer.merge_marketplace(before))
    assert after['interface'] == {'displayName': 'Keep me'}
    assert after['plugins'][0] == json.loads(before)['plugins'][0]
    assert after['plugins'][1] == entry


def test_unchanged_owned_entry_retains_exact_marketplace_bytes():
    before = installer.merge_marketplace(None)
    assert installer.merge_marketplace(before, owned_entry=installer.marketplace_entry()) is before


@pytest.mark.parametrize('change', ['unknown_owner', 'edited', 'duplicate'])
def test_never_overwrite_colliding_or_edited_registration(change):
    entry = installer.marketplace_entry()
    data = {'name': 'personal', 'plugins': [entry]}
    owned = installer.marketplace_entry()
    if change == 'unknown_owner':
        owned = None
    elif change == 'edited':
        data['plugins'][0]['source']['path'] = './plugins/custom'
    else:
        data['plugins'].append(entry)
    with pytest.raises(RuntimeError, match='not overwritten'):
        installer.merge_marketplace(json.dumps(data).encode(), owned_entry=owned)


@pytest.mark.parametrize('value', [[], {}, {'name': '../bad', 'plugins': []}, {'name': 'ok', 'plugins': {}}])
def test_invalid_marketplace_rejected(value):
    with pytest.raises(RuntimeError, match='Invalid'):
        installer.merge_marketplace(json.dumps(value).encode())


def test_launcher_does_not_interpolate_install_path_as_powershell_code():
    dangerous = Path("a'; Start-Process bad; #")
    text = installer.windows_launcher_text(dangerous, 'telegram_search_mcp.server')
    assert str(dangerous) not in text
    assert '$PSScriptRoot' in text
    assert '-I -X utf8 -m telegram_search_mcp.server @args' in text
    assert 'ReparsePoint' in text
    with pytest.raises(ValueError):
        installer.windows_launcher_text(dangerous, 'bad;code')


def test_plugin_template_is_fail_closed_until_configured():
    source = Path(__file__).resolve().parents[1]
    release = installer._release_module(source)
    assets = release.windows_assets(release.inventory(source))
    manifest = json.loads(assets['plugins/telegram-mcp-work/.codex-plugin/plugin.json'])
    config = json.loads(assets['plugins/telegram-mcp-work/.mcp.json'])
    assert manifest['name'] == installer.PLUGIN_NAME
    assert manifest['mcpServers'] == './.mcp.json'
    assert 'exit 1' in config['mcpServers']['telegram']['args'][-1]


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows PowerShell pipe behavior')
@pytest.mark.parametrize('hidden_console', [False, True])
def test_native_launcher_preserves_unicode_json_stdio(tmp_path, hidden_console):
    """A disposable fake MCP server exercises the real launcher without a profile."""
    root = tmp_path / 'Телеграм install'
    version = root / 'version-0.9.0-abcdef012345'
    version.mkdir(parents=True)
    (version / installer.VERSION_MARKER).write_bytes(b'telegram-search-mcp\n')
    (root / 'current.json').write_text(json.dumps({'version': version.name}), encoding='utf-8')
    environment = version / '.venv'
    subprocess.run([sys.executable, '-I', '-m', 'venv', '--without-pip', str(environment)],
                   check=True, capture_output=True, timeout=60)
    package = environment / 'Lib/site-packages/telegram_search_mcp'
    package.mkdir()
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'server.py').write_text('''import json
import sys
assert sys.flags.isolated and sys.flags.utf8_mode
# MCP uses UTF-8 text wrappers around these binary pipes.
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    if request['method'] == 'initialize':
        result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'Тест 🌍', 'version': '1'}}
    elif request['method'] == 'tools/call':
        result = {'content': [{'type': 'text', 'text': request['params']['arguments']['text']}]}
    else:
        raise AssertionError(request)
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result},
                     ensure_ascii=False), flush=True)
''', encoding='utf-8')
    launcher = root / 'launch-mcp.ps1'
    launcher.write_text(installer.windows_launcher_text(root, 'telegram_search_mcp.server'),
                        encoding='utf-8', newline='\n')
    unicode_text = 'Привет, мир — café 🌍 中文'
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
         'params': {'name': 'echo', 'arguments': {'text': unicode_text}}},
    ]
    wire = ''.join(json.dumps(request, ensure_ascii=False) + '\n' for request in requests).encode('utf-8')
    result = subprocess.run(
        [str(installer.powershell_path()), '-NoLogo', '-NoProfile', '-NonInteractive',
         '-ExecutionPolicy', 'Bypass', '-File', str(launcher)],
        input=wire, capture_output=True, timeout=30, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if hidden_console else 0,
    )
    responses = [json.loads(line) for line in result.stdout.decode('utf-8').splitlines()]
    assert len(responses) == 2, 'Launcher emitted extra stdout or lost a protocol response'
    assert responses[0]['result']['serverInfo']['name'] == 'Тест 🌍'
    assert responses[1]['result']['content'][0]['text'] == unicode_text
    assert result.stderr == b''
