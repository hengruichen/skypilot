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
          accelerator_args: the accelerator-specific arguments required.
            If a dict, must be a dict of the form
            ``{'V100': {'gpu_memory_mib': '16000'}}``.
          use_spot: whether to use a spot instance.
          spot_recovery: the strategy for recovering from preempted instances.
            Must be one of ``'requeue'``, ``'fail'``, or ``'retry'``.
          region: the region to use.
          zone: the zone to use.
          image_id: the image ID to use.
            If a str, must be a string of the form
            ``'ubuntu-2004-lts-v20220907-0'``.
            If a dict, must be a dict of the form
            ``{'us-west1': 'ubuntu-2004-lts-v20220907-0'}``.
          disk_size: the size of the disk in GB.
          disk_tier: the tier of the disk. Must be one of
            ``'high'``, ``'medium'``, or ``'low'``.
          ports: the ports to expose.
            If an int, must be a port number.
            If a str, must be a string of the form ``'22'`` or ``'22-23'``,
            where the ``-`` indicates that the task requires ports 22 and 23.
            If a list, must be a list of port numbers.
            If a tuple, must be a tuple of port numbers.
          _docker_login_config: the docker login configuration.
            If a dict, must be a dict of the form
            ``{'dockerhub': {'username': 'user', 'password': 'pass'}}``.
          _is_image_managed: whether the image is managed by SkyPilot.
            If True, the image is managed by SkyPilot.
            If False, the image is not managed by SkyPilot.
            If None, the image is not managed by SkyPilot.

        Raises:
          ValueError: if the resources are invalid.
        """
        self._version = self._VERSION

        if not isinstance(cloud, clouds.Cloud):
            raise ValueError(
                f'Invalid cloud: {cloud}. Must be a Cloud instance.')

        if not isinstance(instance_type, str):
            raise ValueError(
                f'Invalid instance_type: {instance_type}. '
                'Must be a string.')

        if not isinstance(cpus, (int, float, str, type(None))):
            raise ValueError(
                f'Invalid cpus: {cpus}. Must be an int, float, or str.')

        if not isinstance(memory, (int, float, str, type(None))):
            raise ValueError(
                f'Invalid memory: {memory}. Must be an int, float, or str.')

        if not isinstance(accelerators, (str, dict, type(None))):
            raise ValueError(
                f'Invalid accelerators: {accelerators}. '
                'Must be a string, dict, or None.')

        if accelerator_args is not None and not isinstance(
                accelerator_args, dict):
            raise ValueError(
                f'Invalid accelerator_args: {accelerator_args}. '
                'Must be a dict.')

        if use_spot is not None and not isinstance(use_spot, bool):
            raise ValueError(
                f'Invalid use_spot: {use_spot}. Must be a bool.')

        if spot_recovery is not None and not isinstance(
                spot_recovery, str):
            raise ValueError(
                f'Invalid spot_recovery: {spot_recovery}. '
                'Must be a string.')

        if region is not None and not isinstance(region, str):
            raise ValueError(
                f'Invalid region: {region}. Must be a string.')

        if zone is not None and not isinstance(zone, str):
            raise ValueError(
                f'Invalid zone: {zone}. Must be a string.')

        if not isinstance(image_id, (str, dict, type(None))):
            raise ValueError(
                f'Invalid image_id: {image_id}. '
                'Must be a string or dict.')

        if not isinstance(disk_size, (int, type(None))):
            raise ValueError(
                f'Invalid disk_size: {disk_size}. Must be an int.')

        if not isinstance(disk_tier, (str, type(None))):
            raise ValueError(
                f'Invalid disk_tier: {disk_tier}. '
                'Must be a string.')

        if not isinstance(ports, (int, str, list, tuple, type(None))):
            raise ValueError(
                f'Invalid ports: {ports}. '
                'Must be an int, str, list, or tuple.')

        if not isinstance(_docker_login_config, (dict, type(None))):
            raise ValueError(
                f'Invalid _docker_login_config: {_docker_login_config}. '
                'Must be a dict.')

        if not isinstance(_is_image_managed, (bool, type(None))):
            raise ValueError(
                f'Invalid _is_image_managed: {_is_image_managed}. '
                'Must be a bool.')

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

        self._validate()

    @property
    def cloud(self) -> clouds.Cloud:
        """Returns the cloud."""
        return self._cloud

    @property
    def instance_type(self) -> str:
        """Returns the instance type."""
        return self._instance_type

    @property
    def cpus(self) -> Optional[Union[int, float, str]]:
        """Returns the number of CPUs required for the task."""
        return self._cpus

    @property
    def memory(self) -> Optional[Union[int, float, str]]:
        """Returns the amount of memory in GiB required."""
        return self._memory

    @property
    def accelerators(self) -> Optional[Union[str, Dict[str, int]]]:
        """Returns the accelerators required."""
        return self._accelerators

    @property
    def accelerator_args(self) -> Optional[Dict[str, str]]:
        """Returns the accelerator-specific arguments required."""
        return self._accelerator_args

    @property
    def use_spot(self) -> Optional[bool]:
        """Returns whether to use a spot instance."""
        return self._use_spot

    @property
    def spot_recovery(self) -> Optional[str]:
        """Returns the strategy for recovering from preempted instances."""
        return self._spot_recovery

    @property
    def region(self) -> Optional[str]:
        """Returns the region to use."""
        return self._region

    @property
    def zone(self) -> Optional[str]:
        """Returns the zone to use."""
        return self._zone

    @property
    def image_id(self) -> Optional[Union[str, Dict[str, str]]]:
        """Returns the image ID to use."""
        return self._image_id

    @property
    def disk_size(self) -> Optional[int]:
        """Returns the size of the disk in GB."""
        return self._disk_size

    @property
    def disk_tier(self) -> Optional[Literal['high', 'medium', 'low']]:
        """Returns the tier of the disk."""
        return self._disk_tier

    @property
    def ports(self) -> Optional[Union[int, str, List[str], Tuple[str]]]:
        """Returns the ports to expose."""
        return self._ports

    @property
    def _docker_login_config(self) -> Optional[docker_utils.DockerLoginConfig]:
        """Returns the docker login configuration."""
        return self._docker_login_config

    @property
    def _is_image_managed(self) -> Optional[bool]:
        """Returns whether the image is managed by SkyPilot."""
        return self._is_image_managed

    def _validate(self):
        """Validates the resources."""
        if self._instance_type is not None:
            if not self._cloud.is_valid_instance_type(
                    self._instance_type):
                raise ValueError(
                    f'Invalid instance_type: {self._instance_type}. '
                    'Must be a valid instance type for the cloud.')

        if self._cpus is not None:
            if not isinstance(self._cpus, str):
                raise ValueError(
                    f'Invalid cpus: {self._cpus}. '
                    'Must be a string of the form ``"2"`` or ``"2+"``, '
                    'where the ``+`` indicates that the task requires '
                    'at least 2 CPUs.')

        if self._memory is not None:
            if not isinstance(self._memory, str):
                raise ValueError(
                    f'Invalid memory: {self._memory}. '
                    'Must be a string of the form ``"16"`` or ``"16+"``, '
                    'where the ``+`` indicates that the task requires '
                    'at least 16 GB of memory.')

        if self._accelerators is not None:
            if isinstance(self._accelerators, str):
                self._accelerators = {
                    accelerator_registry.canonicalize_accelerator_name(
                        self._accelerators): 1
                }
            elif not isinstance(self._accelerators, dict):
                raise ValueError(
                    f'Invalid accelerators: {self._accelerators}. '
                    'Must be a string or dict.')

        if self._accelerator_args is not None:
            if not isinstance(self._accelerator_args, dict):
                raise ValueError(
                    f'Invalid accelerator_args: {self._accelerator_args}. '
                    'Must be a dict.')

        if self._use_spot is not None:
            if not isinstance(self._use_spot, bool):
                raise ValueError(
                    f'Invalid use_spot: {self._use_spot}. '
                    'Must be a bool.')

        if self._spot_recovery is not None:
            if self._spot_recovery not in ['requeue', 'fail', 'retry']:
                raise ValueError(
                    f'Invalid spot_recovery: {self._spot_recovery}. '
                    'Must be one of ``"requeue"``, ``"fail"``, or ``"retry"``.')

        if self._region is not None:
            if not isinstance(self._region, str):
                raise ValueError(
                    f'Invalid region: {self._region}. '
                    'Must be a string.')

        if self._zone is not None:
            if not isinstance(self._zone, str):
                raise ValueError(
                    f'Invalid zone: {self._zone}. '
                    'Must be a string.')

        if self._image_id is not None:
            if isinstance(self._image_id, str):
                self._image_id = {self._region: self._image_id}
            elif not isinstance(self._image_id, dict):
                raise ValueError(
                    f'Invalid image_id: {self._image_id}. '
                    'Must be a string or dict.')

        if self._disk_size is not None:
            if not isinstance(self._disk_size, int):
                raise ValueError(
                    f'Invalid disk_size: {self._disk_size}. '
                    'Must be an int.')

        if self._disk_tier is not None:
            if self._disk_tier not in ['high', 'medium', 'low']:
                raise ValueError(
                    f'Invalid disk_tier: {self._disk_tier}. '
                    'Must be one of ``"high"``, ``"medium"``, or ``"low"``.')

        if self._ports is not None:
            if isinstance(self._ports, int):
                if self._ports <= 0:
                    raise ValueError(
                        f'Invalid ports: {self._ports}. '
                        'Must be a positive integer.')
            elif isinstance(self._ports, str):
                if '-' in self._ports:
                    if not self._ports.startswith('-'):
                        raise ValueError(
                            f'Invalid ports: {self._ports}. '
                            'Must be a string of the form ``"22"`` or '
                            '``"22-23"``, where the ``-`` indicates that '
                            'the task requires ports 22 and 23.')
                    if not self._ports.endswith('-'):
                        raise ValueError(
                            f'Invalid ports: {self._ports}. '
                            'Must be a string of the form ``"22"`` or '
                            '``"22-23"``, where the ``-`` indicates that '
                            'the task requires ports 22 and 23.')
                    if not self._ports.count('-') == 1:
                        raise ValueError(
                            f'Invalid ports: {self._ports}. '
                            'Must be a string of the form ``"22"`` or '
                            '``"22-23"``, where the ``-`` indicates that '
                            'the task requires ports 22 and 23.')
                    if not self._ports.replace('-', '').isdigit():
                        raise ValueError(
                            f'Invalid ports: {self._ports}. '
                            'Must be a string of the form ``"22"`` or '
                            '``"22-23"``, where the ``-`` indicates that '
                            'the task requires ports 22 and 23.')
                    if not self._ports.replace('-', '').isdigit():
                        raise ValueError(
                            f'Invalid ports: {self._ports}. '
                            'Must be a string of the form ``"22"`` or '
                            '``"22-23"``, where the ``-`` indicates that '
                            'the task requires ports 22 and 23.')
                    if not self._ports.replace('-', '').isdigit():
                        raise ValueError(
                            f'Invalid ports: {self._ports}. '
                            'Must be a string of the form ``"22"`` or '
                            '``"22-23"``, where the ``-`` indicates that '
                            'the task requires ports 22 and 23.')
                    if not self._ports.replace('-', '').isdigit():
                        raise ValueError(
                            f'Invalid ports: {self._ports}. '
                            'Must be a string of the form ``"22"`` or '
                            '``"22-