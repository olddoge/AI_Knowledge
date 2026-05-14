from src.config import get_bool_config, load_env
from src.logging_module import setup_logger
from src.pipeline import build_pipeline_config, run_pipeline


def main() -> None:
    config = load_env()
    logger = setup_logger(enable_logging=get_bool_config(config, "ENABLE_LOGGING", True))
    run_pipeline(build_pipeline_config(config), logger)


if __name__ == "__main__":
    main()
