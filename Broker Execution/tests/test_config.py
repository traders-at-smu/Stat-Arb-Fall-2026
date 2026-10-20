# Configuration changes must preserve safe defaults and support private
# research paths.
import json
from strategy.config import load_settings, research_root
from strategy.paper_trader import ENTRY_EXECUTION_LOCK


def test_default_settings_keep_paper_diagnostic_and_execution_lock():
    settings = load_settings()
    assert settings['ibkr']['host'] == '127.0.0.1'
    assert settings['ibkr']['port'] == 7497
    assert settings['book_size'] == 100000
    assert settings['research_directory'] is None
    assert ENTRY_EXECUTION_LOCK is True


def test_explicit_private_research_path(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(
        json.dumps({'research_directory': str(tmp_path / 'private')})
    )
    monkeypatch.setenv('BROKER_EXECUTION_CONFIG', str(config))
    assert research_root() == tmp_path / 'private'
