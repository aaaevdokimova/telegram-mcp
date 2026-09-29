"""Registration edits must preserve other plugins and user customizations."""
import json
from pathlib import Path
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
    assert '-I -m telegram_search_mcp.server @args' in text
    assert 'ReparsePoint' in text
    with pytest.raises(ValueError):
        installer.windows_launcher_text(dangerous, 'bad;code')


def test_plugin_template_is_fail_closed_until_configured():
    source = Path(__file__).resolve().parents[1]
    template = source / 'plugins/telegram-mcp-work'
    manifest = json.loads((template / '.codex-plugin/plugin.json').read_text())
    config = json.loads((template / '.mcp.json').read_text())
    assert manifest['name'] == installer.PLUGIN_NAME
    assert manifest['mcpServers'] == './.mcp.json'
    assert 'exit 1' in config['mcpServers']['telegram']['args'][-1]
