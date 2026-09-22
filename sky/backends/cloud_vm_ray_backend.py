"""Backend: runs on cloud virtual machines, managed by Ray."""
import ast
import copy
import enum
import getpass
import inspect
import json
import math
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import typing
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

import colorama
import filelock

import sky
from sky import backends
from sky import cloud_stores
from sky import clouds
from sky import exceptions
from sky import global_user_state
from sky import optimizer
from sky import provision as provision_lib
from sky import resources as resources_lib
from sky import sky_logging
from sky import skypilot_config
from sky import spot as spot_lib
from sky import status_lib
from sky import task as task_lib
from sky.backends import backend_utils
from sky.backends import onprem_utils
from sky.backends import wheel_utils
from sky.data import data_utils
from sky.data import storage as storage_lib
from sky.data import storage_utils
from sky.data import storage_utils
from sky.provision import instance_setup
from sky.provision import metadata_utils
from sky.provision import provisioner
from sky.skylet import autostop_lib
from sky.skylet import constants
from sky.skylet import job_lib
from sky.skylet import log_lib
from sky.skylet import log_lib
from sky.usage import usage_lib
from sky.utils import command_runner
from sky.utils import common_utils
from sky.utils import env_options
from sky.utils import log_utils
from sky.utils import resources_utils
from sky.utils import rich_utils
from sky.utils import subprocess_utils
from sky.utils import timeline
from sky.utils import tpu_utils
from sky.utils import ux_utils

if typing.TYPE_CHECKING:
    from sky import dag

Path = str

SKY_REMOTE_APP_DIR = backend_utils.SKY_REMOTE_APP_DIR
SKY_REMOTE_WORKDIR = constants.SKY_REMOTE_WORKDIR

logger = sky_logging.init_logger(__name__)

_PATH_SIZE_MEGABYTES_WARN_THRESHOLD = 256

# Timeout (seconds) for provision progress: if in this duration no new nodes
# are launched, abort and failover.
_NODES_LAUNCHING_PROGRESS_TIMEOUT = {
    clouds.AWS: 90,
    clouds.Azure: 90,
    clouds.GCP: 240,
    clouds.Lambda: 150,
    clouds.IBM: 160,
    clouds.Local: 90,
    clouds.OCI: 300,
    clouds.Kubernetes: 300,
}

# Time gap between retries after failing to provision in all possible places.
# Used only if --retry-until-up is set.
_RETRY_UNTIL_UP_INIT_GAP_SECONDS = 60

# The maximum retry count for fetching IP address.
_FETCH_IP_MAX_ATTEMPTS = 3

_TEARDOWN_FAILURE_MESSAGE = (
    f'\n{colorama.Fore.RED}Failed to terminate '
    '{cluster_name}. {extra_reason}'
    'If you want to ignore this error and remove the cluster '
    'from the status table, use `sky down --purge`.'
    f'{colorama.Style.RESET_ALL}\n'
    '**** STDOUT ****\n'
    '{stdout}\n'
    '**** STDERR ****\n'
    '{stderr}')

_TEARDOWN_PURGE_WARNING = (
    f'{colorama.Fore.YELLOW}'
    'WARNING: Received non-zero exit code from {reason}. '
    'Make sure resources are manually deleted.\n'
    'Details: {details}'
    f'{colorama.Style.RESET_ALL}')

_RSYNC_NOT_FOUND_MESSAGE = (
    '`rsync` command is not found in the specified image. '
    'Please use an image with rsync installed.')

_TPU_NOT_FOUND_ERROR = 'ERROR: (gcloud.compute.tpus.delete) NOT_FOUND'

_CTRL_C_TIP_MESSAGE = ('INFO: Tip: use Ctrl-C to exit log streaming '
                       '(task will not be killed).')

_MAX_RAY_UP_RETRY = 5

# Number of retries for getting zones.
_MAX_GET_ZONE_RETRY = 3

_JOB_ID_PATTERN = re.compile(r'Job ID: ([0-9]+)')

# Path to the monkey-patched ray up script.
# We don't do import then __file__ because that script needs to be filled in
# (so import would fail).
_RAY_UP_WITH_MONKEY_PATCHED_HASH_LAUNCH_CONF_PATH = (
    pathlib.Path(sky.__file__).resolve().parent / 'backends' /
    'monkey_patches' / 'monkey_patch_ray_up.py')

# Restart skylet when the version does not match to keep the skylet up-to-date.
_MAYBE_SKYLET_RESTART_CMD = 'python3 -m sky.skylet.attemp
# ... [truncated] ...
o = isinstance(handle.launched_resources.cloud, clouds.Local)
        codegen.add_ray_task(
            bash_script=command_for_node,
            env_vars=task.envs,
            task_name=task.name,
            job_run_id=job_run_id,
            ray_resources_dict=backend_utils.get_task_demands_dict(task),
            log_dir=log_dir,
            use_sudo=use_sudo)

        codegen.add_epilogue()

        self._exec_code_on_head(handle,
                                codegen.build(),
                                job_id,
                                executable='python3',
                                detach_run=detach_run,
                                spot_dag=task.spot_dag)

    def _execute_task_n_nodes(self, handle: CloudVmRayResourceHandle,
                              task: task_lib.Task, job_id: int,
                              detach_run: bool) -> None:
        # Strategy:
        #   ray.init(...)
        #   for node:
        #     submit _run_cmd(cmd) with resource {node_i: 1}
        log_dir_base = self.log_dir
        log_dir = os.path.join(log_dir_base, 'tasks')
        accelerator_dict = backend_utils.get_task_demands_dict(task)
        internal_ips = handle.internal_ips()
        assert internal_ips is not None, 'internal_ips is not cached in handle'

        # If TPU VM Pods is used, #num_nodes should be #num_tpu_devices
        is_tpu_vm_pod = tpu_utils.is_tpu_vm_pod(handle.launched_resources)
        if is_tpu_vm_pod:
            num_actual_nodes = tpu_utils.get_num_tpu_devices(
                handle.launched_resources)
        else:
            num_actual_nodes = task.num_nodes
        assert isinstance(num_actual_nodes, int), num_actual_nodes

        codegen = RayCodeGen()
        is_local = isinstance(handle.launched_resources.cloud, clouds.Local)
        codegen.add_prologue(job_id, is_local=is_local)
        codegen.add_gang_scheduling_placement_group_and_setup(
            num_actual_nodes,
            accelerator_dict,
            stable_cluster_internal_ips=internal_ips,
            setup_cmd=self._setup_cmd,
            setup_log_path=os.path.join(log_dir, 'setup.log'),
            envs=task.envs)

        if callable(task.run):
            run_fn_code = textwrap.dedent(inspect.getsource(task.run))
            run_fn_name = task.run.__name__
            codegen.register_run_fn(run_fn_code, run_fn_name)

        # If it is a managed spot job, the TASK_ID_ENV_VAR will have been
        # already set by the controller.
        job_run_id = task.envs.get(
            constants.TASK_ID_ENV_VAR,
            common_utils.get_global_job_id(self.run_timestamp,
                                           cluster_name=handle.cluster_name,
                                           job_id=str(job_id)))

        # TODO(zhwu): The resources limitation for multi-node ray.tune and
        # horovod should be considered.
        for i in range(num_actual_nodes):
            command_for_node = task.run if isinstance(task.run, str) else None

            # Ray's per-node resources, to constrain scheduling each command to
            # the corresponding node, represented by private IPs.
            use_sudo = isinstance(handle.launched_resources.cloud, clouds.Local)
            codegen.add_ray_task(
                bash_script=command_for_node,
                env_vars=task.envs,
                task_name=task.name,
                job_run_id=job_run_id,
                ray_resources_dict=accelerator_dict,
                log_dir=log_dir,
                gang_scheduling_id=i,
                use_sudo=use_sudo,
            )

        codegen.add_epilogue()
        # TODO(zhanghao): Add help info for downloading logs.
        self._exec_code_on_head(handle,
                                codegen.build(),
                                job_id,
                                executable='python3',
                                detach_run=detach_run,
                                spot_dag=task.spot_dag)

