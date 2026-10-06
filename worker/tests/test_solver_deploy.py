"""DockerSandbox.deployed: Coolify renames a compose file's images, volumes and networks."""

from types import SimpleNamespace

from lms_worker.solver.sandbox import DockerSandbox


class NotFound(Exception):
    pass


class FakeDocker:
    def __init__(self, images, networks, containers, hostname_known=False):
        self._images, self._networks, self._containers = images, networks, containers
        self.hostname_known = hostname_known
        self.images = SimpleNamespace(get=self._get_image, list=lambda: [SimpleNamespace(tags=[t]) for t in images])
        self.created = []
        self.networks = SimpleNamespace(get=self._get_net, create=lambda name, driver: self.created.append(name),
                                        list=lambda: [SimpleNamespace(name=n, attrs={"Internal": i}) for n, i in networks.items()])
        self.containers = SimpleNamespace(get=self._get_container, list=self._list)

    def _get_image(self, name):
        if name not in self._images:
            raise NotFound(name)

    def _get_net(self, name):
        if name not in self._networks:
            raise NotFound(name)

    def _get_container(self, cid):
        if not self.hostname_known:
            raise NotFound(cid)
        return self._containers["worker"]

    def _list(self, filters):
        service = filters["label"].split("=", 1)[1]
        return [self._containers[service]] if service in self._containers else []


def mounts(name):
    return SimpleNamespace(attrs={"Mounts": [{"Type": "volume", "Destination": "/data", "Name": "x_artifacts"},
                                             {"Type": "volume", "Destination": "/claude-home", "Name": name}]})


def test_names_as_compose_gives_them():
    client = FakeDocker(["lms-solver:latest"], {"lms-sandbox": True}, {"worker": mounts("lms-claude-home")}, hostname_known=True)
    box = DockerSandbox.deployed("lms-solver:latest", "lms-sandbox", None, "/claude-home", client=client)
    assert (box.image, box.network, box.login_volume) == ("lms-solver:latest", "lms-sandbox", "lms-claude-home")


def test_names_as_coolify_renames_them():
    client = FakeDocker(["k8c4w0-solver-image:latest", "postgres:16-alpine"], {"k8c4w0_sandbox": False, "k8c4w0": False},
                        {"api": mounts("k8c4w0_claude-home")})
    box = DockerSandbox.deployed("lms-solver:latest", "lms-sandbox", None, "/claude-home", client=client)
    assert (box.image, box.network, box.login_volume) == ("k8c4w0-solver-image:latest", "k8c4w0_sandbox", "k8c4w0_claude-home")


def test_a_missing_sandbox_network_is_made():
    client = FakeDocker(["lms-solver:latest"], {"bridge": False}, {"worker": mounts("v")}, hostname_known=True)
    box = DockerSandbox.deployed("lms-solver:latest", "lms-sandbox", None, "/claude-home", client=client)
    assert box.network == "lms-sandbox" and client.created == ["lms-sandbox"]


def test_an_explicit_login_volume_wins():
    client = FakeDocker(["lms-solver:latest"], {"lms-sandbox": True}, {"worker": mounts("auto")}, hostname_known=True)
    box = DockerSandbox.deployed("lms-solver:latest", "lms-sandbox", "my-volume", "/claude-home", client=client)
    assert box.login_volume == "my-volume"
