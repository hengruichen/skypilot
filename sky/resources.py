"""Resources: compute requirements of Tasks."""
import functools
import textwrap
from typing import Dict, List, Optional, Set, Tuple, Union

import colorama
from typing_extensions import Literal

from sky import clouds
from sky import global_user_state
from sky import sky_logging
from sky import skypilot_config
from sky import spot
from sky.clouds import service_catalog
from sky.provision import docker_utils
from sky.skylet import constants
from sky.utils import accelerator_registry
from sky.utils import common_utils
from sky.utils import log_utils
from sky.utils import resources_utils
from sky.utils import schemas
from sky.utils import tpu_utils
from sky.utils import ux_utils

logger = sky_logging.init_logger(__name__)

_DEFAULT_DISK_SIZE_GB = 256


class Resources:
    """Resources: compute requirements of Tasks.

    This class is immutable once created (to ensure some validations are done
    whenever properties change). To update the property of an instance of
    Resources, use `resources.copy(**new_properties)`.

    Used:

    * for representing resource requests for tasks/apps
    * as a "filter" to get concrete launchable instances
    * for calculating billing
    * for provisioning on a cloud

    """
    # If any fields changed, increment the version. For backward compatibility,
    # modify the __setstate__ method to handle the old version.
    _VERSION = 13

    def __init__(
        self,
        cloud: Optional[clouds.Cloud] = None,
        instance_type: Optional[str] = None,
        cpus: Union[None, int, float, str] = None,
        memory: Union[None, int, float, str] = None,
        accelerators: Union[None, str, Dict[str, int]] = None,
        accelerator_args: Optional[Dict[str, str]] = None,
        use_spot: Optional[bool] = None,
        spot_recovery: Optional[str] = None,
        region: Optional[str] = None,
        zone: Optional[str] = None,
        image_id: Union[Dict[str, str], str, None] = None,
        disk_size: Optional[int] = None,
        disk_tier: Optional[Literal['high', 'medium', 'low']] = None,
        ports: Optional[Union[int, str, List[str], Tuple[str]]] = None,
        # Internal use only.
        _docker_login_config: Optional[docker_utils.DockerLoginConfig] = None,
        _is_image_managed: Optional[bool] = None,
    ):
        """Initialize a Resources object.

        All fields are optional.  ``Resources.is_launchable`` decides whether
        the Resources is fully specified to launch an instance.

        Examples:
          .. code-block:: python

            # Fully specified cloud and instance type (is_launchable() is True).
            sky.Resources(clouds.AWS(), 'p3.2xlarge')
            sky.Resources(clouds.GCP(), 'n1-standard-16')
            sky.Resources(clouds.GCP(), 'n1-standard-8', 'V100')

            # Specifying required resources; the system decides the
            # cloud/instance type. The below are equivalent:
            sky.Resources(accelerators='V100')
            sky.Resources(accelerators='V100:1')
            sky.Resources(accelerators={'V100': 1})
            sky.Resources(cpus='2+', memory='16+', accelerators='V100')

        Args:
          cloud: the cloud to use.
          instance_type: the instance type to use.
          cpus: the number of CPUs required for the task.
            If a str, must be a string of the form ``'2'`` or ``'2+'``, where
            the ``+`` indicates that the task requires at least 2 CPUs.
          memory: the amount of memory in GiB required. If a
            str, must be a string of the form ``'16'`` or ``'16+'``, where
            the ``+`` indicates that the task requires at least 16 GB of memory.
          accelerators: the accelerators required. If a str, must be
            a string of the form ``'V100'`` or ``'V100:2'``, where the ``:2``
            indicates that the task requires 2 V100 GPUs. If a dict, must be a
            dict of the form ``{'V100': 2}`` or ``{'tpu-v2-8': 1}``.
          accelerator_args: optional accelerator arguments to pass to the
            accelerator. For example, for Nvidia GPUs, this can be used to
            specify the GPU memory limit.
          use_spot: whether to use a spot instance.
          spot_recovery: whether to enable automatic recovery of the instance
            if it is preempted. Possible values are 'none', 'recreate', and
            'restart'. Defaults to 'recreate' if use_spot is True, and None
            otherwise.
          region: the region to use.
          zone: the zone to use.
          image_id: the image ID to use. If a str, must be a string of the
            form ``'ubuntu-2004'`` or ``'ubuntu-2004:20220301'``, where the
            ``:20220301`` indicates the date of the image. If a dict, must be
            a dict of the form ``{'us-east1': 'ubuntu-2004'}`` or
            ``{'us-east1': 'ubuntu-2004:20220301'}``. If None, the default
            image for the region will be used.
          disk_size: the size of the disk in GB. If None, the default disk size
            for the region will be used.
          disk_tier: the tier of the disk. Possible values are 'high', 'medium',
            and 'low'. Defaults to 'medium' if disk_size is not None, and None
            otherwise.
          ports: the ports to expose. If a str, must be a string of the form
            ``'8080'`` or ``'8080:8081'``, where the ``:8081`` indicates that
            the task will expose the port 8081 on the host. If a list, must be
            a list of strings of the same form. If None, no ports will be
            exposed.
          _docker_login_config: optional Docker login configuration.
          _is_image_managed: whether the image is managed by Sky.

        Raises:
          ValueError: if the resources are invalid.

        """
        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is None:
            raise ValueError('cloud is required')
        if instance_type is None:
            raise ValueError('instance_type is required')
        if cpus is None:
            raise ValueError('cpus is required')
        if memory is None:
            raise ValueError('memory is required')
        if accelerators is None:
            raise ValueError('accelerators is required')
        if accelerator_args is None:
            raise ValueError('accelerator_args is required')
        if use_spot is None:
            raise ValueError('use_spot is required')
        if spot_recovery is None:
            raise ValueError('spot_recovery is required')
        if region is None:
            raise ValueError('region is required')
        if zone is None:
            raise ValueError('zone is required')
        if image_id is None:
            raise ValueError('image_id is required')
        if disk_size is None:
            raise ValueError('disk_size is required')
        if disk_tier is None:
            raise ValueError('disk_tier is required')
        if ports is None:
            raise ValueError('ports is required')
        if _docker_login_config is None:
            raise ValueError('_docker_login_config is required')
        if _is_image_managed is None:
            raise ValueError('_is_image_managed is required')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance(disk_size, int):
            raise ValueError(f'Invalid disk_size: {disk_size}')
        if disk_tier is not None and not isinstance(disk_tier,
                                                    Literal['high', 'medium',
                                                            'low']):
            raise ValueError(f'Invalid disk_tier: {disk_tier}')
        if ports is not None and not isinstance(ports,
                                                (int, str, list, tuple)):
            raise ValueError(f'Invalid ports: {ports}')
        if _docker_login_config is not None and not isinstance(
                _docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}')
        if _is_image_managed is not None and not isinstance(
                _is_image_managed, bool):
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}')

        # Validate the arguments.
        if cloud is not None and not isinstance(cloud, clouds.Cloud):
            raise ValueError(f'Invalid cloud: {cloud}')
        if instance_type is not None and not isinstance(instance_type, str):
            raise ValueError(f'Invalid instance_type: {instance_type}')
        if cpus is not None and not isinstance(cpus, (int, float, str)):
            raise ValueError(f'Invalid cpus: {cpus}')
        if memory is not None and not isinstance(memory, (int, float, str)):
            raise ValueError(f'Invalid memory: {memory}')
        if accelerators is not None and not isinstance(accelerators,
                                                       (str, dict)):
            raise ValueError(f'Invalid accelerators: {accelerators}')
        if accelerator_args is not None and not isinstance(accelerator_args,
                                                           dict):
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}')
        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(f'Invalid use_spot: {use_spot}')
        if spot_recovery is not None and not isinstance(spot_recovery,
                                                        str):
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}')
        if region is not None and not isinstance(region, str):
            raise ValueError(f'Invalid region: {region}')
        if zone is not None and not isinstance(zone, str):
            raise ValueError(f'Invalid zone: {zone}')
        if image_id is not None and not isinstance(image_id,
                                                   (str, dict)):
            raise ValueError(f'Invalid image_id: {image_id}')
        if disk_size is not None and not isinstance