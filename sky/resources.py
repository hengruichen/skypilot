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
          accelerator_args: the accelerator arguments required.
          use_spot: whether to use a preemptible/virtual machine.
          spot_recovery: the action to take when the preemptible/virtual
            machine is preempted. Valid values are ``'recreate'``,
            ``'restart'``, and ``'fail'``.
          region: the region to use.
          zone: the zone to use.
          image_id: the image ID to use.
          disk_size: the size of the disk in GB.
          disk_tier: the tier of the disk. Valid values are ``'high'``,
            ``'medium'``, and ``'low'``.
          ports: the ports to expose.
          _docker_login_config: the docker login config.
          _is_image_managed: whether the image is managed by SkyPilot.
        """
        self._version = self._VERSION

        if isinstance(cloud, str):
            cloud = clouds.Cloud.from_string(cloud)

        if isinstance(instance_type, str):
            instance_type = clouds.InstanceType.from_string(instance_type)

        if isinstance(accelerators, str):
            accelerators = {
                accelerator_registry.canonicalize_accelerator_name(acc):
                acc_count for acc, acc_count in
                resources_utils.parse_accelerators(accelerators).items()
            }

        if isinstance(image_id, str):
            image_id = {region: image_id for region in common_utils.get_regions(
                cloud)}

        # TODO (zhwu): Add a validation to check that the resource fields are
        # consistent with the cloud's service catalog.
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

        # Validate the fields.
        self._validate()

    def _validate(self):
        """Validate the fields of the Resources object."""
        if self._cloud is None:
            raise ValueError('Cloud must be specified.')

        if self._instance_type is None:
            raise ValueError('Instance type must be specified.')

        if self._cpus is not None:
            if isinstance(self._cpus, str):
                if self._cpus.endswith('+'):
                    self._cpus = float(self._cpus[:-1])
                    self._cpus = self._cpus + 1
                else:
                    self._cpus = float(self._cpus)
            elif isinstance(self._cpus, int):
                self._cpus = float(self._cpus)
            else:
                raise ValueError('CPUs must be a string or an integer.')

        if self._memory is not None:
            if isinstance(self._memory, str):
                if self._memory.endswith('+'):
                    self._memory = float(self._memory[:-1])
                    self._memory = self._memory + 1
                else:
                    self._memory = float(self._memory)
            elif isinstance(self._memory, int):
                self._memory = float(self._memory)
            else:
                raise ValueError('Memory must be a string or an integer.')

        if self._accelerators is not None:
            if isinstance(self._accelerators, str):
                self._accelerators = {
                    accelerator_registry.canonicalize_accelerator_name(acc):
                    acc_count for acc, acc_count in
                    resources_utils.parse_accelerators(self._accelerators).items()
                }
            elif not isinstance(self._accelerators, dict):
                raise ValueError('Accelerators must be a string or a dict.')

        if self._accelerator_args is not None:
            if not isinstance(self._accelerator_args, dict):
                raise ValueError('Accelerator args must be a dict.')

        if self._use_spot is not None:
            if not isinstance(self._use_spot, bool):
                raise ValueError('Use_spot must be a boolean.')

        if self._spot_recovery is not None:
            if not isinstance(self._spot_recovery, str):
                raise ValueError('Spot_recovery must be a string.')

        if self._region is not None:
            if not isinstance(self._region, str):
                raise ValueError('Region must be a string.')

        if self._zone is not None:
            if not isinstance(self._zone, str):
                raise ValueError('Zone must be a string.')

        if self._image_id is not None:
            if not isinstance(self._image_id, dict):
                raise ValueError('Image_id must be a dict.')

        if self._disk_size is not None:
            if not isinstance(self._disk_size, int):
                raise ValueError('Disk_size must be an integer.')

        if self._disk_tier is not None:
            if not isinstance(self._disk_tier, str):
                raise ValueError('Disk_tier must be a string.')

        if self._ports is not None:
            if not isinstance(self._ports, (int, str, list, tuple)):
                raise ValueError('Ports must be an integer, a string, a list, '
                                 'or a tuple.')

        if self._docker_login_config is not None:
            if not isinstance(self._docker_login_config,
                              docker_utils.DockerLoginConfig):
                raise ValueError('Docker_login_config must be a DockerLoginConfig.')

        if self._is_image_managed is not None:
            if not isinstance(self._is_image_managed, bool):
                raise ValueError('Is_image_managed must be a boolean.')

    @classmethod
    def from_yaml_config(cls, config: Dict[str, Union[str, int]]) -> 'Resources':
        """Create a Resources object from a yaml-style config dict.

        Args:
          config: a yaml-style dict of config for this resource bundle.

        Returns:
          A Resources object.
        """
        resources_fields = {}

        def get_if_exists(key):
            return config.get(key, None)

        def get_if_exists_and_convert(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return converter(value)
            return value

        def get_if_exists_and_convert_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return [converter(item) for item in value]
            return value

        def get_if_exists_and_convert_dict(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): v for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_str_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_str_list(key, converter):
            value = get_if_exists(key)
            if value is not None:
                return {converter(k): [converter(item) for item in v.split(',')]
                        for k, v in value.items()}
            return value

        def get_if_exists_and_convert_dict_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_int_list(key, converter):
            value = get_if_exists(key)
