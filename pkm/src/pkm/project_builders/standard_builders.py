import shutil
import tarfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from shutil import ignore_patterns
from typing import Optional, ContextManager
from zipfile import ZipFile

from pkm.api.distributions.distinfo import WheelFileConfiguration, DistInfo
from pkm.api.distributions.pth_link import PthLink
from pkm.api.packages.package_metadata import PackageMetadata
from pkm.api.packages.package_monitors import HasBuildStepMonitor
from pkm.api.projects.project import ProjectDirectories, Project
from pkm.api.projects.pyproject_configuration import ProjectConfig, PyProjectConfiguration
from pkm.api.versions.version import StandardVersion
from pkm.utils.files import temp_dir
from pkm.utils.iterators import distinct
from pkm.utils.monitors import no_monitor


def build_sdist(project: Project, target_dir: Optional[Path] = None, *,
                monitor: HasBuildStepMonitor = no_monitor()) -> Path:
    """
    build a source distribution from this project
    :param project: the project to build
    :param target_dir: the directory to put the created archive in
    :param monitor: monitor the operations made by this method
    :return: the path to the created archive
    """

    with monitor.on_build(project.descriptor, 'sdist') as build_monitor:
        target_dir = target_dir or (project.directories.dist / str(project.version))
        target_dir.mkdir(parents=True, exist_ok=True)

        with _build_context(project) as bc:
            sdist_path = target_dir / bc.sdist_file_name()
            data_dir = bc.build_dir / sdist_path.name[:-len('.tar.gz')]
            data_dir.mkdir()

            dist_info_path = bc.build_dir / 'build.dist-info'
            bc.build_dist_info(dist_info_path)
            shutil.copy(dist_info_path / "METADATA", data_dir / "PKG-INFO")
            shutil.copy(bc.pyproject.path, data_dir / 'pyproject.toml')

            if bc.pyproject.pkm_project.packages:
                bc.copy_sources(data_dir)
            else:
                bc.copy_sources(data_dir / 'src')

            with tarfile.open(sdist_path, 'w:gz', format=tarfile.PAX_FORMAT) as sdist:
                for file in data_dir.glob('*'):
                    sdist.add(file, file.relative_to(bc.build_dir))

        return sdist_path


def build_wheel(project: Project, target_dir: Optional[Path] = None, only_meta: bool = False,
                editable: bool = False, *, monitor: HasBuildStepMonitor = no_monitor()) -> Path:
    """
    build a wheel distribution from this project
    :param project: the project to build
    :param target_dir: directory to put the resulted wheel in
    :param only_meta: if True, only builds the dist-info directory otherwise the whole wheel
    :param editable: if True, a wheel for editable install will be created
    :param monitor: monitor the operations made by this method
    :return: path to the built artifact (directory if only_meta, wheel archive otherwise)
    """

    requested_artifact = 'metadata' if only_meta else 'editable' if editable else 'wheel'
    with monitor.on_build(project.descriptor, requested_artifact) as build_monitor:
        target_dir = target_dir or (project.directories.dist / str(project.version))

        with _build_context(project) as bc:
            if only_meta:
                dist_info_path = target_dir / bc.dist_info_dir_name()
                bc.build_dist_info(dist_info_path)
                return dist_info_path

            dist_info_path = bc.build_dir / bc.dist_info_dir_name()
            dist_info = bc.build_dist_info(dist_info_path)
            bc.copy_sources(bc.build_dir, editable)
            records_file = dist_info.load_record_cfg()
            records_file.sign(bc.build_dir)
            records_file.save()

            wheel_path = target_dir / bc.wheel_file_name()
            target_dir.mkdir(parents=True, exist_ok=True)
            with ZipFile(wheel_path, 'w', compression=zipfile.ZIP_DEFLATED) as wheel:
                for file in bc.build_dir.rglob('*'):
                    wheel.write(file, file.relative_to(bc.build_dir))

        return wheel_path


@contextmanager
def _build_context(project: Project) -> ContextManager["_BuildContext"]:
    project_cfg: ProjectConfig = project.config.project

    project_name_underscores = project_cfg.name.replace('-', '_')

    with temp_dir() as build_dir:
        yield _BuildContext(project.config, build_dir, project_name_underscores)


@dataclass
class _BuildContext:
    pyproject: PyProjectConfiguration
    build_dir: Path
    project_name_underscore: str

    def _project_and_version_file_prefix(self):
        return f"{self.project_name_underscore}-{self.pyproject.project.version}"

    def wheel_file_name(self) -> str:
        project_cfg = self.pyproject.project
        min_interpreter: StandardVersion = project_cfg.requires_python.min.version \
            if project_cfg.requires_python else StandardVersion((3,))

        req_interpreter = 'py' + ''.join(str(it) for it in min_interpreter.release[:2])
        return f"{self._project_and_version_file_prefix()}-{req_interpreter}-none-any.whl"

    def sdist_file_name(self) -> str:
        return f"{self._project_and_version_file_prefix()}.tar.gz"

    def dist_info_dir_name(self) -> str:
        return f'{self._project_and_version_file_prefix()}.dist-info'

    def build_dist_info(self, dst: Path) -> DistInfo:
        di = DistInfo.load(dst)

        dst.mkdir(exist_ok=True, parents=True)
        project_config: ProjectConfig = self.pyproject.project

        PackageMetadata.from_project_config(project_config).save_to(di.metadata_path())
        di.license_path().write_text(
            project_config.license_content())

        # TODO: probably later we will want to add the version of pkm in the generator..
        WheelFileConfiguration.create(generator="pkm", purelib=True).save_to(di.wheel_path())

        entrypoints = di.load_entrypoints_cfg()
        entrypoints.entrypoints = [e for entries in self.pyproject.project.entry_points.values() for e in entries]
        entrypoints.save()

        return di

    def copy_sources(self, dst: Path, link_only: bool = False):
        dirs = ProjectDirectories.create(self.pyproject)

        if link_only:
            PthLink(
                dst / f"{self._project_and_version_file_prefix()}.pth",
                links=list(distinct(p.absolute().parent for p in dirs.src_packages))
            ).save()
            return

        for package_dir in dirs.src_packages:
            destination = dst / package_dir.name
            if package_dir.exists():
                shutil.copytree(package_dir, destination, ignore=ignore_patterns('__pycache__'))
            else:
                raise FileNotFoundError(f"the package {package_dir}, which is specified in pyproject.toml"
                                        " has no corresponding directory in project")