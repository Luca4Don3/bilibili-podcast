import sys
from types import SimpleNamespace

import pytest

sys.modules.setdefault("uvicorn", SimpleNamespace(run=None))

from bilibili_podcast.web import runner


def test_web_runner_allows_process_only_listener_overrides(monkeypatch) -> None:
    snapshot = SimpleNamespace(
        web=SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=8000)),
    )
    manager = SimpleNamespace(load=lambda: snapshot)
    monkeypatch.setattr(runner, "ConfigManager", lambda: manager)
    monkeypatch.setattr(runner, "create_app", lambda selected, manager: "application")
    calls = []
    monkeypatch.setattr(runner.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert runner.main(["--host", "127.0.0.2", "--port", "8101"]) == 0
    assert calls == [(('application',), {
        "host": "127.0.0.2", "port": 8101, "access_log": False,
    })]
    assert snapshot.web.server.port == 8000


def test_web_runner_rejects_invalid_override_port() -> None:
    with pytest.raises(SystemExit) as exc:
        runner.main(["--port", "0"])
    assert exc.value.code == 2


def test_web_runner_rejects_non_loopback_override_host() -> None:
    with pytest.raises(SystemExit) as exc:
        runner.main(["--host", "0.0.0.0"])
    assert exc.value.code == 2
