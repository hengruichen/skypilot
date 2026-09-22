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
          accelerator_args: the accelerator arguments.
          use_spot: whether to use a spot instance.
          spot_recovery: the type of instance to recover if the
            original instance is preempted.
          region: the region to use.
          zone: the zone to use.
          image_id: the image ID to use.
          disk_size: the size of the disk in GB.
          disk_tier: the tier of the disk.
          ports: the ports to use.
          # Internal use only.
          _docker_login_config: the docker login config.
          _is_image_managed: whether the image is managed by Sky.

        Returns:
          A new Resources object.
        """
        # Validate the inputs.
        if isinstance(accelerators, str):
            accelerators = {
                accelerator_registry.canonicalize_accelerator_name(acc):
                acc_count for acc, acc_count in
                resources_utils.parse_accelerator_str(accelerators).items()
            }
        elif isinstance(accelerators, dict):
            accelerators = {
                accelerator_registry.canonicalize_accelerator_name(acc):
                acc_count for acc, acc_count in accelerators.items()
            }

        if isinstance(image_id, str):
            image_id = {region: image_id for region in [region]}

        # Store the inputs.
        self._cloud = cloud
        self._instance_type = instance_type
        self._cpus = cpus
        self._memory = memory
        self._accelerators = accelerators
        self._accelerator_args = accelerator_args
        self._use_spot = use_spot
        self._spot_recovery = spot_recovery
        self._region = region
        self._zone = zone
        self._image_id = image_id
        self._disk_size = disk_size
        self._disk_tier = disk_tier
        self._ports = ports
        self._docker_login_config = _docker_login_config
        self._is_image_managed = _is_image_managed

        # Validate the inputs.
        if self._cloud is None:
            raise ValueError('Cloud is required.')
        if self._instance_type is None:
            raise ValueError('Instance type is required.')
        if self._cpus is None:
            raise ValueError('CPUs are required.')
        if self._memory is None:
            raise ValueError('Memory is required.')
        if self._accelerators is None:
            raise ValueError('Accelerators are required.')
        if self._use_spot is None:
            raise ValueError('Use spot is required.')
        if self._spot_recovery is None:
            raise ValueError('Spot recovery is required.')
        if self._region is None:
            raise ValueError('Region is required.')
        if self._zone is None:
            raise ValueError('Zone is required.')
        if self._image_id is None:
            raise ValueError('Image ID is required.')
        if self._disk_size is None:
            raise ValueError('Disk size is required.')
        if self._disk_tier is None:
            raise ValueError('Disk tier is required.')
        if self._ports is None:
            raise ValueError('Ports are required.')
        if self._docker_login_config is None:
            raise ValueError('Docker login config is required.')
        if self._is_image_managed is None:
            raise ValueError('Is image managed is required.')

        # Validate the inputs.
        if not isinstance(self._cloud, clouds.Cloud):
            raise ValueError('Cloud is not a valid cloud.')
        if not isinstance(self._instance_type, str):
            raise ValueError('Instance type is not a valid string.')
        if not isinstance(self._cpus, (int, float, str)):
            raise ValueError('CPUs is not a valid number.')
        if not isinstance(self._memory, (int, float, str)):
            raise ValueError('Memory is not a valid number.')
        if not isinstance(self._accelerators, dict):
            raise ValueError('Accelerators is not a valid dict.')
        if not isinstance(self._accelerator_args, dict):
            raise ValueError('Accelerator args is not a valid dict.')
        if not isinstance(self._use_spot, bool):
            raise ValueError('Use spot is not a valid bool.')
        if not isinstance(self._spot_recovery, str):
            raise ValueError('Spot recovery is not a valid string.')
        if not isinstance(self._region, str):
            raise ValueError('Region is not a valid string.')
        if not isinstance(self._zone, str):
            raise ValueError('Zone is not a valid string.')
        if not isinstance(self._image_id, dict):
            raise ValueError('Image ID is not a valid dict.')
        if not isinstance(self._disk_size, int):
            raise ValueError('Disk size is not a valid number.')
        if not isinstance(self._disk_tier, str):
            raise ValueError('Disk tier is not a valid string.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a valid bool.')

        # Validate the inputs.
        if self._cpus <= 0:
            raise ValueError('CPUs must be greater than 0.')
        if self._memory <= 0:
            raise ValueError('Memory must be greater than 0.')
        if self._disk_size <= 0:
            raise ValueError('Disk size must be greater than 0.')
        if self._disk_tier not in ['high', 'medium', 'low']:
            raise ValueError('Disk tier must be one of high, medium, or low.')
        if not isinstance(self._ports, (int, str, list, tuple)):
            raise ValueError('Ports is not a valid number, string, list, or '
                             'tuple.')
        if not isinstance(self._docker_login_config, docker_utils.DockerLoginConfig):
            raise ValueError('Docker login config is not a valid DockerLoginConfig.')
        if not isinstance(self._is_image_managed, bool):
            raise ValueError('Is image managed is not a