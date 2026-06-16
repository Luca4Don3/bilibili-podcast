import logging

from bilibili_podcast.sync import LOGGER, build_parser, setup_logging


def test_log_level_accepts_standard_levels_case_insensitive() -> None:
    parser = build_parser()

    assert parser.parse_args(["--log-level", "debug"]).log_level == "DEBUG"
    assert parser.parse_args(["--log-level", "INFO"]).log_level == "INFO"
    assert parser.parse_args(["--log-level", "warning"]).log_level == "WARNING"
    assert parser.parse_args(["--log-level", "error"]).log_level == "ERROR"
    assert parser.parse_args(["--log-level", "critical"]).log_level == "CRITICAL"


def test_debug_flag_remains_available() -> None:
    parser = build_parser()

    args = parser.parse_args(["--log-level", "ERROR", "--debug"])
    assert args.log_level == "ERROR"
    assert args.debug is True


def test_setup_logging_keeps_legacy_debug_positional(tmp_path) -> None:
    setup_logging(str(tmp_path), True)

    assert LOGGER.level == logging.DEBUG
