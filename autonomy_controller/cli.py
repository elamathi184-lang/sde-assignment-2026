from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import httpx

from .service import Controller
from .store import Store


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the outcome-first controller against the simulator.")
    parser.add_argument("--url", default=os.environ.get("SIM_URL", "http://127.0.0.1:8700"))
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("CONTROLLER_STATE_DIR", "runs/controller-state")))
    parser.add_argument(
        "--resume",
        action="store_true",
        default=os.environ.get("CONTROLLER_RESUME", "").lower() in ("1", "true", "yes"),
        help="resume state only when reconnecting to the same simulator run",
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with httpx.Client(base_url=args.url.rstrip("/"), timeout=120) as client:
        response = client.get("/sim/state")
        response.raise_for_status()
        scenario = response.json()["scenario"]
    store = Store(args.state_dir / f"{scenario}.sqlite3", resume=args.resume)
    controller = Controller(args.url, store)
    try:
        controller.run()
    finally:
        controller.close()
        store.close()

if __name__ == "__main__":
    main()
