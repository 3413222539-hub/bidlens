"""Runtime environment wins over optional local .env; never log values."""
import os
from pathlib import Path

ENV_FILE = Path(__file__).with_name('.env')


def settings():
    config = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding='utf-8-sig').splitlines():
            name, sep, value = line.partition('=')
            if sep and not name.strip().startswith('#'):
                config[name.strip()] = value.strip().strip('\"\'')
    config.update(os.environ)
    return config


def require(*names):
    config = settings()
    missing = [name for name in names if not config.get(name, '').strip()]
    if missing:
        raise ValueError('缺少配置：' + ', '.join(missing) + '；请设置环境变量或项目 .env')
    return {name: config[name].strip() for name in names}
