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
            ``{'V100': {'gpu_memory_mib': 16384}}``.
          use_spot: whether to use a spot instance. If None, the system will
            decide whether to use a spot instance.
          spot_recovery: the type of recovery to use for the spot instance.
            If None, the system will decide whether to use a spot recovery.
          region: the region to use. If None, the system will decide which
            region to use.
          zone: the zone to use. If None, the system will decide which
            zone to use.
          image_id: the image ID to use. If a dict, must be a dict of the
            form ``{'us-west-2': 'ami-12345678'}``. If a str, must be
            a string of the form ``'ami-12345678'``.
          disk_size: the size of the disk in GB. If None, the system will
            decide which disk size to use.
          disk_tier: the tier of the disk. If None, the system will decide
            which disk tier to use.
          ports: the ports to use. If a str, must be a string of the
            form ``'8080'`` or ``'8080:8081'``, where the ``:8081`` indicates
            that the task requires two ports. If a list, must be a list of
            strings of the form ``'8080'`` or ``'8080:8081'``.
          _docker_login_config: the docker login config. If None, the system
            will decide which docker login config to use.
          _is_image_managed: whether the image is managed by the system.
            If None, the system will decide whether the image is managed.

        Raises:
          ValueError: if the resources are invalid.
        """
        # TODO(zhwu): Remove the following fields in the future.
        self._version = self._VERSION
        self._region = region
        self._zone = zone
        self._use_spot = use_spot
        self._spot_recovery = spot_recovery
        self._image_id = image_id
        self._disk_size = disk_size
        self._disk_tier = disk_tier
        self._ports = ports
        self._docker_login_config = _docker_login_config
        self._is_image_managed = _is_image_managed

        # The following fields are immutable once initialized.
        self._cloud = cloud
        self._instance_type = instance_type
        self._cpus = cpus
        self._memory = memory
        self._accelerators = accelerators
        self._accelerator_args = accelerator_args

        if self._version < 12:
            original_ports = self._ports
            if original_ports is not None:
                self._ports = resources_utils.simplify_ports(
                    [str(port) for port in original_ports])

        if self._version < 13:
            if self._docker_login_config is not None:
                self._docker_login_config = docker_utils.DockerLoginConfig(
                    self._docker_login_config)

        if self._version < 13:
            if self._is_image_managed is not None:
                self._is_image_managed = bool(self._is_image_managed)

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

        self.__dict__.update(state)

    @classmethod
    def from_yaml_config(cls, config: Dict[str, Union[str, int]]) -> 'Resources':
        """Creates a Resources object from a yaml-style dict of config.

        Args:
          config: a yaml-style dict of config for this resource bundle.

        Returns:
          A Resources object.
        """
        return cls(
            cloud=config.get('cloud'),
            instance_type=config.get('instance_type'),
            cpus=config.get('cpus'),
            memory=config.get('memory'),
            accelerators=config.get('accelerators'),
            accelerator_args=config.get('accelerator_args'),
            use_spot=config.get('use_spot'),
            spot_recovery=config.get('spot_recovery'),
            region=config.get('region'),
            zone=config.get('zone'),
            image_id=config.get('image_id'),
            disk_size=config.get('disk_size'),
            disk_tier=config.get('disk_tier'),
            ports=config.get('ports'),
            _docker_login_config=config.get('_docker_login_config'),
            _is_image_managed=config.get('_is_image_managed'),
        )

    @classmethod
    def from_dict(cls, config: Dict[str, Union[str, int]]) -> 'Resources':
        """Creates a Resources object from a dict of config.

        Args:
          config: a dict of config for this resource bundle.

        Returns:
          A Resources object.
        """
        return cls(
            cloud=clouds.Cloud.from_str(config.get('cloud')),
            instance_type=config.get('instance_type'),
            cpus=config.get('cpus'),
            memory=config.get('memory'),
            accelerators=config.get('accelerators'),
            accelerator_args=config.get('accelerator_args'),
            use_spot=config.get('use_spot'),
            spot_recovery=config.get('spot_recovery'),
            region=config.get('region'),
            zone=config.get('zone'),
            image_id=config.get('image_id'),
            disk_size=config.get('disk_size'),
            disk_tier=config.get('disk_tier'),
            ports=config.get('ports'),
            _docker_login_config=config.get('_docker_login_config'),
            _is_image_managed=config.get('_is_image_managed'),
        )

    @classmethod
    def from_str(cls, config: str) -> 'Resources':
        """Creates a Resources object from a string of config.

        Args:
          config: a string of config for this resource bundle.

        Returns:
          A Resources object.
        """
        config = config.strip()
        if not config:
            return cls()
        if config.startswith('{'):
            return cls.from_dict(json.loads(config))
        elif config.startswith('['):
            return cls.from_yaml_config(yaml.safe_load(config))
        else:
            raise ValueError(f'Invalid resource config: {config}')

    def __repr__(self) -> str:
        return f'Resources({self.to_yaml_config()})'

    def __str__(self) -> str:
        return self.to_yaml_config()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Resources):
            return NotImplemented
        return self.to_yaml_config() == other.to_yaml_config()

    def __ne__(self, other: object) -> bool:
        if not isinstance(other, Resources):
            return NotImplemented
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(self.to_yaml_config())

    @property
    def version(self) -> int:
        """Returns the version of the Resources object."""
        return self._version

    @property
    def cloud(self) -> Optional[clouds.Cloud]:
        """Returns the cloud."""
        return self._cloud

    @property
    def instance_type(self) -> Optional[str]:
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
        """Returns the type of recovery to use for the spot instance."""
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
        """Returns the ports to use."""
        return self._ports

    @property
    def docker_login_config(self) -> Optional[docker_utils.DockerLoginConfig]:
        """Returns the docker login config."""
        return self._docker_login_config

    @property
    def is_image_managed(self) -> Optional[bool]:
        """Returns whether the image is managed by the system."""
        return self._is_image_managed

    def copy(
        self,
        *,
        cloud: Optional[clouds.Cloud] = None,
        instance_type: Optional[str] = None,
        cpus: Optional[Union[int, float, str]] = None,
        memory: Optional[Union[int, float, str]] = None,
        accelerators: Optional[Union[str, Dict[str, int]]] = None,
        accelerator_args: Optional[Dict[str, str]] = None,
        use_spot: Optional[bool] = None,
        spot_recovery: Optional[str] = None,
        region: Optional[str] = None,
        zone: Optional[str] = None,
        image_id: Optional[Union[str, Dict[str, str]]] = None,
        disk_size: Optional[int] = None,
        disk_tier: Optional[Literal['high', 'medium', 'low']] = None,
        ports: Optional[Union[int, str, List[str], Tuple[str]]] = None,
        # Internal use only.
        _docker_login_config: Optional[docker_utils.DockerLoginConfig] = None,
        _is_image_managed: Optional[bool] = None,
    ) -> 'Resources':
        """Returns a new Resources object with the given properties updated.

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
            ``{'V100': {'gpu_memory_mib': 16384}}``.
          use_spot: whether to use a spot instance. If None, the system will
            decide whether to use a spot instance.
          spot_recovery: the type of recovery to use for the spot instance.
            If None, the system will decide whether to use a spot recovery.
          region: the region to use. If None, the system will decide which
            region to use.
          zone: the zone to use. If None, the system will decide which
            zone to use.
          image_id: the image ID to use. If a dict, must be a dict of the
            form ``{'us-west-2': 'ami-12345678'}``. If a str, must be
            a string of the form ``'ami-12345678'``.
          disk_size: the size of the disk in GB. If None, the system will
            decide which disk size to use.
          disk_tier: the tier of the disk. If None, the system will decide
            which disk tier to use.
          ports: the ports to use. If a str, must be a string of the
            form ``'8080'`` or ``'8080:8081'``, where the ``:8081`` indicates
            that the task requires two ports. If a list, must be a list of
            strings of the form ``'8080'`` or ``'8080:8081'``.
          _docker_login_config: the docker login config. If None, the system
            will decide which docker login config to use.
          _is_image_managed: whether the image is managed by the system.
            If None, the system will decide whether the image is managed.

        Returns:
          A new Resources object with the given properties updated.

        Raises:
          ValueError: if the resources are invalid.
        """
        resources_fields = {
            'cloud': self.cloud,
            'instance_type': self.instance_type,
            'cpus': self.cpus,
            'memory': self.memory,
            'accelerators': self.accelerators,
            'accelerator_args': self.accelerator_args,
            'use_spot': self.use_spot,
            'spot_recovery': self.spot_recovery,
            'region': self.region,
            'zone': self.zone,
            'image_id': self.image_id,
            'disk_size': self.disk_size,
            'disk_tier': self.disk_tier,
            'ports': self.ports,
            '_docker_login_config': self._docker_login_config,
            '_is_image_managed': self._is_image_managed,
        }

        if cloud is not None:
            resources_fields['cloud'] = cloud
        if instance_type is not None:
            resources_fields['instance_type'] = instance_type
        if cpus is not None:
            resources_fields['cpus'] = cpus
        if memory is not None:
            resources_fields['memory'] = memory
        if accelerators is not None:
            if isinstance(accelerators, str):
                accelerators = {
                    accelerator_registry.canonicalize_accelerator_name(acc):
                    acc_count for acc, acc_count in
                    accelerator_registry.parse_accelerators(accelerators).items()
                }
            elif isinstance(accelerators, dict):
                accelerators = {
                    accelerator_registry.canonicalize_accelerator_name(acc):
                    acc_count for acc, acc_count in accelerators.items()
                }
            else:
                raise ValueError(
                    f'Invalid accelerators: {accelerators}')
            resources_fields['accelerators'] = accelerators
        if accelerator_args is not None:
            if isinstance(accelerator_args, dict):
                accelerator_args = {
                    accelerator_registry.canonicalize_accelerator_name(acc):
                    acc_args for acc, acc_args in accelerator_args.items()
                }
            else:
                raise ValueError(
                    f'Invalid accelerator_args: {accelerator_args}')
            resources_fields['accelerator_args'] = accelerator_args
        if use_spot is not None:
            self._use_spot = use_spot
        if spot_recovery is not None:
            self._spot_recovery = spot_recovery
        if region is not None:
            self._region = region
        if zone is not None:
            self._zone = zone
        if image_id is not None:
            if isinstance(image_id, str):
                image_id = {self.region: image_id}
            elif isinstance(image_id, dict):
                pass
            else:
                raise ValueError(
                    f'Invalid image_id: {image_id}')
            self._image_id = image_id
        if disk_size is not None:
            self._disk_size = int(disk_size)
        if disk_tier is not None:
            self._disk_tier = disk_tier
        if ports is not None:
            if isinstance(ports, str):
                ports = [ports]
            elif isinstance(ports, (list, tuple)):
                pass
            else:
                raise ValueError(
                    f'Invalid ports: {ports}')
            self._ports = ports
        if _docker_login_config is not None:
            self._docker_login_config = docker_utils.DockerLoginConfig(
                _docker_login_config)
        if _is_image_managed is not None:
            self._is_image_managed = bool(_is_image_managed)

        return Resources(**resources_fields)

    def to_dict(self) -> Dict[str, Union[str, int]]:
        """Returns a dict of config for this resource bundle."""
        return self.to_yaml_config()

    def to_yaml(self) -> str:
        """Returns a yaml string of this resource bundle."""
        return yaml.safe_dump(self.to_yaml_config())

    def to_json(self) -> str:
        """Returns a json string of this resource bundle."""
        return json.dumps(self.to_yaml_config())

    def to_str(self) -> str:
        """Returns a string of this resource bundle."""
        return self.to_yaml()

    def to_config(self) -> schemas.Resources:
        """Returns a ResourcesConfig object for this resource bundle."""
        return schemas.Resources(
            cloud=self.cloud,
            instance_type=self.instance_type,
            cpus=self.cpus,
            memory=self.memory,
            accelerators=self.accelerators,
            accelerator_args=self.accelerator_args,
            use_spot=self.use_spot,
            spot_recovery=self.spot_recovery,
            region=self.region,
            zone=self.zone,
            image_id=self.image_id,
            disk_size=self.disk_size,
            disk_tier=self.disk_tier,
            ports=self.ports,
            docker_login_config=self.docker_login_config,
            is_image_managed=self.is_image_managed,
        )

    def to_resources_config(self) -> schemas.Resources:
        """Returns a ResourcesConfig object for this resource bundle."""
        return self.to_config()

    @property
    def is_launchable(self) -> bool:
        """Returns whether the Resources is fully specified to launch an
        instance."""
        return (self.cloud is not None and
                self.instance_type is not None and
                self.accelerators is not None)

    @property
    def is_valid(self) -> bool:
        """Returns whether the Resources is valid."""
        if not self.is_launchable:
            return False
        if self.accelerators is not None:
            for acc, acc_count in self.accelerators.items():
                if acc_count < 0:
                    return False
        return True

    @property
    def is_managed(self) -> bool:
        """Returns whether the Resources is managed by the system."""
        return self.is_image_managed is True

    @property
    def is_managed_by_user(self) -> bool:
        """Returns whether the Resources is managed by the user."""
        return self.is_image_managed is False

    def get_launchable_instances(
            self,
            *,
            region: Optional[str] = None,
            zone: Optional[str] = None,
            image_id: Optional[str] = None,
            disk_size: Optional[int] = None,
            disk_tier: Optional[Literal['high', 'medium', 'low']] = None,
            ports: Optional[Union[int, List[str], Tuple[str]]] = None,
            accelerator_args: Optional[Dict[str, str]] = None,
            _docker_login_config: Optional[docker_utils.DockerLoginConfig] = None,
            _is_image_managed: Optional[bool] = None) -> Set['Resources']:
        """Returns a set of launchable instances that match the Resources.

        Args:
          region: the region to use. If None, the system will decide which
            region to use.
          zone: the zone to use. If None, the system will decide which
            zone to use.
          image_id: the image ID to use. If a dict, must be a dict of the
            form ``{'us-west-2': 'ami-12345678'}``. If a str, must be
            a string of the form ``'ami-12345678'``.
          disk_size: the size of the disk in GB. If None, the system will
            decide which disk size to use.
          disk_tier: the tier of the disk. If None, the system will decide
            which disk tier to use.
          ports: the ports to use. If a str, must be a string of the
            form ``'8080'`` or ``'8080:8081'``, where the ``:8081`` indicates
            that the task requires two ports. If a list, must be a list of
            strings of the form ``'8080'`` or ``'8080:8081'``.
          accelerator_args: the accelerator-specific arguments required.
            If a dict, must be a dict of the form
            ``{'V100': {'gpu_memory_mib': 16384}}``.
          _docker_login_config: the docker login config. If None, the system
            will decide which docker login config to use.
          _is_image_managed: whether the image is managed by the system.
            If None, the system will decide whether the image is managed.

        Returns:
          A set of launchable instances that match the Resources.
        """
        if self._version < 12:
            original_ports = self.ports
            if original_ports is not None:
                ports = resources_utils.simplify_ports(
                    [str(port) for port in original_ports])

        if self._version < 13:
            if _docker_login_config is not None:
                _docker_login_config = docker_utils.DockerLoginConfig(
                    _docker_login_config)

        if self._version < 13:
            if _is_image_managed is not None:
                _is_image_managed = bool(_is_image_managed)

        if self._version < 12:
            original_ports = self.ports
            if original_ports is not None:
                ports = resources_utils.simplify_ports(
                    [str(port) for port in original_ports])

        if self._version < 13:
            if _docker_login_config is not None:
                _docker_login_config = docker_utils.DockerLoginConfig(
                    _docker_login_config)

        if self._version < 13:
            if _is_image_managed is not None:
                _is_image_managed = bool(_is_image_managed)

        if not self.is_launchable:
            raise ValueError(
                'Resources is not fully specified to launch an '
                'instance: {self}')
        if not self.is_valid:
            raise ValueError(
                'Resources is invalid: {self}')

        if self.cloud is not None:
            region = self.region or region
            zone = self.zone or zone
            image_id = self.image_id or image_id
            disk_size = self.disk_size or disk_size
            disk_tier = self.disk_tier or disk_tier
            ports = self.ports or ports
            accelerator_args = self.accelerator_args or accelerator_args
            _docker_login_config = self.docker_login_config or _docker_login_config
            _is_image_managed = self.is_image_managed or _is_image_managed
        else:
            region = region or self.region
            zone = zone or self.zone
            image_id = image_id or self.image_id
            disk_size = disk_size or self.disk_size
            disk_tier = disk_tier or self.disk_tier
            ports = ports or self.ports
            accelerator_args = accelerator_args or self.accelerator_args
            _docker_login_config = _docker_login_config or self.docker_login_config
            _is_image_managed = _is_image_managed or self.is_image_managed

        if region is None:
            region = self.cloud.get_default_region()
        if zone is None:
            zone = self.cloud.get_default_zone(region)
        if image_id is None:
            image_id = self.cloud.get_default_image_id(region)
        if disk_size is None:
            disk_size = self.cloud.get_default_disk_size(region)
        if disk_tier is None:
            disk_tier = self.cloud.get_default_disk_tier(region)
        if ports is None:
            ports = self.cloud.get_default_ports(region)
        if accelerator_args is None:
            accelerator_args = self.cloud.get_default_accelerator_args(region)
        if _docker_login_config is None:
            _docker_login_config = self.cloud.get_default_docker_login_config()
        if _is_image_managed is None:
            _is_image_managed = self.cloud.get_default_is_image_managed()

        # TODO(zhwu): Remove the following fields in the future.
        self._region = region
        self._zone = zone
        self._image_id = image_id
        self._disk_size = disk_size
        self._disk_tier = disk_tier
        self._ports = ports
        self._docker_login_config = _docker_login_config
        self._is_image_managed = _is_image_managed

        return {
            Resources(
                cloud=self.cloud,
                instance_type=self.instance_type,
                cpus=self.cpus,
                memory=self.memory,
                accelerators=self.accelerators,
                accelerator_args=accelerator_args,
                use_spot=self.use_spot,
                spot_recovery=self.spot_recovery,
                region=region,
                zone=zone,
                image_id=image_id,
                disk_size=disk_size,
                disk_tier=disk_tier,
                ports=ports,
                _docker_login_config=_docker_login_config,
                _is_image_managed=_is_image_managed)
        }

    @property
    def is_managed_by_user(self) -> bool:
        """Returns whether the Resources is managed by the user."""
        return self.is_image_managed is False

    @property
    def is_managed(self) -> bool:
        """Returns whether the Resources is managed by the system."""
        return self.is_image_managed is True

    @property
    def is_launchable(self) -> bool:
        """Returns whether the Resources is fully specified to launch an
        instance."""
        return (self.cloud is not None and
                self.instance_type is not None and
                self.accelerators is not None)

    @property
    def is_valid(self) -> bool:
        """Returns whether the Resources is valid."""
        if not self.is_launchable:
            return False
        if self.accelerators is not None:
            for acc, acc_count in self.accelerators.items():
                if acc_count < 0:
                    return False
        return True

    @property
    def is_managed(self) -> bool:
        """Returns whether the Resources is managed by the system."""
        return self.is_image_managed is True

    @property
    def is_managed_by_user(self) -> bool:
        """Returns whether the Resources is managed by the user."""
        return self.is_image_managed is False

    @property
    def is_launchable(self) -> bool:
        """Returns whether the Resources is fully specified to launch an
        instance."""
        return (self.cloud is not None and
                self.instance_type is not None and
                self.accelerators is not None)

    @property
    def is_valid(self) -> bool:
        """Returns whether the Resources is valid."""
        if not self.is_launchable:
            return False
        if self.accelerators is not None:
            for acc, acc_count in self.accelerators.items():
                if acc_count < 0:
                    return False
        return True

    @property
    def is_managed(self) -> bool:
        """Returns whether the Resources is managed by the system."""
        return self.is_image_managed is True

    @property
    def is_managed_by_user(self) -> bool:
        """Returns whether the Resources is managed by the user."""
        return self.is_image_managed is False

    def get_launchable_instances(
            self,
            *,
            region: Optional[str] = None,
            zone: Optional[str] = None,
            image_id: Optional[str] = None,
            disk_size: Optional[int] = None,
            disk_tier: Optional[Literal['high', 'medium', 'low']] = None,
            ports: Optional[Union[int, List[str], Tuple[str]]] = None,
            accelerator_args: Optional[Dict[str, str]] = None,
            _docker_login_config: Optional[docker_utils.DockerLoginConfig] = None,
            _is_image_managed: Optional[bool] = None) -> Set['Resources']:
        """Returns a set of launchable instances that match the Resources.

        Args:
          region: the region to use. If None, the system will decide which
            region to use.
          zone: the zone to use. If None, the system will decide which
            zone to use.
          image_id: the image ID to use. If a dict, must be a dict of the
            form ``{'us-west-2': 'ami-12345678'}``. If a str, must be
            a string of the form ``'ami-12345678'``.
          disk_size: the size of the disk in GB. If None, the system will
            decide which disk size to use.
          disk_tier: the tier of the disk. If None, the system will decide
            which disk tier to use.
          ports: the ports to use. If a str, must be a string of the
            form ``'8080'`` or ``'8080:8081'``, where the