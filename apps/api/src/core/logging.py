import logging


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s request_id=%(request_id)s %(message)s",
    )
