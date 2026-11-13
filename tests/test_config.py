import pytest
import app_config
from tender_assistant import load_config
from mineru_pdf import read_token


def test_environment_only_and_precedence(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    monkeypatch.setattr(app_config, 'ENV_FILE', path)
    for name in ('DASHSCOPE_API_KEY', 'DASHSCOPE_BASE_URL', 'DASHSCOPE_MODEL', 'MINERU_API_TOKEN'):
        monkeypatch.setenv(name, 'environment-value')
    assert read_token() == 'environment-value'
    assert load_config()['DASHSCOPE_MODEL'] == 'environment-value'
    path.write_text('DASHSCOPE_MODEL=local-value\n', encoding='utf-8')
    assert load_config()['DASHSCOPE_MODEL'] == 'environment-value'
    monkeypatch.delenv('DASHSCOPE_MODEL')
    assert load_config()['DASHSCOPE_MODEL'] == 'local-value'


def test_missing_credentials_are_not_echoed(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, 'ENV_FILE', tmp_path / 'missing')
    monkeypatch.delenv('MINERU_API_TOKEN', raising=False)
    with pytest.raises(ValueError, match='MINERU_API_TOKEN'):
        read_token()
