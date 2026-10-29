"""Entry point for the carbon-aware Kubernetes scheduler controller.

This module provides the main entry point for running the controller,
either as a standalone process or within a Kubernetes pod.
"""

import logging
import sys
from typing import Any, NoReturn

import kopf
import structlog

from carbon_scheduler import __version__
from carbon_scheduler.config import get_settings
from carbon_scheduler.metrics.prometheus import setup_metrics


def configure_logging() -> None:
    """Configure structured logging for the controller."""
    settings = get_settings()

    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    processors: list[Any]
    if settings.log_format == "json":
        processors = [
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    for noisy_logger in ["httpx", "httpcore", "urllib3"]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def run_controller() -> NoReturn:
    """Run the carbon scheduler controller.

    This function starts the kopf operator framework and blocks
    until the controller is shut down.
    """
    settings = get_settings()

    configure_logging()
    logger = structlog.get_logger(__name__)

    logger.info(
        "starting_carbon_scheduler",
        version=__version__,
        policy=settings.policy,
        grid_country=settings.grid_country,
        metrics_port=settings.metrics_port,
    )

    try:
        setup_metrics(port=settings.metrics_port)
        logger.info("metrics_server_started", port=settings.metrics_port)
    except Exception as e:
        logger.error("metrics_server_failed", error=str(e))

    kopf.configure(verbose=settings.log_level.upper() == "DEBUG")

    try:
        # Liveness and readiness are served by the metrics server (/healthz, /readyz).
        kopf.run(
            clusterwide=True,
            standalone=True,
        )
    except KeyboardInterrupt:
        logger.info("controller_interrupted")
    except Exception as e:
        logger.exception("controller_failed", error=str(e))
        sys.exit(1)

    sys.exit(0)


async def run_controller_async() -> None:
    """Run the controller asynchronously (for testing or embedding)."""
    settings = get_settings()

    configure_logging()
    logger = structlog.get_logger(__name__)

    logger.info(
        "starting_carbon_scheduler_async",
        version=__version__,
        policy=settings.policy,
    )

    await kopf.spawn_tasks(
        clusterwide=True,
    )


def main() -> NoReturn:
    """Main entry point."""
    run_controller()


if __name__ == "__main__":
    main()
