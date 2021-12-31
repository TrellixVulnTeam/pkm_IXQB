from dataclasses import dataclass
from pathlib import Path

import sys
import os

from pkm.api.repositories.pypi_repository import PyPiRepository
from pkm.api.repositories.source_builds_repository import SourceBuildsRepository
from pkm.utils.http.http_client import HttpClient

ENV_PKM_HOME = "PKM_HOME"


@dataclass
class _PkmRepositories:
    source_builds: SourceBuildsRepository
    pypi: PyPiRepository


class _Pkm:
    def __init__(self):
        self.workspace = workspace = os.environ.get(ENV_PKM_HOME) or _default_home_directory()
        workspace.mkdir(exist_ok=True, parents=True)
        self.httpclient = HttpClient(workspace / 'resources/http')
        self.repositories = _PkmRepositories(
            SourceBuildsRepository(workspace / 'source-builds'),
            PyPiRepository(self.httpclient)
        )


# the methods used for finding the default data directory were adapted from the appdirs library

def _get_win_folder() -> str:
    import ctypes

    csidl_const = 28  # "CSIDL_LOCAL_APPDATA"
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl_const, None, 0, buf)

    # Downgrade to short path name if have highbit chars. See
    # <http://bugs.activestate.com/show_bug.cgi?id=85099>.
    if any(ord(c) > 255 for c in buf):
        buf2 = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(buf.value, buf2, 1024):
            buf = buf2

    return buf.value


def _default_home_directory():
    system = sys.platform
    if system == "win32":
        path = Path(_get_win_folder())
    elif system == 'darwin':
        path = Path('~/Library/Application Support/')
    else:
        path = Path(os.getenv('XDG_DATA_HOME', os.path.expanduser("~/.local/share")))

    return (path / 'pkm').expanduser().resolve()


pkm = _Pkm()