from dataclasses import dataclass

from pathlib import Path

from argparse import Namespace

from typing import Optional, Callable

from pkm.api.environments.environment import Environment
from pkm.api.projects.project import Project
from pkm.api.projects.project_group import ProjectGroup
from pkm.utils.commons import UnsupportedOperationException
from pkm_cli.display.display import Display


def _lookup_project_group(path: Path) -> Optional[ProjectGroup]:
    if (path / 'pyproject-group.toml').exists():
        return ProjectGroup.load(Path.cwd())


def _lookup_project(path: Path) -> Optional[Project]:
    if (path / 'pyproject.toml').exists():
        return Project.load(Path.cwd())


def _lookup_env(path: Path) -> Optional[Environment]:
    if (path / 'pyvenv.cfg').exists():
        return Environment(path)


@dataclass
class _ContextualCommand:
    on_project: Optional[Callable[[Project], None]] = None,
    on_project_group: Optional[Callable[[ProjectGroup], None]] = None,
    on_environment: Optional[Callable[[Environment], None]] = None,

    # noinspection PyCallingNonCallable
    def execute(self, path: Path):
        if (on_project := self.on_project) and (project := _lookup_project(path)):
            Display.print(f"using project context: {project.path}")
            on_project(project)
        elif (on_project_group := self.on_project_group) and (project_group := _lookup_project_group(path)):
            Display.print(f"using project-group context: {project_group.path}")
            on_project_group(project_group)
        elif (on_environment := self.on_environment) and (env := _lookup_env(path)):
            Display.print(f"using virtual-env context: {env.path}")
            on_environment(env)
        else:
            return False
        return True


class Context:
    def __init__(self, path: Path, lookup: bool, use_global: bool):
        self._path = path
        self._lookup = lookup
        self._use_global = use_global

    def run(self,
            on_project: Optional[Callable[[Project], None]] = None,
            on_project_group: Optional[Callable[[ProjectGroup], None]] = None,
            on_environment: Optional[Callable[[Environment], None]] = None,
            on_free_context: Optional[Callable[[], None]] = None,
            on_missing: Optional[Callable[[], None]] = None,
            **junk):

        if self._use_global:
            env = Environment.current()
            Display.print(f"using global virtual-env context: {env.path}")
            on_environment(env)
            return

        path = self._path
        cmd = _ContextualCommand(on_project=on_project, on_project_group=on_project_group,
                                 on_environment=on_environment)
        executed = cmd.execute(path)

        if not executed and self._lookup:
            for parent in path.parents:
                if executed := cmd.execute(parent):
                    break
        if not executed:
            if on_free_context:
                on_free_context()
            elif on_missing:
                on_missing()
            else:
                raise UnsupportedOperationException("could not execute operation")

    @classmethod
    def of(cls, args: Namespace):
        cwd = Path.cwd()
        if context := args.context:
            return cls(Path(context), False, False)
        elif args.global_context:
            return cls(cwd, False, True)
        else:
            return cls(cwd, True, False)