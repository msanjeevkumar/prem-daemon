import logging
import shutil

import docker
import psutil

from app.core import utils

logger = logging.getLogger(__name__)


def get_services(interface_id: str = None) -> dict:
    docker_client = utils.get_docker_client()

    free_memory, total_memory = get_free_total_memory()

    images = docker_client.images.list()
    containers = docker_client.containers.list()

    if interface_id is None:
        services = utils.SERVICES
    else:
        services = [
            service
            for service in utils.SERVICES
            if interface_id in service["interfaces"]
        ]

    rich_services = []
    for service in services:
        service["running"] = False
        service["downloaded"] = False
        service["enoughMemory"] = True
        service["enoughSystemMemory"] = True

        if (
            "memoryRequirements" in service["modelInfo"]
            and free_memory * 1024 < service["modelInfo"]["memoryRequirements"]
        ):
            service["enoughMemory"] = False

        if (
            "memoryRequirements" in service["modelInfo"]
            and total_memory * 1024 < service["modelInfo"]["memoryRequirements"]
        ):
            service["enoughSystemMemory"] = False

        for container in containers:
            if container.name == service["id"]:
                service["running"] = True

        service_image = service["dockerImage"].split(":")[0]

        service_tags = []
        for image in images:
            if len(image.tags) > 0 and service_image == image.tags[0].split(":")[0]:
                service_tags.append(image.tags[0])

        if len(service_tags) > 0:
            service["downloaded"] = True
            if service["dockerImage"] not in service_tags:
                service["needsUpdate"] = True
            else:
                service["needsUpdate"] = False
                service["downloadedDockerImage"] = service["dockerImage"]
        else:
            service["downloaded"] = False

        rich_services.append(service)

    return rich_services


def get_service_by_id(service_id: str) -> dict:
    docker_client = utils.get_docker_client()

    free_memory, total_memory = get_free_total_memory()

    images = docker_client.images.list()
    containers = docker_client.containers.list()

    for service in utils.SERVICES:
        if service["id"] == service_id:
            service["running"] = False
            service["downloaded"] = False
            service["enoughMemory"] = True
            service["enoughSystemMemory"] = True

            if (
                "memoryRequirements" in service["modelInfo"]
                and free_memory * 1024 < service["modelInfo"]["memoryRequirements"]
            ):
                service["enoughMemory"] = False

            if (
                "memoryRequirements" in service["modelInfo"]
                and total_memory * 1024 < service["modelInfo"]["memoryRequirements"]
            ):
                service["enoughSystemMemory"] = False

            for container in containers:
                if container.name == service["id"]:
                    service["running"] = True
                    service["runningPort"] = list(container.ports.values())[0][0][
                        "HostPort"
                    ]
                    try:
                        service["volumeName"] = container.attrs["Mounts"][0]["Name"]
                    except Exception:
                        service["volumeName"] = None

            service_image = service["dockerImage"].split(":")[0]

            service_tags = []
            for image in images:
                if len(image.tags) > 0 and service_image == image.tags[0].split(":")[0]:
                    service_tags.append(image.tags[0])

            if len(service_tags) > 0:
                service["downloaded"] = True
                if service["dockerImage"] not in service_tags:
                    service["needsUpdate"] = True
                else:
                    service["needsUpdate"] = False
                    service["downloadedDockerImage"] = service["dockerImage"]
            else:
                service["downloaded"] = False
            return service


def stop_all_running_services():
    client = utils.get_docker_client()
    containers = client.containers.list()
    services = get_services()

    for container in containers:
        if container.name in [service["id"] for service in services]:
            logger.info(f"Stopping container {container.name}")
            container.remove(force=True)


def run_container_with_retries(service_object):
    client = utils.get_docker_client()

    try:
        client.containers.get(service_object["id"]).remove(force=True)
    except Exception as error:
        logger.info(f"Failed to remove container {error}.")

    port = service_object["defaultPort"] + 1

    if utils.is_gpu_available():
        device_requests = [
            docker.types.DeviceRequest(device_ids=["all"], capabilities=[["gpu"]])
        ]
    else:
        device_requests = []

    volumes = {}
    if "volumePath" in service_object:
        try:
            volume_name = f"prem-{service_object['id']}-data"
            volume = client.volumes.create(name=volume_name)
            volumes = {volume.id: {"bind": service_object["volumePath"], "mode": "rw"}}
        except Exception as error:
            logger.error(f"Failed to create volume {error}")

    for _ in range(10):
        try:
            client.containers.run(
                service_object["downloadedDockerImage"],
                auto_remove=True,
                detach=True,
                ports={f"{service_object['defaultPort']}/tcp": port},
                name=service_object["id"],
                volumes=volumes,
                device_requests=device_requests,
            )
            return port
        except Exception as error:
            logger.error(f"Failed to start {error}")
            port += 1
    return None


def get_docker_stats(container_name: str):
    total, _, _ = shutil.disk_usage("/")

    client = utils.get_docker_client()
    container = client.containers.get(container_name)
    value = container.stats(stream=False)
    cpu_percentage, memory_usage, memory_limit, memory_percentage = utils.format_stats(
        value
    )
    storage_usage = container.image.attrs["Size"]

    return {
        "cpu_percentage": round(cpu_percentage, 2),
        "memory_usage": round(memory_usage / 1024, 2),
        "memory_limit": round(memory_limit, 2),
        "memory_percentage": memory_percentage,
        "storage_percentage": round((storage_usage / total) * 100, 2),
        "storage_usage": round(storage_usage // (2**30), 2),
        "storage_limit": total // (2**30),
    }


def get_system_stats_all():
    total, used, _ = shutil.disk_usage("/")

    memory_info = psutil.virtual_memory()
    memory_limit = memory_info.total / (1024.0**3)
    memory_usage = memory_info.used / (1024.0**3)

    cpu_percentage = psutil.cpu_percent(interval=1)

    return {
        "cpu_percentage": round(cpu_percentage, 2),
        "memory_usage": round(memory_usage, 2),
        "memory_limit": round(memory_limit, 2),
        "memory_percentage": round(memory_info.percent, 2),
        "storage_percentage": round((used / total) * 100, 2),
        "storage_usage": used // (2**30),
        "storage_limit": total // (2**30),
    }


def get_gpu_stats_all():
    if utils.is_gpu_available():
        gpu_name, total_memory, used_memory, memory_percentage = utils.get_gpu_info()
        return {
            "gpu_name": gpu_name,
            "total_memory": round(total_memory / 1024, 2),
            "used_memory": round(used_memory / 1024, 2),
            "memory_percentage": memory_percentage,
        }
    return {}


def system_prune():
    client = utils.get_docker_client()
    client.containers.prune()
    client.volumes.prune()
    client.images.prune()
    client.networks.prune()


def get_free_total_memory():
    if utils.is_gpu_available():
        gpu_values = get_gpu_stats_all()
        free_memory = gpu_values["total_memory"] - gpu_values["used_memory"]
        return free_memory, gpu_values["total_memory"]
    else:
        values = get_system_stats_all()
        free_memory = values["memory_limit"] - values["memory_usage"]
        return free_memory, values["memory_limit"]
