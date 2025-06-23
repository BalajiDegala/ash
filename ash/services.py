from typing import Any

import docker
from docker.models.containers import Container
from kubernetes import client as k8s_client
from kubernetes.config import (
    ConfigException,
    load_incluster_config,
    load_kube_config,
)  # type: ignore[attr-defined]

from ash.config import config
from ash.logging import logger
from ash.models import ServiceConfigModel
from ash.service_logging import ServiceLogger
from ash.utils import slugify


class Services:
    client: docker.DockerClient | None = None
    k8s_api: k8s_client.CoreV1Api | None = None
    prefix: str = "io.ayon.service"

    @classmethod
    def connect(cls) -> None:
        if config.use_k8s:
            try:
                load_incluster_config()
            except ConfigException:
                load_kube_config()
            cls.k8s_api = k8s_client.CoreV1Api()
        else:
            cls.client = docker.DockerClient(base_url="unix://var/run/docker.sock")

    @classmethod
    def get_running_services(cls) -> list[str]:
        result: list[str] = []
        if cls.client is None and cls.k8s_api is None:
            cls.connect()

        if config.use_k8s:
            if cls.k8s_api is None:
                return result
            pods = cls.k8s_api.list_namespaced_pod(
                config.k8s_namespace,
                label_selector=f"{cls.prefix}.service_name",
            )
            for pod in pods.items:
                service_name = pod.metadata.labels.get(f"{cls.prefix}.service_name")
                if service_name:
                    result.append(service_name)
            return result

        if cls.client is None:
            return result

        for container in cls.client.containers.list():
            labels = container.labels
            if service_name := labels.get(f"{cls.prefix}.service_name"):
                result.append(service_name)
        return result

    @classmethod
    def stop_orphans(cls, should_run: list[str]) -> None:
        if cls.client is None and cls.k8s_api is None:
            cls.connect()

        if config.use_k8s:
            if cls.k8s_api is None:
                return
            pods = cls.k8s_api.list_namespaced_pod(
                config.k8s_namespace,
                label_selector=f"{cls.prefix}.service_name",
            )
            for pod in pods.items:
                service_name = pod.metadata.labels.get(f"{cls.prefix}.service_name")
                if service_name in should_run:
                    continue
                logger.warning(f"Stopping service {service_name}")
                cls.k8s_api.delete_namespaced_pod(
                    pod.metadata.name,
                    config.k8s_namespace,
                )
            return

        if cls.client is None:
            return
        for container in cls.client.containers.list():
            labels = container.labels
            if service_name := labels.get(f"{cls.prefix}.service_name"):
                if service_name in should_run:
                    continue
                logger.warning(f"Stopping service {service_name}")
                container.stop()

    @classmethod
    def spawn(
        cls,
        image: str,
        hostname: str,
        environment: dict[str, str],
        labels: dict[str, str],
        volumes: list[str] | None,
        *,
        ports: list[str] | None = None,
        mem_limit: str | None = None,
        user: str | None = None,
        **kwargs: Any,
    ) -> Container | None:
        if cls.client is None and cls.k8s_api is None:
            cls.connect()

        if config.use_k8s:
            if cls.k8s_api is None:
                return None

            env = [k8s_client.V1EnvVar(name=k, value=v) for k, v in environment.items()]
            volume_mounts = []
            volumes_spec = []
            if volumes:
                for idx, bind in enumerate(volumes):
                    parts = bind.split(":")
                    if len(parts) < 2:
                        continue
                    host_path, mount_path = parts[:2]
                    read_only = len(parts) == 3 and parts[2] == "ro"
                    volumes_spec.append(
                        k8s_client.V1Volume(
                            name=f"vol-{idx}",
                            host_path=k8s_client.V1HostPathVolumeSource(path=host_path),
                        )
                    )
                    volume_mounts.append(
                        k8s_client.V1VolumeMount(
                            name=f"vol-{idx}",
                            mount_path=mount_path,
                            read_only=read_only,
                        )
                    )

            container_ports = []
            if ports:
                for p in ports:
                    port_parts = p.split(":")
                    if len(port_parts) == 2:
                        host_p, cont_p = port_parts
                        container_ports.append(
                            k8s_client.V1ContainerPort(
                                container_port=int(cont_p),
                                host_port=int(host_p),
                            )
                        )
                    else:
                        container_ports.append(
                            k8s_client.V1ContainerPort(
                                container_port=int(port_parts[0])
                            )
                        )

            security_context = None
            if user is not None:
                try:
                    security_context = k8s_client.V1SecurityContext(
                        run_as_user=int(user)
                    )
                except ValueError:
                    logger.warning(
                        f"Invalid user value '{user}' for service {hostname}"
                    )

            resources = None
            if mem_limit is not None:
                resources = k8s_client.V1ResourceRequirements(
                    limits={"memory": mem_limit}
                )

            container_obj = k8s_client.V1Container(
                name=hostname,
                image=image,
                env=env,
                volume_mounts=volume_mounts or None,
                ports=container_ports or None,
                security_context=security_context,
                resources=resources,
            )
            pod_spec = k8s_client.V1PodSpec(
                containers=[container_obj],
                restart_policy="Never",
                volumes=volumes_spec or None,
            )
            metadata = k8s_client.V1ObjectMeta(name=hostname, labels=labels)
            pod = k8s_client.V1Pod(metadata=metadata, spec=pod_spec)
            cls.k8s_api.create_namespaced_pod(namespace=config.k8s_namespace, body=pod)
            return None

        if cls.client is None:
            return None

        assert cls.client is not None

        docker_ports = None
        if ports:
            docker_ports = {}
            for mapping in ports:
                parts = mapping.split(":")
                if len(parts) == 2:
                    host_port, container_port = parts
                else:
                    host_port = container_port = parts[0]
                try:
                    docker_ports[int(container_port)] = int(host_port)
                except ValueError:
                    logger.warning(
                        f"Invalid port mapping '{mapping}' for service {hostname}"
                    )

        container: Container = cls.client.containers.run(
            image,
            detach=True,
            auto_remove=True,
            environment=environment,
            hostname=hostname,
            network_mode=config.network_mode,
            network=config.network,
            name=hostname,
            labels=labels,
            volumes=volumes,
            ports=docker_ports,
            mem_limit=mem_limit,
            user=user,
            **kwargs,
        )
        return container

    @classmethod
    def ensure_running(
        cls,
        service_name: str,
        addon_name: str,
        addon_version: str,
        service: str,
        image: str,
        service_config: ServiceConfigModel,
    ) -> None:
        if cls.client is None and cls.k8s_api is None:
            cls.connect()

        if config.use_k8s:
            if cls.k8s_api is None:
                return
            pods = cls.k8s_api.list_namespaced_pod(
                config.k8s_namespace,
                label_selector=f"{cls.prefix}.service_name={service_name}",
            )
            if pods.items:
                return
        else:
            if cls.client is None:
                return

        #
        # Check whether it is running already (Docker mode)
        #

        container = None

        if not config.use_k8s:
            assert cls.client is not None
            for container in cls.client.containers.list():
                labels = container.labels

                if labels.get(f"{cls.prefix}.service_name") != service_name:
                    continue

                try:
                    assert labels.get(f"{cls.prefix}.service") == service
                    assert labels.get(f"{cls.prefix}.addon_name") == addon_name
                    assert labels.get(f"{cls.prefix}.addon_version") == addon_version
                except AssertionError:
                    logger.error("SERVICE MISMATCH. This shouldn't happen. Stopping.")
                    container.stop()

                break
            else:
                container = None
        else:
            container = None

        if container is None:
            # And start it
            addon_string = f"{addon_name}:{addon_version}/{service}"
            logger.info(f"Starting {service_name} {addon_string} (image: {image})")

            kwargs = service_config.model_dump()
            hostname = slugify(f"aysvc-{service_name}")

            environment = {
                "AYON_SERVER_URL": config.server_url,
                "AYON_API_KEY": config.api_key,
                "AYON_ADDON_NAME": addon_name,
                "AYON_ADDON_VERSION": addon_version,
                "AYON_SERVICE_NAME": service_name,
                **kwargs.pop("env", {}),
            }

            labels = {
                f"{cls.prefix}.service_name": service_name,
                f"{cls.prefix}.service": service,
                f"{cls.prefix}.addon_name": addon_name,
                f"{cls.prefix}.addon_version": addon_version,
            }

            volumes = kwargs.pop("volumes", None) or []
            for bind_mount in config.binds:
                # add global storage from the ash itself
                if not isinstance(bind_mount, str):
                    continue
                target = bind_mount.split(":")[1]
                if target.startswith("/storage"):
                    volumes.append(bind_mount)

            container = cls.spawn(
                image,
                hostname,
                environment,
                labels,
                volumes or None,
                **kwargs,
            )

        # Ensure container logger is running
        if not config.use_k8s and container is not None:
            ServiceLogger.add(service_name, container)
