from html.parser import HTMLParser
from pathlib import Path
from typing import List, Union, Tuple, Dict, Optional, Callable

from pkm.api.dependencies.dependency import Dependency
from pkm.api.packages.package import Package, PackageDescriptor
from pkm.api.packages.standard_package import StandardPackageArtifact, AbstractPackage
from pkm.api.repositories.repository import Repository, RepositoryBuilder
from pkm.api.versions.version import Version
from pkm.api.versions.version_specifiers import VersionSpecifier
from pkm.utils.http.cache_directive import CacheDirective
from pkm.utils.http.http_client import HttpClient, Url
from pkm.utils.iterators import groupby
from pkm.utils.strings import endswith_any, without_suffix


class SimpleRepository(Repository):
    """
    implementation of pep503 simple repository
    """

    def __init__(self, name: str, http_client: HttpClient, url: str):
        super().__init__(name)
        self._http_client = http_client
        self._url = url
        self._base_url = Url.parse(url).connection_part()
        self._packages: Dict[str, Dict[str, Package]] = {}  # name -> version -> package

    def accepts(self, dependency: Dependency) -> bool:
        return not dependency.is_url_dependency

    def _do_match(self, dependency: Dependency) -> List[Package]:
        if not (version_to_package := self._packages.get(dependency.package_name)):
            data = self._http_client.fetch_resource(f"{self._url}/{dependency.package_name}").data
            extractor = _HtmlArtifactsExtractor(self._base_url)
            extractor.feed(data.read_text())

            all_artifacts = extractor.artifacts
            grouped_by_version: Dict[str, List[StandardPackageArtifact]] = groupby(
                all_artifacts, lambda a: _extract_version(a.file_name))

            version_to_package = {
                version_str: _SimplePackage(
                    self._http_client,
                    PackageDescriptor(dependency.package_name, Version.parse(version_str)),
                    version_artifacts)

                for version_str, version_artifacts in grouped_by_version.items()
            }
            self._packages[dependency.package_name] = version_to_package

        return [p for p in version_to_package.values() if dependency.version_spec.allows_version(p.version)]


_DISTRIBUTION_EXTENSIONS = (".whl", ".tar.gz", ".zip")


def _extract_version(filename: str) -> str:
    result = without_suffix(filename, suffix := endswith_any(filename, _DISTRIBUTION_EXTENSIONS))
    if suffix == '.whl':
        return result.split("-")[1]
    else:
        return result.split("-")[-1]


class _HtmlArtifactsExtractor(HTMLParser):

    def __init__(self, base_url: str, *, convert_charrefs: bool = ...) -> None:
        super().__init__(convert_charrefs=convert_charrefs)
        self._text_handler: Optional[Callable[[str], None]] = None
        self.artifacts: List[StandardPackageArtifact] = []
        self._base_url = base_url

    def handle_data(self, data: str) -> None:
        if self._text_handler:
            self._text_handler(data)

    def handle_endtag(self, tag: str) -> None:
        if self._text_handler:
            self._text_handler('')

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Union[str, None]]]) -> None:
        self._text_handler = \
            lambda txt: self.handle_element(tag, {kv[0]: kv[1] for kv in attrs if kv[1] is not None}, txt)

    def handle_element(self, tag: str, attrs: Dict[str, str], text: str):
        self._text_handler = None
        if tag == 'a' and 'href' in attrs:
            requires_python = None
            if requires_python_str := attrs.get('data-requires-python'):
                requires_python = VersionSpecifier.parse(requires_python_str)

            url = attrs.get('href')
            if not url.startswith('http'):
                url = f"{self._base_url}/{url.lstrip('/')}"

            if endswith_any(text, _DISTRIBUTION_EXTENSIONS):
                self.artifacts.append(StandardPackageArtifact(text, requires_python, {'url': url}))


class _SimplePackage(AbstractPackage):
    def __init__(self, http_client: HttpClient, descriptor: PackageDescriptor,
                 artifacts: List[StandardPackageArtifact]):
        super().__init__(descriptor, artifacts)
        self._http_client = http_client

    def _retrieve_artifact(self, artifact: StandardPackageArtifact) -> Path:
        return self._http_client.fetch_resource(artifact.other_info['url'], CacheDirective.allways()).data


class SimpleRepositoryBuilder(RepositoryBuilder):
    def __init__(self, http_client: HttpClient):
        super().__init__("simple")
        self._http_client = http_client

    def build(self, name: Optional[str], **kwargs) -> Repository:
        if not (url := kwargs.get('url')):
            raise KeyError("url field is required to build 'simple' repository")
        return SimpleRepository(name or url, self._http_client, str(url).rstrip('/'))
