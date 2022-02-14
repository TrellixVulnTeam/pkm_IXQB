import shutil
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, cast

from pkm.api.dependencies.dependency import Dependency
from pkm.api.packages.package import Package, PackageDescriptor
from pkm.api.projects.project import Project
from pkm.api.projects.project_group import ProjectGroup
from pkm.api.repositories.repository import Repository
from pkm.api.versions.version import NamedVersion
from pkm.utils.commons import NoSuchElementException
from pkm.utils.http.http_client import Url
from pkm.utils.iterators import single_or_raise
from pkm.utils.processes import monitored_run
from pkm.utils.properties import cached_property


# noinspection PyMethodMayBeStatic
class _Git:
    def __init__(self):
        self._git_cmd = shutil.which('git')
        if not self._git_cmd:
            raise NoSuchElementException("could not find the git command in your path")

    def is_valid_git_dir(self, dir_: Path) -> bool:
        return (dir_ / '.git').exists()

    def _is_detached(self, dir_: Path):
        """
        :param dir_: the cloned repository directory to look at
        :return: True if the repository directory is in a detached head state, False if it in a branch
        """
        head = (dir_ / '.git/HEAD').read_text()
        return not (dir_ / '.git/heads' / head).exists()

    def update(self, package_name: str, repo_dir: Path):
        if self._is_detached(repo_dir):
            return

        monitored_run(
            f'fetch {package_name}', [self._git_cmd, 'pull'], cwd=str(repo_dir)).check_returncode()

    def clone(self, package_name: str, repository_url: str, branch_or_commit: Optional[str], target_dir: Path):
        # TODO: the monitoring should probably run as part of a larger "match" monitored operation

        try:
            monitored_run(
                f'fetch {package_name}', [self._git_cmd, 'clone', repository_url, str(target_dir)]).check_returncode()

            if branch_or_commit:
                monitored_run(
                    f'fetch {package_name}', [self._git_cmd, 'checkout', branch_or_commit],
                    cwd=str(target_dir)).check_returncode()
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise


class GitRepository(Repository):

    def __init__(self, workspace: Path):
        super().__init__("git")
        self._workspace = workspace

    @cached_property
    def _git_client(self) -> _Git:
        return _Git()

    def _do_match(self, dependency: Dependency) -> List[Package]:
        if not (url := dependency.url) or url.repository_protocol not in ('git', None):
            return []

        parts = url.url.split("@")
        url = Url.parse(parts[0])
        if (sz := len(parts)) == 1:
            branch = None
        elif sz == 2:
            branch = parts[1]
        else:
            raise ValueError(f"malformed git url: {url}")

        target_dir = self._workspace / dependency.package_name / url.host / url.path.lstrip('/')
        if branch:
            target_dir = target_dir / branch

        if target_dir.exists():
            self._git_client.update(dependency.package_name, target_dir)
        else:
            self._git_client.clone(dependency.package_name, str(url), branch, target_dir)

        desc = PackageDescriptor(dependency.package_name, dependency.url.to_version_spec().version)

        if ProjectGroup.is_group_dir(target_dir):
            project = single_or_raise(p for p in ProjectGroup.load(target_dir).project_children_recursive
                                      if p.name == dependency.package_name)
            project.bump_version('name', cast(NamedVersion, desc.version).name, save=False)
        else:
            project = Project.load(target_dir, package=desc)

        return [_GitPackageWrapper(project)]


class _GitPackageWrapper(Package):

    def __init__(self, project: Project):
        self._project = project

    @property
    def descriptor(self) -> PackageDescriptor:
        return self._project.descriptor

    def _all_dependencies(self, environment: "Environment") -> List["Dependency"]:
        return self._project.dependencies(environment)

    def is_compatible_with(self, env: "Environment") -> bool:
        return self._project.is_compatible_with(env)

    def install_to(self, env: "Environment", user_request: Optional["Dependency"] = None):
        return self._project.install_to(env, user_request, editable=False)
