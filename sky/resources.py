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
            If a dict, must be a dict of the form
            ``{'V100': 'arg1=val1,arg2=val2'}``.
          use_spot: whether to use a spot instance. If None, the system
            decides whether to use a spot instance.
          spot_recovery: the strategy for recovering from a preempted
            or failed instance. If None, the system decides the strategy.
          region: the region to use. If None, the system decides the
            region.
          zone: the zone to use. If None, the system decides the zone.
          image_id: the image ID to use. If a str, must be a string of
            the form ``'image-id'`` or ``'image-id:region'``, where the
            ``:region`` indicates that the image is in the specified region.
            If a dict, must be a dict of the form
            ``{'region': 'image-id'}`` or
            ``{'region': 'image-id:region'}``.
          disk_size: the size of the disk in GB. If None, the system
            decides the disk size.
          disk_tier: the tier of the disk. If None, the system decides
            the disk tier.
          ports: the ports to use. If a str, must be a string of the
            form ``'port1,port2,...'``, where the ``,`` indicates that the
            task requires multiple ports. If a list, must be a list of the
            form ``['port1', 'port2', ...]``, where the ``,`` indicates that
            the task requires multiple ports. If a tuple, must be a tuple of
            the form ``(port1, port2, ...)``.
          _docker_login_config: the Docker login configuration. If None,
            the system decides the Docker login configuration.
          _is_image_managed: whether the image is managed by Sky. If None,
            the system decides whether the image is managed by Sky.
        """
        resources_fields = {}

        def add_if_not_none(key, value):
            if value is not None and value != 'None':
                resources_fields[key] = value

        if cloud is not None:
            add_if_not_none('cloud', cloud)
        if instance_type is not None:
            add_if_not_none('instance_type', instance_type)
        if cpus is not None:
            add_if_not_none('cpus', cpus)
        if memory is not None:
            add_if_not_none('memory', memory)
        if accelerators is not None:
            add_if_not_none('accelerators', accelerators)
        if accelerator_args is not None:
            add_if_not_none('accelerator_args', accelerator_args)
        if use_spot is not None:
            add_if_not_none('use_spot', use_spot)
        if spot_recovery is not None:
            add_if_not_none('spot_recovery', spot_recovery)
        if region is not None:
            add_if_not_none('region', region)
        if zone is not None:
            add_if_not_none('zone', zone)
        if image_id is not None:
            if isinstance(image_id, str):
                add_if_not_none('_image_id', {region: image_id})
            else:
                assert isinstance(image_id, dict)
                add_if_not_none('_image_id', image_id)
        if disk_size is not None:
            assert isinstance(disk_size, int)
            add_if_not_none('disk_size', disk_size)
        if disk_tier is not None:
            assert isinstance(disk_tier, str)
            assert disk_tier in ['high', 'medium', 'low']
            add_if_not_none('disk_tier', disk_tier)
        if ports is not None:
            assert isinstance(ports, (int, str, list, tuple))
            add_if_not_none('ports', ports)
        if _docker_login_config is not None:
            assert isinstance(_docker_login_config, docker_utils.DockerLoginConfig)
            add_if_not_none('_docker_login_config', _docker_login_config)
        if _is_image_managed is not None:
            assert isinstance(_is_image_managed, bool)
            add_if_not_none('_is_image_managed', _is_image_managed)

        assert not config, f'Invalid resource args: {config.keys()}'
        return Resources(**resources_fields)

    def to_yaml_config(self) -> Dict[str, Union[str, int]]:
        """Returns a yaml-style dict of config for this resource bundle."""
        config = {}

        def add_if_not_none(key, value):
            if value is not None and value != 'None':
                config[key] = value

        add_if_not_none('cloud', str(self.cloud))
        add_if_not_none('instance_type', self.instance_type)
        add_if_not_none('cpus', self.cpus)
        add_if_not_none('memory', self.memory)
        add_if_not_none('accelerators', self.accelerators)
        add_if_not_none('accelerator_args', self.accelerator_args)

        if self._use_spot_specified:
            add_if_not_none('use_spot', self.use_spot)
        config['spot_recovery'] = self.spot_recovery
        config['disk_size'] = self.disk_size
        add_if_not_none('region', self.region)
        add_if_not_none('zone', self.zone)
        add_if_not_none('image_id', self.image_id)
        add_if_not_none('disk_tier', self.disk_tier)
        add_if_not_none('ports', self.ports)
        add_if_not_none('_docker_login_config', self._docker_login_config)
        if self._is_image_managed is not None:
            config['_is_image_managed'] = self._is_image_managed
        return config

    def __setstate__(self, state):
        """Set state from pickled state, for backward compatibility."""
        self._version = self._VERSION

        # TODO (zhwu): Design our persistent state format with `__getstate__`,
        # so that to get rid of the version tracking.
        version = state.pop('_version', None)
        # Handle old version(s) here.
        if version is None:
            version = -1
        if version < 0:
            cloud = state.pop('cloud', None)
            state['_cloud'] = cloud

            instance_type = state.pop('instance_type', None)
            state['_instance_type'] = instance_type

            use_spot = state.pop('use_spot', False)
            state['_use_spot'] = use_spot

            accelerator_args = state.pop('accelerator_args', None)
            state['_accelerator_args'] = accelerator_args

            disk_size = state.pop('disk_size', _DEFAULT_DISK_SIZE_GB)
            state['_disk_size'] = disk_size

        if version < 2:
            self._region = None

        if version < 3:
            self._spot_recovery = None

        if version < 4:
            self._image_id = None

        if version < 5:
            self._zone = None

        if version < 6:
            accelerators = state.pop('_accelerators', None)
            if accelerators is not None:
                accelerators = {
                    accelerator_registry.canonicalize_accelerator_name(acc):
                    acc_count for acc, acc_count in accelerators.items()
                }
            state['_accelerators'] = accelerators

        if version < 7:
            self._cpus = None

        if version < 8:
            self._memory = None

        image_id = state.get('_image_id', None)
        if isinstance(image_id, str):
            state['_image_id'] = {state.get('_region', None): image_id}

        if version < 9:
            self._disk_tier = None

        if version < 10:
            self._is_image_managed = None

        if version < 11:
            self._ports = None

        if version < 12:
            self._docker_login_config = None

        if version < 13:
            original_ports = state.get('_ports', None)
            if original_ports is not None:
                state['_ports'] = resources_utils.simplify_ports(
                    [str(port) for port in original_ports])

        self.__dict__.update(state)

