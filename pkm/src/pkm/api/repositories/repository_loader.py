from __future__ import annotations

import warnings
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple, Iterable

from pkm.api.dependencies.dependency import Dependency
from pkm.api.environments.environment import Environment
from pkm.api.environments.environments_zoo import EnvironmentsZoo
from pkm.api.packages.package import Package, PackageDescriptor
from pkm.api.projects.project import Project
from pkm.api.projects.project_group import ProjectGroup
from pkm.api.repositories.repository import Repository, RepositoryBuilder, AbstractRepository
from pkm.repositories.shared_pacakges_repo import SharedPackagesRepository
from pkm.config.configuration import TomlFileConfiguration, computed_based_on
from pkm.resolution.packages_lock import LockPrioritizingRepository
from pkm.utils.commons import NoSuchElementException
from pkm.utils.dicts import remove_none_values, put_if_absent, udict_hash
from pkm.utils.http.http_client import HttpClient

REPOSITORIES_ENTRYPOINT_GROUP = "pkm-repositories"
REPOSITORIES_CONFIGURATION_PATH = "etc/pkm/repositories.toml"


class RepositoryLoader:
    def __init__(self, main_cfg: Path, http: HttpClient, workspace: Path):

        from pkm.api.environments.environment import Environment
        from pkm.repositories.simple_repository import SimpleRepositoryBuilder
        from pkm.repositories.git_repository import GitRepository
        from pkm.repositories.pypi_repository import PyPiRepository
        from pkm.repositories.local_packages_repository import LocalPackagesRepositoryBuilder
        from pkm.repositories.url_repository import UrlRepository

        # base repositories
        self.pypi = PyPiRepository(http)
        self._cached_instances: Dict[RepositoryInstanceConfig, Repository] = {}

        self._url_repos = {
            r.name: r for r in (
                GitRepository(workspace / 'git'),
                UrlRepository(),
            )
        }

        # common builders
        self._builders = {
            b.name: b for b in (
                SimpleRepositoryBuilder(http),
                LocalPackagesRepositoryBuilder(),
            )
        }

        # builders from entrypoints
        for epoint in Environment.current().entrypoints[REPOSITORIES_ENTRYPOINT_GROUP]:
            try:
                builder: RepositoryBuilder = epoint.ref.import_object()()
                if not isinstance(builder, RepositoryBuilder):
                    raise ValueError("repositories entrypoint did not point to a repository builder class")
            except Exception:  # noqa
                import traceback
                warnings.warn(f"malformed repository entrypoint: {epoint}")
                traceback.print_exc()

        self._main = self._compose('main', main_cfg, self.pypi)
        self.workspace = workspace

    @property
    def main(self) -> Repository:
        return self._main

    def load_for_env_zoo(self, zoo: EnvironmentsZoo) -> Repository:
        config = zoo.path / REPOSITORIES_CONFIGURATION_PATH
        repo = self.main
        if config.exists():
            repo = self._compose("zoo-configured-repository", config, repo)

        if zoo.config.package_sharing.enabled:
            repo = SharedPackagesRepository(zoo.path / ".zoo/shared", repo)

        return repo

    def load_for_env(self, env: Environment) -> Repository:
        repo = self.main
        if zoo := env.zoo:
            repo = zoo.attached_repository

        config = env.path / REPOSITORIES_CONFIGURATION_PATH
        if config.exists():
            repo = self._compose("env-configured-repository", config, repo)

        return repo

    def load_for_project(self, project: Project) -> Repository:
        repo = project.attached_environment.attached_repository
        if group := project.group:
            repo = self._load_for_project_group(group, repo)
        else:
            repo = _ProjectsRepository.create('project-repository', [project], repo)

        repo = LockPrioritizingRepository(
            "lock-prioritizing-repository", repo, project.lock,
            project.attached_environment)

        return repo

    def load_for_project_group(self, group: ProjectGroup) -> Repository:
        return self._load_for_project_group(group, None)

    def _load_for_project_group(self, group: ProjectGroup, base_repo: Optional[Repository] = None) -> Repository:
        repo = base_repo or self.main

        config = group.path / REPOSITORIES_CONFIGURATION_PATH
        if config.exists():
            repo = self._compose("group-configured-repository", config, repo)

        repo = _ProjectsRepository.create("group-projects-repository", group.project_children_recursive, repo)
        return repo

    def _compose(self, name: str, config_path: Path, main: Repository) -> Repository:
        package_search_list = []
        package_associated_repo = {}

        config = RepositoriesConfiguration.load(config_path)

        if not (new_repos := config.repositories):
            return main

        for definition in new_repos:
            instance = self.build(definition)

            if definition.packages:
                for package in definition.packages:
                    put_if_absent(package_associated_repo, package, instance)
            else:
                package_search_list.append(instance)

        package_search_list.append(main)

        return _CompositeRepository(name, self._url_repos, package_search_list, package_associated_repo)

    def build(self, config: RepositoryInstanceConfig) -> Repository:
        if not (cached := self._cached_instances.get(config)):
            if not (builder := self._builders.get(config.type)):
                raise KeyError(f"unknown repository type: {config.type}")
            cached = builder.build(config.name, config.packages, **config.args)
            self._cached_instances[config] = cached

        return cached


class RepositoriesConfiguration(TomlFileConfiguration):
    repositories: List[RepositoryInstanceConfig]

    @computed_based_on("")
    def repositories(self) -> List[RepositoryInstanceConfig]:
        return [RepositoryInstanceConfig.from_config(name, repo) for name, repo in self.items()]


@dataclass(frozen=True, eq=True)
class RepositoryInstanceConfig:
    type: str
    packages: Optional[List[str]]
    name: Optional[str]
    args: Dict[str, Any]

    def __hash__(self):
        h = 7
        h = h * 31 + hash(self.type)
        h = h * 31 + hash(self.packages)
        h = h * 31 + hash(self.name)
        h = h * 31 + udict_hash(self.args)

        return h

    def to_config(self) -> Dict[str, Any]:
        return remove_none_values({
            **self.args,
            'type': self.type,
            'packages': self.packages,
        })

    @classmethod
    def from_config(cls, name: str, config: Dict[str, Any]) -> "RepositoryInstanceConfig":
        config = copy(config)
        type_ = config.pop('type')
        packages: Optional[List[str]] = config.pop('packages', None)
        args = config
        return RepositoryInstanceConfig(type_, packages, name, args)


class _CompositeRepository(AbstractRepository):
    def __init__(
            self, name: str, url_handlers: Dict[str, Repository], package_search_list: List[Repository],
            package_associated_repos: Dict[str, Repository]):
        super().__init__(name)

        self._url_handlers = url_handlers
        self._package_search_list = package_search_list
        self._package_associated_repos = package_associated_repos

    def _do_match(self, dependency: Dependency) -> List[Package]:
        if url := dependency.version_spec.specific_url():

            if protocol := url.protocol:
                if repo := self._url_handlers.get(url.protocol):
                    return repo.match(dependency, False)
                raise NoSuchElementException(f"could not find repository to handle url with protocol: {protocol}")
            return self._url_handlers['url'].match(dependency, False)

        if repo := self._package_associated_repos.get(dependency.package_name):
            return repo.match(dependency, False)

        for repo in self._package_search_list:
            if result := repo.match(dependency, False):
                return result

        return []

    def _sort_by_priority(self, dependency: Dependency, packages: List[Package]) -> List[Package]:
        return packages


class _ProjectsRepository(AbstractRepository):
    def __init__(self, name: str, projects: Dict[str, Tuple[PackageDescriptor, Path]], base_repo: Repository):
        super().__init__(name)
        self._packages = projects
        self._base_repo = base_repo

    def _do_match(self, dependency: Dependency) -> List[Package]:
        if (package_and_path := self._packages.get(dependency.package_name)) and \
                dependency.version_spec.allows_version(package_and_path[0].version):
            return [Project.load(package_and_path[1])]
        return self._base_repo.match(dependency, False)

    def _sort_by_priority(self, dependency: Dependency, packages: List[Package]) -> List[Package]:
        return packages

    @classmethod
    def create(cls, name: str, projects: Iterable[Project], base: Repository) -> _ProjectsRepository:
        return _ProjectsRepository(name, {p.name: (p.descriptor, p.path) for p in projects}, base)
