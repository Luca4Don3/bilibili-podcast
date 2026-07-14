from pathlib import Path


def test_nginx_template_uses_auth_request_and_token_safe_logs():
    config = Path("config/nginx.conf.example").read_text()

    assert "auth_request /_auth_rss" in config
    assert "auth_request /_auth_media" in config
    assert "$arg_token" in config
    assert "<new_auth_backup_port> backup;" in config
    assert "upstream bilibili_podcast_web" in config
    assert "<old_web_port>" not in config
    assert "error_page 403 = @rss_denied" in config
    assert "if ($rss_denial_status = 410) { return 410; }" in config
    assert "proxy_next_upstream_tries 2" in config
    assert "$request " not in config
    assert "$request_uri" not in config
    assert "$args" not in config
    assert "$http_cookie" not in config
    assert "error_log /dev/null" in config


def test_nginx_serves_authorized_files_directly_for_head_and_range_support():
    config = Path("config/nginx.conf.example").read_text()

    assert "proxy_pass_request_body off" in config
    assert config.count("proxy_method GET") == 2
    assert "try_files /current/$rss_token_hash/$rss_series.xml" in config
    assert "alias <server_path>/media/$media_series/$media_file" in config
    assert "proxy_pass http://bilibili_podcast_auth/auth" in config
    assert "proxy_pass http://bilibili_podcast_web" in config
