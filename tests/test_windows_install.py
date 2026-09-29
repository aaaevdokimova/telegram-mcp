"""Registration edits must preserve other plugins and user customizations."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import pytest
from telegram_search_mcp import windows_install as installer


@pytest.fixture
def windows_smoke():
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'smoke-install-windows.py'
    spec = importlib.util.spec_from_file_location('windows_smoke', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def windows_identity(build='19045', product_type=1):
    return {
        'ProductName': 'Windows 10 Pro', 'DisplayVersion': '22H2',
        'CurrentBuild': build, 'UBR': 1, 'Version': f'10.0.{build}',
        'Caption': 'Microsoft Windows', 'OSArchitecture': '64-bit',
        'ProductType': product_type, 'PowerShellVersion': '5.1',
    }


@pytest.mark.parametrize(('build', 'expected'), [('10240', '10'), ('19045', '10'), ('22000', '11'), ('26100', '11')])
def test_acceptance_uses_desktop_kernel_build_not_registry_product_name(windows_smoke, build, expected):
    # Windows 11 commonly retains "Windows 10" in the registry ProductName.
    assert windows_smoke.require_windows_version(windows_identity(build), expected) == ('desktop', expected)


@pytest.mark.parametrize('product_type', [2, 3])
@pytest.mark.parametrize('expected', ['10', '11'])
def test_acceptance_never_substitutes_windows_server_for_desktop(windows_smoke, product_type, expected):
    identity = windows_identity('26100', product_type)
    assert windows_smoke.require_windows_version(identity, None) == ('server', None)
    with pytest.raises(RuntimeError, match='actual OS is Windows Server'):
        windows_smoke.require_windows_version(identity, expected)


@pytest.mark.parametrize(('build', 'expected'), [('19045', '11'), ('26100', '10')])
def test_acceptance_rejects_the_other_desktop_version(windows_smoke, build, expected):
    with pytest.raises(RuntimeError, match='Expected desktop Windows'):
        windows_smoke.require_windows_version(windows_identity(build), expected)


@pytest.mark.parametrize('invalid_type', [None, True, '1', 0, 4])
def test_acceptance_rejects_missing_or_invalid_product_type(windows_smoke, invalid_type):
    with pytest.raises(RuntimeError, match='ProductType'):
        windows_smoke.require_windows_version(windows_identity(product_type=invalid_type), '10')


def test_acceptance_rejects_conflicting_os_evidence(windows_smoke):
    identity = windows_identity()
    identity['Version'] = '10.0.26100'
    with pytest.raises(RuntimeError, match='evidence disagree'):
        windows_smoke.require_windows_version(identity, '10')


def test_failed_desktop_acceptance_replaces_old_success_report_before_installation(
    windows_smoke, monkeypatch, tmp_path,
):
    powershell = tmp_path / 'powershell.exe'
    powershell.touch()
    report = tmp_path / 'acceptance.json'
    report.write_text(json.dumps({'status': 'passed'}), encoding='utf-8')
    missing_archive = tmp_path / 'must-not-be-read.zip'
    monkeypatch.setattr(sys, 'argv', ['smoke', str(missing_archive), '--expected-windows', '11',
                                    '--report-path', str(report)])
    monkeypatch.setattr(windows_smoke.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(windows_smoke.platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(windows_smoke, 'system_powershell', lambda: powershell)
    monkeypatch.setattr(windows_smoke, 'read_windows_identity', lambda _: windows_identity('26100', 3))
    monkeypatch.setattr(windows_smoke, 'exercise_archive', lambda *_: pytest.fail('Server must not run desktop checks'))
    with pytest.raises(RuntimeError, match='actual OS is Windows Server'):
        windows_smoke.main()
    evidence = json.loads(report.read_text(encoding='utf-8'))
    assert evidence['status'] == 'failed'
    assert evidence['windows']['ProductType'] == 3
    assert evidence['windows_family'] == 'server'
    assert evidence['detected_desktop_windows_version'] is None
    assert evidence['expected_windows_version'] == '11'
    assert evidence['checks'] == []
    assert evidence['client_ui_tested'] is False
    assert evidence['telegram_authorization_performed'] is False
    assert evidence['error']['type'] == 'RuntimeError'


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows DLL mapping lifetime')
def test_smoke_powershell_lookup_does_not_pin_pywin32_dll_in_verifier():
    # A mapped cached pywin32 DLL prevents deleting uv's hardlinks in disposable
    # installations. Use a fresh process: other pytest tests may map it normally.
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'smoke-install-windows.py'
    check = '''
import ctypes
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('windows_smoke', sys.argv[1])
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
dll = f'pywintypes{sys.version_info.major}{sys.version_info.minor}.dll'
assert not kernel32.GetModuleHandleW(dll), 'Test process must initially have no pywin32 DLL mapping'
assert smoke.system_powershell().is_file()
assert 'win32api' not in sys.modules and 'pywintypes' not in sys.modules
assert not kernel32.GetModuleHandleW(dll), 'Verifier must not pin the DLL while cleaning hardlinked environments'
'''
    subprocess.run([sys.executable, '-I', '-c', check, str(script)],
                   check=True, capture_output=True, timeout=60)


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
    # Reproduce an app started from another PowerShell version: its inherited
    # module path does not contain this runtime's compatible built-in modules.
    external_modules = tmp_path / 'external modules'
    external_modules.mkdir()
    child_environment = {key: value for key, value in os.environ.items() if key.upper() != 'PSMODULEPATH'}
    child_environment['PSModulePath'] = str(external_modules)
    result = subprocess.run(
        [str(installer.powershell_path()), '-NoLogo', '-NoProfile', '-NonInteractive',
         '-ExecutionPolicy', 'Bypass', '-File', str(launcher)],
        input=wire, capture_output=True, timeout=30, check=True, env=child_environment,
        creationflags=subprocess.CREATE_NO_WINDOW if hidden_console else 0,
    )
    responses = [json.loads(line) for line in result.stdout.decode('utf-8').splitlines()]
    assert len(responses) == 2, 'Launcher emitted extra stdout or lost a protocol response'
    assert responses[0]['result']['serverInfo']['name'] == 'Тест 🌍'
    assert responses[1]['result']['content'][0]['text'] == unicode_text
    assert result.stderr == b''
