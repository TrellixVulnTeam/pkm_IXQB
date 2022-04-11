from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Optional, Union, TYPE_CHECKING, Dict, Callable

from pkm.api.distributions.distinfo import DistInfo
from pkm.api.environments.environment_builder import EnvironmentBuilder
from pkm.api.packages.package import Package, PackageDescriptor
from pkm.api.packages.package_installation import PackageInstallationTarget
from pkm.api.packages.package_metadata import PackageMetadata
from pkm.api.packages.package_monitors import PackageInstallMonitoredOp
from pkm.api.pkm import pkm
from pkm.api.projects.environments_config import EnvironmentsConfiguration, ENVIRONMENT_CONFIGURATION_PATH, \
    AttachedEnvironmentConfig
from pkm.api.projects.pyproject_configuration import PyProjectConfiguration, PkmDistributionConfig, \
    PKM_DIST_CFG_TYPE_LIB, PkmApplicationConfig, PKM_DIST_CFG_TYPE_CAPP
from pkm.api.repositories.repository import Repository, RepositoryPublisher, Authentication
from pkm.api.versions.version import StandardVersion, Version, NamedVersion
from pkm.api.versions.version_specifiers import VersionRange, SpecificVersion
from pkm.resolution.packages_lock import PackagesLock
from pkm.utils.commons import UnsupportedOperationException
from pkm.utils.files import temp_dir
from pkm.utils.properties import cached_property, clear_cached_properties

if TYPE_CHECKING:
    from pkm.api.projects.project_group import ProjectGroup
    from pkm.api.environments.environment import Environment
    from pkm.api.dependencies.dependency import Dependency


class Project(Package):

    def __init__(self, pyproject: PyProjectConfiguration, group: Optional["ProjectGroup"] = None):
        self._path = pyproject.path.absolute().parent
        self._pyproject = pyproject
        self._descriptor = pyproject.project.package_descriptor()
        if group:
            self.group = group  # noqa

    @property
    def config(self) -> PyProjectConfiguration:
        """
        :return: the project configuration (a.k.a., pyproject.toml)
        """
        return self._pyproject

    @cached_property
    def group(self) -> Optional["ProjectGroup"]:
        """
        :return: the project group if it belongs to such, otherwise None
        """
        from pkm.api.projects.project_group import ProjectGroup
        return ProjectGroup.of(self)

    @cached_property
    def environments_config(self) -> EnvironmentsConfiguration:
        """
        :return: the environments.toml configuration (etc/pkm/environments.toml)
        """
        return EnvironmentsConfiguration.load(self.path / ENVIRONMENT_CONFIGURATION_PATH)

    @property
    def path(self) -> Path:
        """
        :return: the path to the project root (where the pyproject.toml is located) or
                 None if this project was not loaded from a path
        """
        return self._path

    @cached_property
    def published_metadata(self) -> Optional[PackageMetadata]:
        return PackageMetadata.from_project_config(self.config.project)

    @cached_property
    def computed_metadata(self) -> PackageMetadata:
        if self.config.project.dynamic:
            dist_info = DistInfo.load(self.build_wheel(only_meta=True))
            return dist_info.load_metadata_cfg()
        return self.published_metadata

    @property
    def descriptor(self) -> PackageDescriptor:
        return self._descriptor

    def _all_dependencies(self, environment: "Environment") -> List["Dependency"]:
        prj = self.config.project

        if prj.is_dynamic('dependencies') or prj.is_dynamic('optional-dependencies'):
            return self.computed_metadata.dependencies

        return self._pyproject.project.all_dependencies

    def is_compatible_with(self, env: "Environment") -> bool:
        return self._pyproject.project.requires_python.allows_version(env.interpreter_version)

    def install_to(
            self, target: PackageInstallationTarget, user_request: Optional["Dependency"] = None,
            editable: bool = True):

        from pkm.api.distributions.wheel_distribution import WheelDistribution
        with temp_dir() as tdir, PackageInstallMonitoredOp(self.descriptor):
            wheel = self.build_wheel(tdir, editable=editable, target_env=target.env)
            distribution = WheelDistribution(self.descriptor, wheel)
            distribution.install_to(target, user_request)

    def update_at(self, target: "PackageInstallationTarget", editable: bool = True):
        # fast alternative for application to be installed without passing through wheels
        if self.is_containerized_application():
            target.app_containers.install(self, editable)
        else:
            super(Project, self).update_at(target)

    @cached_property
    def lock(self) -> PackagesLock:
        """
        :return: the project lock, read more about it in `PackagesLock` documentation
        """
        return PackagesLock.load(self.directories.etc_pkm / 'packages-lock.toml')

    @cached_property
    def directories(self) -> "ProjectDirectories":
        """
        :return: common project directories
        """
        return ProjectDirectories.create(self._pyproject)

    def bump_version(self, particle: str, new_name: Optional[str] = None, save: bool = True) -> Version:
        """
        bump up the version of this project
        :param particle: the particle of the version to bump, can be any of: major, minor, patch, a, b, rc, name
        :param new_name: if `particle` equals to 'name' than the new name is taken from this argument
        :param save: if true then the new configuration is saved into pyproject.toml
        :return: the new version after the bump
        """

        if particle == 'name':
            if not new_name:
                raise UnsupportedOperationException("particle was 'name' but no name was provided")

            new_version = NamedVersion(new_name)
        else:
            version: Version = self.config.project.version
            if not isinstance(version, StandardVersion) or not len(version.release) == 3:
                raise UnsupportedOperationException("cannot bump version that does not follow the semver semantics")

            new_version = version.bump(particle)

        self.config.project = replace(self.config.project, version=new_version)
        if save:
            self.config.save()
        return new_version

    def remove_dependencies(self, packages: List[str]):
        """
        remove and uninstall all dependencies that are related to the given list of packages
        :param packages: the list of package names to remove
        """

        package_names_set = set(packages)
        project_dependencies = self._pyproject.project.dependencies or []
        self._pyproject.project = replace(
            self._pyproject.project,
            dependencies=[d for d in project_dependencies if d.package_name not in package_names_set])
        self._pyproject.save()

        # fix installation metadata of the project by reinstalling it (without dependencies)
        self.update_at(self.attached_environment.installation_target)
        self.attached_environment.uninstall(packages)

        self.lock.update_lock(self.attached_environment)
        self.lock.save()

    def install_with_dependencies(
            self, new_dependencies: Optional[List["Dependency"]] = None,
            optional_group: Optional[str] = None):
        """
        install the dependencies of this project to its assigned environments
        :param new_dependencies: if given, resolve and add these dependencies to this project and then install
        :param optional_group: if not None, installs the dependencies including the ones from the given group,
                               also, mark the newly installed dependencies as optional and add them to that group
        """

        deps = {d.package_name: d for d in (self._pyproject.project.dependencies or [])}
        if optional_group:
            deps.update(
                {d.package_name: d for d in (self._pyproject.project.optional_dependencies.get(optional_group) or [])})

        new_deps: Dict[str, "Dependency"] = {d.package_name: d for d in new_dependencies} if new_dependencies else {}

        # save the new dependencies to the configuration:
        _update_dependencies(self.config, new_deps, optional_group)

        repository = self.attached_repository
        # self.attached_environment.force_remove(self.name)
        project_dependency = self.descriptor.to_dependency()
        if optional_group:
            project_dependency = project_dependency.with_extras([optional_group])
        self.update_at(self.attached_environment.installation_target)  # should probably submit the optionals
        self.attached_environment.install(project_dependency, repository)

        new_deps_with_version = {}
        site_packages = self.attached_environment.site_packages
        for dep in new_deps.values():
            if not dep.version_spec.is_any():
                spec = dep.version_spec
            else:
                installed_package = site_packages.installed_package(dep.package_name)
                assert installed_package, \
                    f"package: {dep.package_name} could not be found in site packages after installation"

                installed_version = installed_package.version
                if isinstance(installed_version, StandardVersion):
                    spec = VersionRange(
                        installed_version,
                        replace(installed_version, release=(installed_version.release[0] + 1,)),
                        True, False)
                else:
                    spec = SpecificVersion(installed_version)

            new_deps_with_version[dep.package_name] = replace(dep, version_spec=spec)

        _update_dependencies(self.config, new_deps_with_version, optional_group)
        self.lock.update_lock(self.attached_environment)
        self.lock.save()

    def _reload(self):
        clear_cached_properties(self)

    @cached_property
    def attached_environment(self) -> "Environment":
        """
        :return: the virtual environment that is attached to this project
        """

        cfg = self.environments_config.attached_env
        if not cfg and self.group:
            cfg = self.group.environments_config.attached_env

        cfg = cfg or AttachedEnvironmentConfig()

        if not cfg.path and not cfg.zoo:
            env_path = self.path / ".venv"
        elif cfg.path:
            env_path = cfg.path
        else:
            env_path = cfg.zoo / self.name

        from pkm.api.environments.environment import Environment
        from pkm.api.dependencies.dependency import Dependency

        if not Environment.is_valid(env_path):
            return EnvironmentBuilder.create_matching(
                env_path, Dependency('python', self.config.project.requires_python))
        return Environment(env_path)

    @cached_property
    def attached_repository(self) -> "Repository":
        """
        :return: the repository that is attached to this project (will be used with its attached environment)
        """
        return pkm.repository_loader.load_for_project(self)

    def build_app_sdist(self, target_dir: Optional[Path] = None) -> Path:
        """
        build a containerized application source distribution from this project
        :param target_dir: the directory to put the created archive in
        :return: the path to the created archive
        """
        cnt_app_prj = Project.load(self.path)
        cnt_app_prj.config.pkm_distribution = PkmDistributionConfig(PKM_DIST_CFG_TYPE_LIB)
        cnt_app_prj.config.pkm_application = PkmApplicationConfig(True, self.config.project.dependencies, {}, [])
        cnt_app_prj.config.project = replace(cnt_app_prj.config.project, dependencies=[])
        return cnt_app_prj.build_sdist(target_dir)

    def build_sdist(self, target_dir: Optional[Path] = None) -> Path:
        """
        build a source distribution from this project
        :param target_dir: the directory to put the created archive in
        :return: the path to the created archive
        """

        if self.is_pkm_project():
            from pkm.pep517_builders.pkm_builders import build_sdist
            return build_sdist(self, target_dir)
        else:
            from pkm.pep517_builders.external_builders import build_sdist
            return build_sdist(self, target_dir)

    def build_wheel(self, target_dir: Optional[Path] = None, only_meta: bool = False, editable: bool = False,
                    target_env: Optional[Environment] = None) -> Path:
        """
        build a wheel distribution from this project
        :param target_dir: directory to put the resulted wheel in
        :param only_meta: if True, only builds the dist-info directory otherwise the whole wheel
        :param editable: if True, a wheel for editable install will be created
        :param target_env: the environment that this build should be compatible with, defaults to attached env
        :return: path to the built artifact (directory if only_meta, wheel archive otherwise)
        """
        if self.is_containerized_application() and not only_meta:
            from pkm.pep517_builders.pkm_app_builders import build_wheel
            return build_wheel(self, target_dir, editable=editable, target_env=target_env)
        elif self.is_pkm_project():
            from pkm.pep517_builders.pkm_builders import build_wheel
            return build_wheel(self, target_dir, only_meta, editable, target_env=target_env)
        else:
            from pkm.pep517_builders.external_builders import build_wheel
            return build_wheel(self, target_dir, only_meta, editable, target_env=target_env)

    def build(self, target_dir: Optional[Path] = None) -> List[Path]:
        """
        builds the project into all distributions that are required as part of its configuration
        :param target_dir: directory to put the resulted distributions in
        :return list of paths to all the distributions created
        """

        builders: List[Callable[[Path], Path]] = [self.build_sdist, self.build_wheel]
        if self.is_containerized_application():
            builders = [self.build_sdist]
        elif self.config.pkm_distribution.type == PKM_DIST_CFG_TYPE_CAPP:
            builders = [self.build_app_sdist]

        return [build(target_dir) for build in builders]

    def is_pkm_project(self) -> bool:
        """
        :return: True if this project is a pkm project, False otherwise
        """
        return self.config.build_system.build_backend == 'pkm.api.buildsys'

    def is_containerized_application(self) -> bool:
        """
        :return: true if this project represents a containerized application project
                 (the `tool.pkm.application` section exists)
        """
        return self._pyproject.pkm_application is not None

    def is_built_in_default_location(self) -> bool:
        """
        :return: True if the project default dist folder contain a build directory for the current version,
                 False otherwise
        """

        return (self.directories.dist / str(self.version)).exists()

    def publish(self, repository: Union[Repository, RepositoryPublisher], auth: Authentication,
                distributions_dir: Optional[Path] = None):
        """
        publish/register this project distributions, as found in the given `distributions_dir`
        to the given `repository`. using `auth` for authentication

        :param repository: the repository to publish to
        :param auth: authentication for this repository
        :param distributions_dir: directory containing the distributions (archives like wheels and sdists) to publish
        """

        distributions_dir = distributions_dir or (self.directories.dist / str(self.version))

        if not distributions_dir.exists():
            raise FileNotFoundError(f"{distributions_dir} does not exists")

        publisher = repository if isinstance(repository, RepositoryPublisher) else repository.publisher
        if not publisher:
            raise UnsupportedOperationException(f"the given repository ({repository.name}) is not publishable")

        metadata = PackageMetadata.from_project_config(self._pyproject.project)
        for distribution in distributions_dir.iterdir():
            if distribution.is_file():
                publisher.publish(auth, metadata, distribution)

    @classmethod
    def load(cls, path: Union[Path, str], package: Optional[PackageDescriptor] = None,
             group: Optional["ProjectGroup"] = None) -> "Project":
        path = Path(path)
        pyproject = PyProjectConfiguration.load_effective(path / 'pyproject.toml', package)
        return Project(pyproject, group=group)


@dataclass()
class ProjectDirectories:
    src_packages: List[Path]
    dist: Path
    etc_pkm: Path

    @classmethod
    def create(cls, pyproject: PyProjectConfiguration) -> "ProjectDirectories":
        project_path = pyproject.path.parent
        packages_relative = pyproject.pkm_project.packages
        if packages_relative:
            packages = [project_path / p for p in packages_relative]
        else:
            if not (src_dir := project_path / 'src').exists():
                src_dir = project_path
            packages = [p for p in src_dir.iterdir() if p.is_dir()]

        etc_pkm = project_path / 'etc' / 'pkm'
        etc_pkm.mkdir(parents=True, exist_ok=True)
        return ProjectDirectories(packages, project_path / 'dist', etc_pkm)


def _update_dependencies(
        config: PyProjectConfiguration, new_deps: Dict[str, "Dependency"], optional_group: Optional[str]):
    save_dependencies = [d for d in (config.project.dependencies or []) if d.package_name not in new_deps]
    save_optional_dependencies = config.project.optional_dependencies

    if optional_group:
        save_optional_dependencies[optional_group] = \
            [d
             for d in save_optional_dependencies.get(optional_group, [])
             if d.package_name not in new_deps] + list(new_deps.values())
    else:
        save_dependencies = save_dependencies + list(new_deps.values())

    config.project = replace(
        config.project,
        dependencies=save_dependencies, optional_dependencies=save_optional_dependencies)
    config.save()
