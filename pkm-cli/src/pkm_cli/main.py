import argparse
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import List, Optional, Callable

from pkm.api.dependencies.dependency import Dependency
from pkm.api.environments.environment import Environment
from pkm.api.pkm import pkm
from pkm.api.projects.project import Project
from pkm.api.projects.project_group import ProjectGroup
from pkm.api.repositories.repository import Authentication
from pkm.utils.commons import UnsupportedOperationException
from pkm.utils.resources import ResourcePath
from pkm_cli import cli_monitors
from pkm_cli.context import Context
from pkm_cli.display.display import Display
from pkm_cli.reports.environment_report import EnvironmentReport
from pkm_cli.reports.package_report import PackageReport
from pkm_cli.reports.project_report import ProjectReport
from pkm_cli.scaffold.engine import ScaffoldingEngine


# noinspection PyUnusedLocal
def shell(args: Namespace):
    import xonsh.main

    def on_environment(env: Environment):
        Display.print(f"Using environment: {env.path}")
        with env.activate():
            sys.argv = ['xonsh']
            sys.exit(xonsh.main.main())

    def on_project(project: Project):
        on_environment(project.attached_environment)

    def on_free_context():
        on_environment(Environment.current())

    Context.of(args).run(**locals())


# noinspection PyUnusedLocal
def build(args: Namespace):
    def on_project(project: Project):
        project.build()

    def on_project_group(project_group: ProjectGroup):
        project_group.build_all()

    Context.of(args).run(**locals())


def vbump(args: Namespace):
    def on_project(project: Project):
        new_version = project.bump_version(args.particle)
        Display.print(f"Version bumped to: {new_version}")

    Context.of(args).run(**locals())


def install(args: Namespace):
    dependencies = [Dependency.parse_pep508(it) for it in args.dependencies]

    def on_project(project: Project):
        Display.print(f"Adding dependencies into project: {project.path}")
        project.install_with_dependencies(dependencies)

    def on_project_group(project_group: ProjectGroup):
        if dependencies:
            raise UnsupportedOperationException("could not install dependencies in project group")

        Display.print(f"Installing all projects in group")
        project_group.install_all()

    def on_environment(env: Environment):
        env.install(dependencies, pkm.repositories.main)

    Context.of(args).run(**locals())


def remove(args: Namespace):
    if not (package_names := args.package_names):
        raise ValueError("no package names are provided to be removed")

    def on_project(project: Project):
        Display.print(f"Removing packages from project: {project.path}")
        project.remove_dependencies(package_names)

    def on_environment(env: Environment):
        env.uninstall(package_names)

    Context.of(args).run(**locals())


def publish(args: Namespace):
    if not (uname := args.user):
        raise ValueError("missing user name")

    if not (password := args.password):
        raise ValueError("missing password")

    def on_project(project: Project):
        project.publish(pkm.repositories.pypi, Authentication(uname, password))

    def on_project_group(project_group: ProjectGroup):
        project_group.publish_all(pkm.repositories.pypi, Authentication(uname, password))

    Context.of(args).run(**locals())


def new(args: Namespace):
    ScaffoldingEngine().render(
        ResourcePath('pkm_cli.scaffold', Path(f"new_{args.template}.tar.gz")), Path.cwd(), args.template_args)


def show(args: Namespace):
    def on_project(project: Project):
        ProjectReport(project).display()

    def on_environment(env: Environment):
        EnvironmentReport(env).display()

    Context.of(args).run(**locals())


def show_package(args: Namespace):
    def on_project(project: Project):
        PackageReport(project, args.dependency).display()

    def on_environment(env: Environment):
        PackageReport(env, args.dependency).display()

    Context.of(args).run(**locals())


def main(args: Optional[List[str]] = None):
    args = args or sys.argv[1:]

    pkm_parser = ArgumentParser(description="pkm - python package management for busy developers")
    pkm_subparsers = pkm_parser.add_subparsers()
    all_subparsers = []

    def create_command(name: str, func: Callable[[Namespace], None],
                       subparsers=pkm_subparsers,
                       **defaults) -> ArgumentParser:
        result = subparsers.add_parser(name)
        result.set_defaults(func=func, **defaults)
        all_subparsers.append(result)

        return result

    # pkm build
    create_command('build', build)

    # pkm shell
    create_command('shell', shell)

    # pkm install
    pkm_install_parser = create_command('install', install)
    pkm_install_parser.add_argument('dependencies', nargs=argparse.REMAINDER)

    # pkm remove
    pkm_remove_parser = create_command('remove', remove)
    pkm_remove_parser.add_argument('package_names', nargs=argparse.REMAINDER)

    # pkm new
    pkm_new_parser = create_command('new', new)
    pkm_new_parser.add_argument('template')
    pkm_new_parser.add_argument('template_args', nargs=argparse.REMAINDER)

    # pkm publish
    pkm_publish_parser = create_command('publish', publish)
    pkm_publish_parser.add_argument('user')
    pkm_publish_parser.add_argument('password')

    # pkm vbump
    pkm_vbump_parser = create_command('vbump', vbump, particle='patch')
    pkm_vbump_parser.add_argument('particle', choices=['major', 'minor', 'patch', 'a', 'b', 'rc'], nargs='?')

    # pkm test
    pkm_show_parser = create_command('show', show)
    pkm_show_subparsers = pkm_show_parser.add_subparsers()
    pkm_show_package_parser = create_command('package', show_package, pkm_show_subparsers)
    pkm_show_package_parser.add_argument('dependency')

    # context altering flags
    for subparser in all_subparsers:
        subparser.add_argument('-v', '--verbose', action='store_true')
        subparser.add_argument('-c', '--context')
        subparser.add_argument('-g', '--global-context', action='store_true')

    pargs = pkm_parser.parse_args(args)
    cli_monitors.listen('verbose' in pargs and pargs.verbose)
    if 'func' in pargs:
        pargs.func(pargs)
    else:
        pkm_parser.print_help()


if __name__ == "__main__":
    main()
