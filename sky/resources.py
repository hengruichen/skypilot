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
          accelerator_args: the accelerator arguments required. If a dict, must be
            a dict of the form ``{'V100': {'gpu_memory_utilization': 0.9}}``.
          use_spot: whether to use a spot instance.
          spot_recovery: the strategy to use when the spot instance fails.
          region: the region to use.
          zone: the zone to use.
          image_id: the image ID to use.
          disk_size: the size of the disk in GB.
          disk_tier: the tier of the disk.
          ports: the ports to use.
          _docker_login_config: the docker login config.
          _is_image_managed: whether the image is managed.

        Returns:
          A Resources object.

        Raises:
          ValueError: If the resources are invalid.

        """
        # Validate all the fields.
        if cloud is not None:
            if not isinstance(cloud, clouds.Cloud):
                raise ValueError(
                    f'Invalid cloud: {cloud}. Must be a Cloud object.')
        if instance_type is not None:
            if not isinstance(instance_type, str):
                raise ValueError(
                    f'Invalid instance_type: {instance_type}. Must be a '
                    'string.')
        if cpus is not None:
            if not isinstance(cpus, (int, float, str)):
                raise ValueError(
                    f'Invalid cpus: {cpus}. Must be an int, float, or str.')
        if memory is not None:
            if not isinstance(memory, (int, float, str)):
                raise ValueError(
                    f'Invalid memory: {memory}. Must be an int, float, or str.')
        if accelerators is not None:
            if not isinstance(accelerators, (str, dict)):
                raise ValueError(
                    f'Invalid accelerators: {accelerators}. Must be a str or '
                    'a dict.')
        if accelerator_args is not None:
            if not isinstance(accelerator_args, dict):
                raise ValueError(
                    f'Invalid accelerator_args: {accelerator_args}. Must be a '
                    'dict.')
        if use_spot is not None:
            if not isinstance(use_spot, bool):
                raise ValueError(
                    f'Invalid use_spot: {use_spot}. Must be a bool.')
        if spot_recovery is not None:
            if not isinstance(spot_recovery, str):
                raise ValueError(
                    f'Invalid spot_recovery: {spot_recovery}. Must be a str.')
        if region is not None:
            if not isinstance(region, str):
                raise ValueError(
                    f'Invalid region: {region}. Must be a string.')
        if zone is not None:
            if not isinstance(zone, str):
                raise ValueError(
                    f'Invalid zone: {zone}. Must be a string.')
        if image_id is not None:
            if not isinstance(image_id, (str, dict)):
                raise ValueError(
                    f'Invalid image_id: {image_id}. Must be a string or a dict.')
        if disk_size is not None:
            if not isinstance(disk_size, int):
                raise ValueError(
                    f'Invalid disk_size: {disk_size}. Must be an int.')
        if disk_tier is not None:
            if not isinstance(disk_tier, str):
                raise ValueError(
                    f'Invalid disk_tier: {disk_tier}. Must be a string.')
        if ports is not None:
            if not isinstance(ports, (int, str, list, tuple)):
                raise ValueError(
                    f'Invalid ports: {ports}. Must be an int, str, list, or '
                    'tuple.')
        if _docker_login_config is not None:
            if not isinstance(_docker_login_config, docker_utils.DockerLoginConfig):
                raise ValueError(
                    f'Invalid _docker_login_config: {_docker_login_config}. '
                    'Must be a DockerLoginConfig object.')
        if _is_image_managed is not None:
            if not isinstance(_is_image_managed, bool):
                raise ValueError(
                    f'Invalid _is_image_managed: {_is_image_managed}. '
                    'Must be a bool.')

        # Validate the fields.
        if cloud is None:
            raise ValueError('cloud is required.')
        if instance_type is None:
            raise ValueError('instance_type is required.')
        if cpus is None:
            raise ValueError('cpus is required.')
        if memory is None:
            raise ValueError('memory is required.')
        if accelerators is None:
            raise ValueError('accelerators is required.')
        if accelerator_args is None:
            raise ValueError('accelerator_args is required.')
        if use_spot is None:
            raise ValueError('use_spot is required.')
        if spot_recovery is None:
            raise ValueError('spot_recovery is required.')
        if region is None:
            raise ValueError('region is required.')
        if zone is None:
            raise ValueError('zone is required.')
        if image_id is None:
            raise ValueError('image_id is required.')
        if disk_size is None:
            raise ValueError('disk_size is required.')
        if disk_tier is None:
            raise ValueError('disk_tier is required.')
        if ports is None:
            raise ValueError('ports is required.')
        if _docker_login_config is None:
            raise ValueError('_docker_login_config is required.')
        if _is_image_managed is None:
            raise ValueError('_is_image_managed is required.')

        # Validate the values.
        if cpus <= 0:
            raise ValueError(f'Invalid cpus: {cpus}. Must be > 0.')
        if memory <= 0:
            raise ValueError(f'Invalid memory: {memory}. Must be > 0.')
        if accelerators <= 0:
            raise ValueError(f'Invalid accelerators: {accelerators}. Must be > 0.')
        if accelerator_args <= 0:
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}. '
                             'Must be > 0.')
        if use_spot < 0:
            raise ValueError(f'Invalid use_spot: {use_spot}. Must be >= 0.')
        if spot_recovery < 0:
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}. Must be '
                             '>= 0.')
        if region < 0:
            raise ValueError(f'Invalid region: {region}. Must be >= 0.')
        if zone < 0:
            raise ValueError(f'Invalid zone: {zone}. Must be >= 0.')
        if image_id < 0:
            raise ValueError(f'Invalid image_id: {image_id}. Must be >= 0.')
        if disk_size < 0:
            raise ValueError(f'Invalid disk_size: {disk_size}. Must be >= 0.')
        if disk_tier < 0:
            raise ValueError(f'Invalid disk_tier: {disk_tier}. Must be >= 0.')
        if ports < 0:
            raise ValueError(f'Invalid ports: {ports}. Must be >= 0.')
        if _docker_login_config < 0:
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}. '
                             'Must be >= 0.')
        if _is_image_managed < 0:
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}. '
                             'Must be >= 0.')

        # Validate the types.
        if not isinstance(cloud, clouds.Cloud):
            raise ValueError(
                f'Invalid cloud: {cloud}. Must be a Cloud object.')
        if not isinstance(instance_type, str):
            raise ValueError(
                f'Invalid instance_type: {instance_type}. Must be a '
                'string.')
        if not isinstance(cpus, (int, float, str)):
            raise ValueError(
                f'Invalid cpus: {cpus}. Must be an int, float, or str.')
        if not isinstance(memory, (int, float, str)):
            raise ValueError(
                f'Invalid memory: {memory}. Must be an int, float, or str.')
        if not isinstance(accelerators, (str, dict)):
            raise ValueError(
                f'Invalid accelerators: {accelerators}. Must be a str or '
                'a dict.')
        if not isinstance(accelerator_args, dict):
            raise ValueError(
                f'Invalid accelerator_args: {accelerator_args}. Must be a '
                'dict.')
        if not isinstance(use_spot, bool):
            raise ValueError(
                f'Invalid use_spot: {use_spot}. Must be a bool.')
        if not isinstance(spot_recovery, str):
            raise ValueError(
                f'Invalid spot_recovery: {spot_recovery}. Must be a str.')
        if not isinstance(region, str):
            raise ValueError(
                f'Invalid region: {region}. Must be a string.')
        if not isinstance(zone, str):
            raise ValueError(
                f'Invalid zone: {zone}. Must be a string.')
        if not isinstance(image_id, (str, dict)):
            raise ValueError(
                f'Invalid image_id: {image_id}. Must be a string or a dict.')
        if not isinstance(disk_size, int):
            raise ValueError(
                f'Invalid disk_size: {disk_size}. Must be an int.')
        if not isinstance(disk_tier, str):
            raise ValueError(
                f'Invalid disk_tier: {disk_tier}. Must be a string.')
        if not isinstance(ports, (int, str, list, tuple)):
            raise ValueError(
                f'Invalid ports: {ports}. Must be an int, str, list, or '
                'tuple.')
        if not isinstance(_docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(
                f'Invalid _docker_login_config: {_docker_login_config}. '
                'Must be a DockerLoginConfig object.')
        if not isinstance(_is_image_managed, bool):
            raise ValueError(
                f'Invalid _is_image_managed: {_is_image_managed}. '
                'Must be a bool.')

        # Validate the values.
        if cpus <= 0:
            raise ValueError(f'Invalid cpus: {cpus}. Must be > 0.')
        if memory <= 0:
            raise ValueError(f'Invalid memory: {memory}. Must be > 0.')
        if accelerators <= 0:
            raise ValueError(f'Invalid accelerators: {accelerators}. Must be > 0.')
        if accelerator_args <= 0:
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}. '
                             'Must be > 0.')
        if use_spot < 0:
            raise ValueError(f'Invalid use_spot: {use_spot}. Must be >= 0.')
        if spot_recovery < 0:
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}. Must be '
                             '>= 0.')
        if region < 0:
            raise ValueError(f'Invalid region: {region}. Must be >= 0.')
        if zone < 0:
            raise ValueError(f'Invalid zone: {zone}. Must be >= 0.')
        if image_id < 0:
            raise ValueError(f'Invalid image_id: {image_id}. Must be >= 0.')
        if disk_size < 0:
            raise ValueError(f'Invalid disk_size: {disk_size}. Must be >= 0.')
        if disk_tier < 0:
            raise ValueError(f'Invalid disk_tier: {disk_tier}. Must be >= 0.')
        if ports < 0:
            raise ValueError(f'Invalid ports: {ports}. Must be >= 0.')
        if _docker_login_config < 0:
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}. '
                             'Must be >= 0.')
        if _is_image_managed < 0:
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}. '
                             'Must be >= 0.')

        # Validate the types.
        if not isinstance(cloud, clouds.Cloud):
            raise ValueError(
                f'Invalid cloud: {cloud}. Must be a Cloud object.')
        if not isinstance(instance_type, str):
            raise ValueError(
                f'Invalid instance_type: {instance_type}. Must be a '
                'string.')
        if not isinstance(cpus, (int, float, str)):
            raise ValueError(
                f'Invalid cpus: {cpus}. Must be an int, float, or str.')
        if not isinstance(memory, (int, float, str)):
            raise ValueError(
                f'Invalid memory: {memory}. Must be an int, float, or str.')
        if not isinstance(accelerators, (str, dict)):
            raise ValueError(
                f'Invalid accelerators: {accelerators}. Must be a str or '
                'a dict.')
        if not isinstance(accelerator_args, dict):
            raise ValueError(
                f'Invalid accelerator_args: {accelerator_args}. Must be a '
                'dict.')
        if not isinstance(use_spot, bool):
            raise ValueError(
                f'Invalid use_spot: {use_spot}. Must be a bool.')
        if not isinstance(spot_recovery, str):
            raise ValueError(
                f'Invalid spot_recovery: {spot_recovery}. Must be a str.')
        if not isinstance(region, str):
            raise ValueError(
                f'Invalid region: {region}. Must be a string.')
        if not isinstance(zone, str):
            raise ValueError(
                f'Invalid zone: {zone}. Must be a string.')
        if not isinstance(image_id, (str, dict)):
            raise ValueError(
                f'Invalid image_id: {image_id}. Must be a string or a dict.')
        if not isinstance(disk_size, int):
            raise ValueError(
                f'Invalid disk_size: {disk_size}. Must be an int.')
        if not isinstance(disk_tier, str):
            raise ValueError(
                f'Invalid disk_tier: {disk_tier}. Must be a string.')
        if not isinstance(ports, (int, str, list, tuple)):
            raise ValueError(
                f'Invalid ports: {ports}. Must be an int, str, list, or '
                'tuple.')
        if not isinstance(_docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(
                f'Invalid _docker_login_config: {_docker_login_config}. '
                'Must be a DockerLoginConfig object.')
        if not isinstance(_is_image_managed, bool):
            raise ValueError(
                f'Invalid _is_image_managed: {_is_image_managed}. '
                'Must be a bool.')

        # Validate the values.
        if cpus <= 0:
            raise ValueError(f'Invalid cpus: {cpus}. Must be > 0.')
        if memory <= 0:
            raise ValueError(f'Invalid memory: {memory}. Must be > 0.')
        if accelerators <= 0:
            raise ValueError(f'Invalid accelerators: {accelerators}. Must be > 0.')
        if accelerator_args <= 0:
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}. '
                             'Must be > 0.')
        if use_spot < 0:
            raise ValueError(f'Invalid use_spot: {use_spot}. Must be >= 0.')
        if spot_recovery < 0:
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}. Must be '
                             '>= 0.')
        if region < 0:
            raise ValueError(f'Invalid region: {region}. Must be >= 0.')
        if zone < 0:
            raise ValueError(f'Invalid zone: {zone}. Must be >= 0.')
        if image_id < 0:
            raise ValueError(f'Invalid image_id: {image_id}. Must be >= 0.')
        if disk_size < 0:
            raise ValueError(f'Invalid disk_size: {disk_size}. Must be >= 0.')
        if disk_tier < 0:
            raise ValueError(f'Invalid disk_tier: {disk_tier}. Must be >= 0.')
        if ports < 0:
            raise ValueError(f'Invalid ports: {ports}. Must be >= 0.')
        if _docker_login_config < 0:
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}. '
                             'Must be >= 0.')
        if _is_image_managed < 0:
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}. '
                             'Must be >= 0.')

        # Validate the types.
        if not isinstance(cloud, clouds.Cloud):
            raise ValueError(
                f'Invalid cloud: {cloud}. Must be a Cloud object.')
        if not isinstance(instance_type, str):
            raise ValueError(
                f'Invalid instance_type: {instance_type}. Must be a '
                'string.')
        if not isinstance(cpus, (int, float, str)):
            raise ValueError(
                f'Invalid cpus: {cpus}. Must be an int, float, or str.')
        if not isinstance(memory, (int, float, str)):
            raise ValueError(
                f'Invalid memory: {memory}. Must be an int, float, or str.')
        if not isinstance(accelerators, (str, dict)):
            raise ValueError(
                f'Invalid accelerators: {accelerators}. Must be a str or '
                'a dict.')
        if not isinstance(accelerator_args, dict):
            raise ValueError(
                f'Invalid accelerator_args: {accelerator_args}. Must be a '
                'dict.')
        if not isinstance(use_spot, bool):
            raise ValueError(
                f'Invalid use_spot: {use_spot}. Must be a bool.')
        if not isinstance(spot_recovery, str):
            raise ValueError(
                f'Invalid spot_recovery: {spot_recovery}. Must be a str.')
        if not isinstance(region, str):
            raise ValueError(
                f'Invalid region: {region}. Must be a string.')
        if not isinstance(zone, str):
            raise ValueError(
                f'Invalid zone: {zone}. Must be a string.')
        if not isinstance(image_id, (str, dict)):
            raise ValueError(
                f'Invalid image_id: {image_id}. Must be a string or a dict.')
        if not isinstance(disk_size, int):
            raise ValueError(
                f'Invalid disk_size: {disk_size}. Must be an int.')
        if not isinstance(disk_tier, str):
            raise ValueError(
                f'Invalid disk_tier: {disk_tier}. Must be a string.')
        if not isinstance(ports, (int, str, list, tuple)):
            raise ValueError(
                f'Invalid ports: {ports}. Must be an int, str, list, or '
                'tuple.')
        if not isinstance(_docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(
                f'Invalid _docker_login_config: {_docker_login_config}. '
                'Must be a DockerLoginConfig object.')
        if not isinstance(_is_image_managed, bool):
            raise ValueError(
                f'Invalid _is_image_managed: {_is_image_managed}. '
                'Must be a bool.')

        # Validate the values.
        if cpus <= 0:
            raise ValueError(f'Invalid cpus: {cpus}. Must be > 0.')
        if memory <= 0:
            raise ValueError(f'Invalid memory: {memory}. Must be > 0.')
        if accelerators <= 0:
            raise ValueError(f'Invalid accelerators: {accelerators}. Must be > 0.')
        if accelerator_args <= 0:
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}. '
                             'Must be > 0.')
        if use_spot < 0:
            raise ValueError(f'Invalid use_spot: {use_spot}. Must be >= 0.')
        if spot_recovery < 0:
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}. Must be '
                             '>= 0.')
        if region < 0:
            raise ValueError(f'Invalid region: {region}. Must be >= 0.')
        if zone < 0:
            raise ValueError(f'Invalid zone: {zone}. Must be >= 0.')
        if image_id < 0:
            raise ValueError(f'Invalid image_id: {image_id}. Must be >= 0.')
        if disk_size < 0:
            raise ValueError(f'Invalid disk_size: {disk_size}. Must be >= 0.')
        if disk_tier < 0:
            raise ValueError(f'Invalid disk_tier: {disk_tier}. Must be >= 0.')
        if ports < 0:
            raise ValueError(f'Invalid ports: {ports}. Must be >= 0.')
        if _docker_login_config < 0:
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}. '
                             'Must be >= 0.')
        if _is_image_managed < 0:
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}. '
                             'Must be >= 0.')

        # Validate the types.
        if not isinstance(cloud, clouds.Cloud):
            raise ValueError(
                f'Invalid cloud: {cloud}. Must be a Cloud object.')
        if not isinstance(instance_type, str):
            raise ValueError(
                f'Invalid instance_type: {instance_type}. Must be a '
                'string.')
        if not isinstance(cpus, (int, float, str)):
            raise ValueError(
                f'Invalid cpus: {cpus}. Must be an int, float, or str.')
        if not isinstance(memory, (int, float, str)):
            raise ValueError(
                f'Invalid memory: {memory}. Must be an int, float, or str.')
        if not isinstance(accelerators, (str, dict)):
            raise ValueError(
                f'Invalid accelerators: {accelerators}. Must be a str or '
                'a dict.')
        if not isinstance(accelerator_args, dict):
            raise ValueError(
                f'Invalid accelerator_args: {accelerator_args}. Must be a '
                'dict.')
        if not isinstance(use_spot, bool):
            raise ValueError(
                f'Invalid use_spot: {use_spot}. Must be a bool.')
        if not isinstance(spot_recovery, str):
            raise ValueError(
                f'Invalid spot_recovery: {spot_recovery}. Must be a str.')
        if not isinstance(region, str):
            raise ValueError(
                f'Invalid region: {region}. Must be a string.')
        if not isinstance(zone, str):
            raise ValueError(
                f'Invalid zone: {zone}. Must be a string.')
        if not isinstance(image_id, (str, dict)):
            raise ValueError(
                f'Invalid image_id: {image_id}. Must be a string or a dict.')
        if not isinstance(disk_size, int):
            raise ValueError(
                f'Invalid disk_size: {disk_size}. Must be an int.')
        if not isinstance(disk_tier, str):
            raise ValueError(
                f'Invalid disk_tier: {disk_tier}. Must be a string.')
        if not isinstance(ports, (int, str, list, tuple)):
            raise ValueError(
                f'Invalid ports: {ports}. Must be an int, str, list, or '
                'tuple.')
        if not isinstance(_docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(
                f'Invalid _docker_login_config: {_docker_login_config}. '
                'Must be a DockerLoginConfig object.')
        if not isinstance(_is_image_managed, bool):
            raise ValueError(
                f'Invalid _is_image_managed: {_is_image_managed}. '
                'Must be a bool.')

        # Validate the values.
        if cpus <= 0:
            raise ValueError(f'Invalid cpus: {cpus}. Must be > 0.')
        if memory <= 0:
            raise ValueError(f'Invalid memory: {memory}. Must be > 0.')
        if accelerators <= 0:
            raise ValueError(f'Invalid accelerators: {accelerators}. Must be > 0.')
        if accelerator_args <= 0:
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}. '
                             'Must be > 0.')
        if use_spot < 0:
            raise ValueError(f'Invalid use_spot: {use_spot}. Must be >= 0.')
        if spot_recovery < 0:
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}. Must be '
                             '>= 0.')
        if region < 0:
            raise ValueError(f'Invalid region: {region}. Must be >= 0.')
        if zone < 0:
            raise ValueError(f'Invalid zone: {zone}. Must be >= 0.')
        if image_id < 0:
            raise ValueError(f'Invalid image_id: {image_id}. Must be >= 0.')
        if disk_size < 0:
            raise ValueError(f'Invalid disk_size: {disk_size}. Must be >= 0.')
        if disk_tier < 0:
            raise ValueError(f'Invalid disk_tier: {disk_tier}. Must be >= 0.')
        if ports < 0:
            raise ValueError(f'Invalid ports: {ports}. Must be >= 0.')
        if _docker_login_config < 0:
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}. '
                             'Must be >= 0.')
        if _is_image_managed < 0:
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}. '
                             'Must be >= 0.')

        # Validate the types.
        if not isinstance(cloud, clouds.Cloud):
            raise ValueError(
                f'Invalid cloud: {cloud}. Must be a Cloud object.')
        if not isinstance(instance_type, str):
            raise ValueError(
                f'Invalid instance_type: {instance_type}. Must be a '
                'string.')
        if not isinstance(cpus, (int, float, str)):
            raise ValueError(
                f'Invalid cpus: {cpus}. Must be an int, float, or str.')
        if not isinstance(memory, (int, float, str)):
            raise ValueError(
                f'Invalid memory: {memory}. Must be an int, float, or str.')
        if not isinstance(accelerators, (str, dict)):
            raise ValueError(
                f'Invalid accelerators: {accelerators}. Must be a str or '
                'a dict.')
        if not isinstance(accelerator_args, dict):
            raise ValueError(
                f'Invalid accelerator_args: {accelerator_args}. Must be a '
                'dict.')
        if not isinstance(use_spot, bool):
            raise ValueError(
                f'Invalid use_spot: {use_spot}. Must be a bool.')
        if not isinstance(spot_recovery, str):
            raise ValueError(
                f'Invalid spot_recovery: {spot_recovery}. Must be a str.')
        if not isinstance(region, str):
            raise ValueError(
                f'Invalid region: {region}. Must be a string.')
        if not isinstance(zone, str):
            raise ValueError(
                f'Invalid zone: {zone}. Must be a string.')
        if not isinstance(image_id, (str, dict)):
            raise ValueError(
                f'Invalid image_id: {image_id}. Must be a string or a dict.')
        if not isinstance(disk_size, int):
            raise ValueError(
                f'Invalid disk_size: {disk_size}. Must be an int.')
        if not isinstance(disk_tier, str):
            raise ValueError(
                f'Invalid disk_tier: {disk_tier}. Must be a string.')
        if not isinstance(ports, (int, str, list, tuple)):
            raise ValueError(
                f'Invalid ports: {ports}. Must be an int, str, list, or '
                'tuple.')
        if not isinstance(_docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(
                f'Invalid _docker_login_config: {_docker_login_config}. '
                'Must be a DockerLoginConfig object.')
        if not isinstance(_is_image_managed, bool):
            raise ValueError(
                f'Invalid _is_image_managed: {_is_image_managed}. '
                'Must be a bool.')

        # Validate the values.
        if cpus <= 0:
            raise ValueError(f'Invalid cpus: {cpus}. Must be > 0.')
        if memory <= 0:
            raise ValueError(f'Invalid memory: {memory}. Must be > 0.')
        if accelerators <= 0:
            raise ValueError(f'Invalid accelerators: {accelerators}. Must be > 0.')
        if accelerator_args <= 0:
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}. '
                             'Must be > 0.')
        if use_spot < 0:
            raise ValueError(f'Invalid use_spot: {use_spot}. Must be >= 0.')
        if spot_recovery < 0:
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}. Must be '
                             '>= 0.')
        if region < 0:
            raise ValueError(f'Invalid region: {region}. Must be >= 0.')
        if zone < 0:
            raise ValueError(f'Invalid zone: {zone}. Must be >= 0.')
        if image_id < 0:
            raise ValueError(f'Invalid image_id: {image_id}. Must be >= 0.')
        if disk_size < 0:
            raise ValueError(f'Invalid disk_size: {disk_size}. Must be >= 0.')
        if disk_tier < 0:
            raise ValueError(f'Invalid disk_tier: {disk_tier}. Must be >= 0.')
        if ports < 0:
            raise ValueError(f'Invalid ports: {ports}. Must be >= 0.')
        if _docker_login_config < 0:
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}. '
                             'Must be >= 0.')
        if _is_image_managed < 0:
            raise ValueError(f'Invalid _is_image_managed: {_is_image_managed}. '
                             'Must be >= 0.')

        # Validate the types.
        if not isinstance(cloud, clouds.Cloud):
            raise ValueError(
                f'Invalid cloud: {cloud}. Must be a Cloud object.')
        if not isinstance(instance_type, str):
            raise ValueError(
                f'Invalid instance_type: {instance_type}. Must be a '
                'string.')
        if not isinstance(cpus, (int, float, str)):
            raise ValueError(
                f'Invalid cpus: {cpus}. Must be an int, float, or str.')
        if not isinstance(memory, (int, float, str)):
            raise ValueError(
                f'Invalid memory: {memory}. Must be an int, float, or str.')
        if not isinstance(accelerators, (str, dict)):
            raise ValueError(
                f'Invalid accelerators: {accelerators}. Must be a str or '
                'a dict.')
        if not isinstance(accelerator_args, dict):
            raise ValueError(
                f'Invalid accelerator_args: {accelerator_args}. Must be a '
                'dict.')
        if not isinstance(use_spot, bool):
            raise ValueError(
                f'Invalid use_spot: {use_spot}. Must be a bool.')
        if not isinstance(spot_recovery, str):
            raise ValueError(
                f'Invalid spot_recovery: {spot_recovery}. Must be a str.')
        if not isinstance(region, str):
            raise ValueError(
                f'Invalid region: {region}. Must be a string.')
        if not isinstance(zone, str):
            raise ValueError(
                f'Invalid zone: {zone}. Must be a string.')
        if not isinstance(image_id, (str, dict)):
            raise ValueError(
                f'Invalid image_id: {image_id}. Must be a string or a dict.')
        if not isinstance(disk_size, int):
            raise ValueError(
                f'Invalid disk_size: {disk_size}. Must be an int.')
        if not isinstance(disk_tier, str):
            raise ValueError(
                f'Invalid disk_tier: {disk_tier}. Must be a string.')
        if not isinstance(ports, (int, str, list, tuple)):
            raise ValueError(
                f'Invalid ports: {ports}. Must be an int, str, list, or '
                'tuple.')
        if not isinstance(_docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError(
                f'Invalid _docker_login_config: {_docker_login_config}. '
                'Must be a DockerLoginConfig object.')
        if not isinstance(_is_image_managed, bool):
            raise ValueError(
                f'Invalid _is_image_managed: {_is_image_managed}. '
                'Must be a bool.')

        # Validate the values.
        if cpus <= 0:
            raise ValueError(f'Invalid cpus: {cpus}. Must be > 0.')
        if memory <= 0:
            raise ValueError(f'Invalid memory: {memory}. Must be > 0.')
        if accelerators <= 0:
            raise ValueError(f'Invalid accelerators: {accelerators}. Must be > 0.')
        if accelerator_args <= 0:
            raise ValueError(f'Invalid accelerator_args: {accelerator_args}. '
                             'Must be > 0.')
        if use_spot < 0:
            raise ValueError(f'Invalid use_spot: {use_spot}. Must be >= 0.')
        if spot_recovery < 0:
            raise ValueError(f'Invalid spot_recovery: {spot_recovery}. Must be '
                             '>= 0.')
        if region < 0:
            raise ValueError(f'Invalid region: {region}. Must be >= 0.')
        if zone < 0:
            raise ValueError(f'Invalid zone: {zone}. Must be >= 0.')
        if image_id < 0:
            raise ValueError(f'Invalid image_id: {image_id}. Must be >= 0.')
        if disk_size < 0:
            raise ValueError(f'Invalid disk_size: {disk_size}. Must be >= 0.')
        if disk_tier < 0:
            raise ValueError(f'Invalid disk_tier: {disk_tier}. Must be >= 0.')
        if ports < 0:
            raise ValueError(f'Invalid ports: {ports}. Must be >= 0.')
        if _docker_login_config < 0:
            raise ValueError(f'Invalid _docker_login_config: {_docker_login_config}. '
                             'Must be >= 0.')
        if _is_image_managed < 0:
            raise ValueError(f'Invalid _is_image_managed