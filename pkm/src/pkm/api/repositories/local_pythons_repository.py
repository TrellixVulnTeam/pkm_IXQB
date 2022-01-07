import os.path
import platform
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Set

from pkm.api.dependencies.dependency import Dependency
from pkm.api.environments.environment import Environment
from pkm.api.environments.lightweight_environment_builder import LightweightEnvironments
from pkm.api.packages.package import PackageDescriptor, Package
from pkm.api.repositories.repository import Repository
from pkm.api.versions.version import Version
from pkm.utils.http.http_monitors import FetchResourceMonitor
from pkm.utils.properties import cached_property
from pkm.utils.systems import is_executable

_DEFAULT_PKG_EXTRAS = {'pip', 'wheel', 'setuptools'}


class InstalledPythonsRepository(Repository):

    def __init__(self):
        super().__init__('local-pythons')

    @cached_property
    def _interpreters(self) -> List["LocalInterpreterPackage"]:
        result: List[LocalInterpreterPackage] = []
        interpreters_in_path = _interpreters_in_path()
        for interpreter_path in interpreters_in_path:
            try:
                cmdout = subprocess.run(
                    [str(interpreter_path), "-c", "import platform; print(platform.python_version())"],
                    capture_output=True)
                cmdout.check_returncode()

                result.append(LocalInterpreterPackage(
                    interpreter_path,
                    PackageDescriptor("python", Version.parse(cmdout.stdout.decode().strip())),
                    _DEFAULT_PKG_EXTRAS))

            except ChildProcessError:
                ...  # skip this interpreter

        return result

    def accepts(self, dependency: Dependency) -> bool:
        return dependency.package_name == 'python'

    def _do_match(self, dependency: Dependency) -> List[Package]:
        extras = set(dependency.extras) if dependency.extras is not None else _DEFAULT_PKG_EXTRAS

        return [
            p.with_extras(extras)
            for p in self._interpreters
            if dependency.version_spec.allows_version(p.version)]


class LocalInterpreterPackage(Package):

    def __init__(self, interpreter: Path, desc: PackageDescriptor, extras: Set[str]):
        self._interpreter = interpreter
        self._desc = desc
        self._extras = extras

    def with_extras(self, extras: Set[str]) -> "LocalInterpreterPackage":
        if self._extras == extras:
            return self
        return LocalInterpreterPackage(self._interpreter, self._desc, extras)

    @property
    def descriptor(self) -> PackageDescriptor:
        return self._desc

    def _all_dependencies(self, environment: "Environment", monitor: FetchResourceMonitor) -> List["Dependency"]:
        return []

    def is_compatible_with(self, env: Environment):
        return not env.path.exists() or next(env.path.iterdir(), None) is None

    def to_environment(self) -> Environment:
        return Environment(env_path=self._interpreter.parent, interpreter_path=self._interpreter)

    def install_to(self, env: "Environment", build_packages_repo: Optional[Repository] = None,
                   user_request: Optional[Dependency] = None):
        LightweightEnvironments.create(env.path, self._interpreter.absolute())


_OS = platform.system()
_PYTHON_EXEC_RX = re.compile(r"python-?[0-9.]*(.exe)?")


def _interpreters_in_path() -> Set[Path]:
    path_parts = [path for it in (os.environ.get("PATH") or "").split(os.pathsep) if (path := Path(it)).exists()]
    return {
        file.resolve()
        for path in path_parts
        for file in path.iterdir()
        if _PYTHON_EXEC_RX.fullmatch(file.name.lower()) and is_executable(file)}