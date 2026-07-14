from pathlib import Path


def test_nginx_template_uses_auth_request_and_token_safe_logs():
    config = Path("config/nginx.conf.example").read_text()

    assert "auth_request /_auth_rss" in config
    assert "auth_request /_auth_media" in config
    assert "$arg_token" in config
    assert "backup;" in config
    assert "$request " not in config
    assert "$request_uri" not in config
    assert "$args" not in config
    assert "$http_cookie" not in config
    assert "error_log /dev/null" in config


def test_nginx_serves_authorized_files_directly_for_head_and_range_support():
    config = Path("config/nginx.conf.example").read_text()

    assert "proxy_pass_request_body off" in config
    assert "try_files /current/$rss_token_hash/$rss_series.xml" in config
    assert "alias <server_path>/media/$media_series/$media_file" in config
    assert "proxy_pass http://bilibili_podcast_web/auth" in config
