from typing import Dict

from assemblyline_core.scaler.scaling import ServiceProfile

from assemblyline.odm.models.service import DockerConfig
from .interface import ControllerInterface, ServiceControlError


class DockerController(ControllerInterface):
    """A controller for *non* swarm mode docker."""
    def __init__(self, logger, prefix='', labels=None, cpu_overallocation=1, memory_overallocation=1):
        """
        :param logger: A logger to report status and debug information.
        :param prefix: A prefix used to distinguish containers launched by this controller.
        :param cpu_overallocation: A multiplier on CPU usage. (2 means act like there are twice as many CPU present)
        :param memory_overallocation: A multiplier on memory usage. (2 means act like there is twice as much memory)
        """
        # Connect to the host docker port
        import docker
        self.client = docker.from_env()
        self.log = logger
        self.global_mounts = []
        self._prefix = prefix
        self._labels = labels

        # CPU and memory reserved for the host
        self._reserved_cpu = 0.3
        self._reserved_mem = 500
        self.cpu_overallocation = cpu_overallocation
        self.memory_overallocation = memory_overallocation
        self._profiles: Dict[str, ServiceProfile] = {}

        # Prefetch some info that shouldn't change while we are running
        self._info = self.client.info()

        # We aren't checking for swarm nodes
        assert not self._info['Swarm']['NodeID']

    def add_profile(self, profile, updates=None):
        """Tell the controller about a service profile it needs to manage."""
        self._profiles[profile.name] = profile

    def _start(self, service_name):
        """Launch a docker container in a manner suitable for Assembylyline."""
        container_name = self._name_container(service_name)
        prof = self._profiles[service_name]
        cfg = prof.container_config
        labels = dict(self._labels)
        labels.update({'component': service_name})
        self.client.containers.run(
            image=cfg.image,
            name=container_name,
            cpu_period=100000,
            cpu_quota=int(100000*prof.container_config.cpu_cores),
            mem_limit=f'{prof.container_config.ram_mb}m',
            labels=labels,
            restart_policy={'Name': 'always'},
            command=cfg.command,
            volumes={row[0]: {'bind': row[1], 'mode': 'ro'} for row in self.global_mounts},
            network=cfg.network[0],
            environment=[f'{_e.name}={_e.value}' for _e in cfg.environment],
            detach=True,
        )

        # TODO network needs to be adjusted

    def _name_container(self, service_name):
        """Find an unused name for a container.

        Container names must be unique, but we want our names to be predictable and informative.
        Cycle through the pattern we want until we find the lowest free numerical suffix.
        """
        # Load all container names on the system now
        used_names = []
        for container in self.client.containers.list(all=True):
            used_names.append(container.name)

        # Try names until one works
        used_names = set(used_names)
        index = 0
        while True:
            name = f'{service_name}_{index}'
            if self._prefix:
                name = self._prefix + '_' + name
            if name not in used_names:
                return name
            index += 1

    def free_cpu(self):
        """Try to estimate how much CPU the docker host has unreserved.

        NOTE: There is probably a better way to do this.
        """
        cpu = self._info['NCPU'] * self.cpu_overallocation - self._reserved_cpu
        for container in self.client.containers.list():
            if container.attrs['HostConfig']['CpuPeriod']:
                cpu -= container.attrs['HostConfig']['CpuQuota']/container.attrs['HostConfig']['CpuPeriod']
        self.log.debug(f'Total CPU available {cpu}/{self._info["NCPU"]}')
        return cpu

    def free_memory(self):
        """Try to estimate how much RAM the docker host has unreserved.

        NOTE: There is probably a better way to do this.
        """
        mem = self._info['MemTotal']/1024 * self.memory_overallocation - self._reserved_mem
        for container in self.client.containers.list():
            mem -= container.attrs['HostConfig']['Memory']/1024
        self.log.debug(f'Total Memory available {mem}/{self._info["MemTotal"]/2**10}')
        return mem

    def get_target(self, service_name):
        """Get how many instances of a service we expect to be running.

        Since we start our containers with 'restart always' we just need to count how many
        docker is currently trying to keep running.
        """
        running = 0
        for container in self.client.containers.list(filters={'label': f'component={service_name}'}):
            if container.status in {'restarting', 'running'}:
                running += 1
            elif container.status in {'created', 'removing', 'paused', 'exited', 'dead'}:
                pass
            else:
                self.log.warning(f"Unknown docker status string: {container.status}")
        return running

    def set_target(self, service_name, target):
        """Change how many instances of a service docker is trying to keep up.

        This is managed by killing extra containers at random, or launching new ones.
        """
        try:
            running = self.get_target(service_name)
            self.log.debug(f"New target for {service_name}: {running} -> {target}")
            delta = target - running

            if delta < 0:
                # Kill off delta instances of of the service
                running = [container for container in self.client.containers.list(filters={'label': f'component={service_name}'})
                           if container.status in {'restarting', 'running'}]
                running = running[0:-delta]
                for container in running:
                    container.kill()

            if delta > 0:
                # Start delta instances of the service
                for _ in range(delta):
                    self._start(service_name)

            # Every time we change our container allocation do a little clean up to keep things fresh
            self.client.containers.prune()
            self.client.volumes.prune()
        except Exception as error:
            raise ServiceControlError(str(error), service_name)