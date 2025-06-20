import time

from ash.api import api
from ash.config import config
from ash.health import get_health
from ash.logging import logger
from ash.models import ServiceConfigModel, ServiceModel
from ash.services import Services


def main() -> None:
    health = get_health()

    payload = {
        "hostname": config.hostname,
        "health": health,
        "services": Services.get_running_services(),
    }

    try:
        response = api.post("hosts/heartbeat", json=payload)
        if not response:
            logger.error("Heartbeat: No response")
            return
        services = response.json()["services"]
    except Exception:
        logger.error("Unable to connect Ayon server")
        return

    should_run: list[str] = []
    for service_data in services:
        service = ServiceModel(**service_data)

        should_run.append(service.name)
        if not service.data.image:
            continue

        service_config = ServiceConfigModel(**service.data.model_dump())

        Services.ensure_running(
            service_name=service.name,
            addon_name=service.addon_name,
            addon_version=service.addon_version,
            service=service.service,
            image=service.data.image,
            service_config=service_config,
        )

    Services.stop_orphans(should_run=should_run)


if __name__ == "__main__":
    while True:
        main()
        time.sleep(2)
