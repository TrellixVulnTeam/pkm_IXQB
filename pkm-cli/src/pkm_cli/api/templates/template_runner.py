from __future__ import annotations

import importlib.util as iu
import shutil
from contextlib import contextmanager
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import List, Any, Optional, ContextManager, Set, Mapping

import questionary as q
from jinja2 import FileSystemLoader
from jinja2.sandbox import SandboxedEnvironment

from pkm.utils.commons import UnsupportedOperationException, IllegalStateException
from pkm.utils.files import temp_dir
from pkm.utils.properties import cached_property
from pkm_cli.display.display import Display
from pkm_cli.utils.method_clis import MethodCliArgs


class TemplateRunner:

    def __init__(self):
        self.jinja_context = SandboxedEnvironment(loader=FileSystemLoader("/"))
        self.user_interface = _UserInteface()

    @contextmanager
    def _load_template(self, name: str) -> ContextManager[_Template]:
        name = name.replace('-', '_')
        renderer_name = f"{name}.render"

        try:
            if template_spec := iu.find_spec(f"pkm_templates.{renderer_name}"):
                yield _Template(template_spec, self)
                return
        except ModuleNotFoundError:
            ...

        raise FileNotFoundError(f"No such template: {name}")

    def run(self, template_name: str, target_dir: Path, args: List[str], allow_overwrite: bool = False):
        with self._load_template(template_name) as template:
            template.execute(target_dir, MethodCliArgs.parse(args), allow_overwrite)

    def describe(self, template_name: str) -> str:
        with self._load_template(template_name) as template:
            return template.describe()


# noinspection PyMethodMayBeStatic
class _Template:

    def __init__(self, render_spec: ModuleSpec, runner: TemplateRunner):
        self._render_spec = render_spec
        self._runner = runner
        self._template_dir = Path(self._render_spec.origin).parent

    def describe(self) -> str:
        module = self._module
        return module.__doc__ or f"No Description Provided for template '{self._template_dir.name}'"

    def execute(self, target_dir: Path, cli_args: MethodCliArgs, allow_overwrite: bool = False):
        module = self._module
        module.target_dir = target_dir

        context = self._setup(cli_args, module)
        ignored_files = self._load_ignored_files(self._template_dir)
        if allow_overwrite:
            self._render(self._template_dir, target_dir, context, ignored_files)
        else:
            with temp_dir() as tdir:
                self._render(self._template_dir, tdir, context, ignored_files)
                for file in tdir.rglob("*"):
                    relative_file = file.relative_to(tdir)
                    if (target_file := target_dir / relative_file).exists():
                        raise IOError(f"file already exists: {target_file}")

                shutil.copytree(tdir, target_dir, dirs_exist_ok=True)
        if callable(post_render := getattr(module, "post_render", None)):
            post_render(context)

    def _load_ignored_files(self, template_root: Path) -> Set[Path]:
        result = {self._template_dir / file for file in ("render.py", "__pycache__", ".templateignore")}

        ignore_file = self._template_dir / ".templateignore"

        if ignore_file.exists():
            lines = ignore_file.read_text().splitlines()
            result = {f.resolve() for line in lines if line.strip() for f in template_root.glob(line)}

        for sub_dir in template_root.iterdir():
            if sub_dir.is_dir():
                result.update(self._load_ignored_files(sub_dir))

        return result

    def _render(self, template_dir: Path, target_dir: Path, context: Mapping, ignored_files: Set[Path]):

        jinja = self._runner.jinja_context

        for template_child in template_dir.iterdir():
            if template_child in ignored_files:
                continue

            name = jinja.from_string(template_child.name).render(context)

            if not name:  # empty names indicate unneeded files
                continue

            target_child = (target_dir / name).resolve()

            if not target_child.parent.exists():
                target_child.parent.mkdir(parents=True)

            if template_child.is_dir():
                if (template_child / ".templatepreserve").exists():
                    shutil.copytree(str(template_child.absolute()), str(target_child.absolute()))
                    return

                target_child.mkdir(exist_ok=True)
                self._render(template_child, target_child, context, ignored_files)
            elif target_child.suffix == ".tmpl":
                with target_child.with_suffix("").open("w") as f:
                    jinja.from_string(template_child.read_text()).stream(context).dump(f)
            else:
                shutil.copy(template_child, target_child)

    def _setup(self, cli_args: MethodCliArgs, module: ModuleType) -> Mapping:
        setup_function = module.setup
        result = cli_args.execute(setup_function)
        if not isinstance(result, Mapping):
            raise IllegalStateException(
                f"invalid return value from template setup function (dict is required, got {type(result)})")
        return result

    @cached_property
    def _module(self) -> ModuleType:
        ui = self._runner.user_interface
        module = iu.module_from_spec(self._render_spec)

        ui.install(module)
        self._render_spec.loader.exec_module(module)

        if not callable(getattr(module, 'setup', None)):
            raise UnsupportedOperationException(f"illegal template, no setup function defiend")

        return module


# noinspection PyMethodMayBeStatic
class _UserInteface:
    def print(self, msg: str):
        Display.print(msg)

    def confirm(self, prompt: str, default: bool = True) -> bool:
        r = q.confirm(prompt, default=default).ask()
        return r if isinstance(r, bool) else str(r).lower() in ('y', 'yes')

    def ask(self, prompt: str, default: Any = "", options: Optional[List[str]] = None,
            secret: bool = False, autocomplete: bool = False, multiselect: bool = False,
            path: bool = False):

        if options:
            options = list(options)  # ensure we have a list
            default = default or options[0]
            if multiselect:
                return q.checkbox(prompt, choices=options, default=default).ask()
            elif autocomplete:
                return q.autocomplete(prompt, choices=options, default=default).ask()
            else:
                return q.select(prompt, choices=options, default=default).ask()
        else:
            if secret:
                return q.password(prompt, default=default).ask()
            elif path:
                return q.path(prompt, default=default).ask()
            else:
                return q.text(prompt, default=default).ask()

    def install(self, module: ModuleType):
        module.ask = self.ask
        module.confirm = self.confirm
        module.print = self.print
