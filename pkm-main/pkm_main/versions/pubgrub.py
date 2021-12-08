from abc import ABC, abstractmethod
from collections import defaultdict
from itertools import chain
from typing import List, Dict, Iterable, Tuple, Optional, cast, DefaultDict, Union, Any

from dataclasses import dataclass
from pkm.utils.sequences import argmax
from pkm.api.versions.version import Version
from pkm.api.versions.version_specifiers import VersionSpecifier, VersionUnion, VersionRange, SpecificVersion, \
    AnyVersion


class UnsolvableProblemException(Exception):
    def __init__(self, incompatability: "Incompatability"):
        super().__init__(f'Dependency Versions Resolution Failed\n{incompatability.report()}')
        self.incompatability = incompatability


@dataclass(frozen=True)
class Term:
    package: str
    constraint: VersionSpecifier
    optional: bool = False

    def negate(self) -> "Term":
        return Term(self.package, self.constraint.inverse(), not self.optional)

    def intersect(self, other: "Term") -> "Term":
        assert self.package == other.package, 'cannot intesect terms of different packages'
        return Term(self.package, self.constraint.intersect(other.constraint))

    def satisfies(self, constraint: VersionSpecifier) -> bool:
        """self ⊆ term"""
        return (self.optional and constraint.is_none()) or constraint.allows_all(self.constraint)

    @classmethod
    def join(cls, package: str, terms: Iterable["Term"]) -> "Term":
        terms_iter = iter(terms)
        first_term = next(terms_iter, None)

        if not first_term:
            return Term.create(package, "*")

        next_term = next(terms_iter, None)
        if not next_term:
            return first_term

        package = first_term.package
        constraint = first_term.constraint

        while next_term is not None:
            constraint = constraint.intersect(next_term.constraint)
            next_term = next(terms_iter, None)

        return cls(package, constraint)

    def __repr__(self):
        return f"[{self.package} {self.constraint}]"

    @classmethod
    def create(cls, package_name: str, constraint: str) -> "Term":
        return cls(package_name, VersionSpecifier.parse(constraint))


@dataclass
class Assignment:
    term: Term
    decision_level: int
    order_index: int
    cause: Optional["Incompatability"]
    accumulated: VersionSpecifier

    def is_decision(self) -> bool:
        return self.cause is None

    def __repr__(self):
        return f"A({self.term}, dlevel={self.decision_level}, cause={self.cause})"


class PartialSolution:
    def __init__(self, root_package: str = 'root'):
        self._root_package = root_package
        self._assignments_by_order: List[Assignment] = []
        self.assignments_by_package: DefaultDict[str, List[Assignment]] = defaultdict(list)
        self._required_packages: Dict[str, int] = {}
        self._decisions: Dict[str, Assignment] = {}

    def undecided_packages(self) -> List[str]:
        return [req for req in self._required_packages if req not in self._decisions]

    @property
    def _decision_level(self) -> int:
        return max(0, len(self._decisions) - 1)

    def requiering_decision(self, package: str):
        decision_level = self._required_packages[package]
        return next(acc for acc in self._decisions.values()
                    if acc.decision_level == decision_level)

    def backtrack(self, decision_level: int):
        print(f"backtrack to decision_level: {decision_level}")

        def filtered(lst: List[Assignment]) -> Iterable[Assignment]:
            return (ass for ass in lst if ass.decision_level <= decision_level)

        self._assignments_by_order = list(filtered(self._assignments_by_order))

        assignments_by_package = defaultdict(list)
        for package, assignments in self.assignments_by_package.items():
            assignments_by_package[package].extend(filtered(assignments))

        self.assignments_by_package = assignments_by_package

        self._required_packages = {
            package: req_decision_level
            for package, req_decision_level in self._required_packages.items()
            if req_decision_level <= decision_level
        }

        self._decisions = {
            package: decision
            for package, decision in self._decisions.items()
            if decision.decision_level <= decision_level
        }

    def require(self, packages: Iterable[str]):
        required_packages = self._required_packages
        for package in packages:
            if package not in required_packages:
                required_packages[package] = self._decision_level

    def requires(self, package: str) -> bool:
        return package in self._required_packages

    def make_assignment(self, assignment_value: Term, cause: Optional["Incompatability"] = None):
        package_assignments = self.assignments_by_package[assignment_value.package]

        prev_ass = package_assignments[-1].term if package_assignments \
            else Term.create(assignment_value.package, '*')

        dlevel = self._decision_level

        if cause is None and assignment_value.package != self._root_package:
            dlevel += 1

        # print(f'prev ass: {prev_ass.constraint}, new: {assignment_value.constraint}, intersection: {prev_ass.constraint.intersect(assignment_value.constraint)}')

        return Assignment(assignment_value, dlevel, len(self._assignments_by_order),
                          cause, prev_ass.constraint.intersect(assignment_value.constraint))

    def assign(self, assignment_value: Union[Term, Assignment], cause: Optional["Incompatability"] = None):
        assignment = self.make_assignment(assignment_value, cause) \
            if not isinstance(assignment_value, Assignment) else assignment_value

        self.assignments_by_package[assignment.term.package].append(assignment)
        self._assignments_by_order.append(assignment)

        if assignment.is_decision():
            print(f"decided: {assignment}")
            self._decisions[assignment.term.package] = assignment
            print(f'entering decision level: {self._decision_level}')
        else:
            print(f"derrived: {assignment}")

    def __repr__(self):
        return f"PartialSolution({self._assignments_by_order})"

    def decisions(self) -> Dict[str, Version]:
        return {package: cast(SpecificVersion, ass.term.constraint).version for package, ass in self._decisions.items()}


@dataclass
class IncompatabilitySatisfaction:
    incompatability: "Incompatability"
    satisfier: Optional[Assignment] = None
    prev_satisfier: Optional[Assignment] = None
    undecided_term: Optional[Term] = None

    def is_full(self) -> bool:
        return bool(self.satisfier)

    def is_almost_full(self) -> bool:
        return bool(self.undecided_term)


@dataclass
class Incompatability:
    terms: Tuple[Term, ...]
    interlal_cause: Optional[Tuple["Incompatability", "Incompatability"]]
    external_cause: Optional[str]
    added: bool = False

    def is_simple(self):
        if self.interlal_cause:
            ic1, ic2 = self.interlal_cause
            return ic1.external_cause and ic2.external_cause

        return False

    def term_for(self, package: str) -> Optional[Term]:
        return next((term for term in self.terms if term.package == package), None)

    def check_satisfaction(self, solution: "PartialSolution") -> IncompatabilitySatisfaction:
        assignments = solution.assignments_by_package
        undecided_term: Optional[Term] = None
        satisfiers: List[Assignment] = []

        for term in self.terms:
            satisfier: Optional[Assignment] = None

            for assignment in assignments[term.package]:
                acc = assignment.accumulated

                if acc.is_none() and not term.optional:
                    return IncompatabilitySatisfaction(self)

                if term.constraint.allows_all(acc):
                    satisfier = assignment
                    break

                if not acc.allows_any(term.constraint):
                    return IncompatabilitySatisfaction(self)

            if not satisfier:
                if not undecided_term:
                    undecided_term = term
                else:
                    return IncompatabilitySatisfaction(self)
            else:
                satisfiers.append(satisfier)

        if undecided_term:
            return IncompatabilitySatisfaction(self, undecided_term=undecided_term)

        satisfier_index = argmax(satisfiers, key=lambda s: s.order_index)
        satisfier = satisfiers.pop(satisfier_index)

        for term in self.terms:
            if solution.requires(term.package):
                satisfiers.append(solution.requiering_decision(term.package))
            if term.package == satisfier.term.package:
                for assignment in assignments[term.package]:
                    if assignment is satisfier:
                        break

                    if term.constraint.allows_all(
                            assignment.accumulated.intersect(satisfier.term.constraint)):
                        satisfiers.append(assignment)
                        break

        prev_satisfier_index = argmax(satisfiers, key=lambda s: s.order_index) if satisfiers else None

        return IncompatabilitySatisfaction(
            self, satisfier=satisfier,
            prev_satisfier=satisfiers[prev_satisfier_index] if prev_satisfier_index is not None else None)

    def is_empty(self) -> bool:
        return not self.terms

    def __eq__(self, o: object) -> bool:
        return isinstance(o, Incompatability) and o.terms == self.terms

    def __hash__(self):
        return hash(self.terms)

    def __str__(self):
        if self.external_cause:
            return self.external_cause

        terms = self.terms
        if len(terms) == 1:
            if terms[0].constraint.is_any():
                return f"{terms[0].package} cannot be resolved"
            return f"{terms[0].package} must be {terms[0].constraint.inverse()}"
        elif len(terms) == 2:
            sterms = sorted(terms, key=lambda x: bool(isinstance(x.constraint, VersionUnion)))
            return f"{sterms[0]} requires that {sterms[1].negate()}"

        return f"{terms}"

    def __repr__(self):

        if self.external_cause:
            return f"EXI'{str(self)}'"
        else:
            return f"INI'{str(self)}'"

    def report(self):
        report: Dict[Any, Tuple[int, str]] = {}

        def write(key: Any, text: str):
            report[key] = len(report), text

        def generate(incompatability: Incompatability):
            if incompatability.external_cause:
                write(incompatability, incompatability.external_cause)
                return

            ic1, ic2 = incompatability.interlal_cause
            if ic1.interlal_cause and ic2.interlal_cause:
                l1, l2 = report.get(ic1), report.get(ic2)

                if l1 and l2:
                    write(incompatability, f"Because {ic1} ({l1[0]}) and {ic2} ({l2[0]}), {incompatability}")
                elif l1:
                    generate(ic2)
                    write(incompatability, f"And because {ic1} ({l1[0]}), {incompatability}")
                elif l2:
                    generate(ic1)
                    write(incompatability, f"And because {ic2} ({l2[1]}), {incompatability}")
                elif ic1.is_simple():
                    generate(ic2)
                    generate(ic1)
                    write(incompatability, f"Thus, {incompatability}")
                elif ic2.is_simple():
                    generate(ic1)
                    generate(ic2)
                    write(incompatability, f"Thus, {incompatability}")
                else:
                    generate(ic1)
                    write(len(report), '')
                    generate(ic2)
                    write(len(report), '')
                    write(incompatability,
                          f"So, because {ic1} ({report[ic1][0]}) and {ic2} ({report[ic2][0]}), {incompatability}")
            elif ic1.interlal_cause or ic2.interlal_cause:
                dr = ic1 if ic1.interlal_cause else ic2
                ex = ic1 if ic2 is dr else ic2

                dl, el = report.get(dr), report.get(ex)
                if dl:
                    write(incompatability, f"Because {ex} and {dr} ({dl[0]}), {incompatability}")
                elif bool(dr.interlal_cause[0].interlal_cause) ^ bool(dr.interlal_cause[1].interlal_cause):
                    prior_dr = dr.interlal_cause[0] if dr.interlal_cause[0].interlal_cause else dr.interlal_cause[1]
                    prior_ex = dr.interlal_cause[0] if prior_dr is dr.interlal_cause[1] else dr.interlal_cause[1]
                    generate(prior_dr)
                    write(incompatability, f"And because {prior_ex} and {ex}, {incompatability}")
                else:
                    generate(dr)
                    write(incompatability, f"And because {ex}, {incompatability}")
            else:
                write(incompatability, f"Because {ic1} and {ic2}, {incompatability}")

        generate(self)
        lines = sorted(report.values(), key=lambda x: x[0])
        return '\n'.join(f"{line}) {text}" for line, text in lines)

    @classmethod
    def create(cls, terms: Iterable[Term],
               internal_cause: Optional[Tuple["Incompatability", "Incompatability"]] = None,
               external_cause: Optional[str] = None) -> "Incompatability":

        grouped_terms: Dict[str, List[Term]] = defaultdict(list)
        for term in terms:
            grouped_terms[term.package].append(term)

        normalized_terms = [Term.join(package, terms)
                            for package, terms in grouped_terms.items()]

        sorted_terms = sorted(normalized_terms, key=lambda x: x.package)
        return cls(tuple(sorted_terms), internal_cause, external_cause)

    def update_dependency(self, new_depender: Term):
        assert len(self.terms) == 2, 'attempting to update terms for non dependency incompatability'

        updated_terms = []
        found = False
        for i, term in enumerate(self.terms):
            if term.package == new_depender.package:
                updated_terms.append(new_depender)
                found = True
                self.external_cause = f"{new_depender} depends on {self.terms[(i + 1) % 2].negate()}"
            else:
                updated_terms.append(term)

        assert found, "attempting to update dependency incompatability with a term that does not belongs to it"
        self.terms = tuple(sorted(updated_terms, key=lambda x: x.package))


@dataclass
class PackageVersion:
    term: Term
    generalized_constraint: Optional[VersionRange] = None
    dependencies: Optional[Dict[str, "PackageDependency"]] = None
    next: Optional["PackageVersion"] = None

    @property
    def version(self) -> Version:
        return cast(SpecificVersion, self.term.constraint).version

    def compute_incompatabilities(self) -> List[Incompatability]:
        result: List[Incompatability] = []
        for dependency in self.dependencies.values():
            last_requiring = self

            while last_requiring.next:
                nxt = last_requiring.next
                nxt_deps = nxt.dependencies
                if not nxt_deps: break

                nxt_dep = nxt_deps.get(dependency.term.package)
                if not nxt_dep or nxt_dep.term.constraint != dependency.term.constraint: break

                last_requiring = nxt

            if last_requiring is not self:
                incompatability = last_requiring.dependencies[dependency.term.package].incompatability
                if incompatability:
                    result.append(incompatability)
                    incompatability.update_dependency(Term(self.term.package, VersionRange(
                        min=self.generalized_constraint.min, max=last_requiring.generalized_constraint.max)))
                    continue

            nt = Term(self.term.package, self.generalized_constraint)
            dependency.incompatability = Incompatability.create(
                [nt, dependency.term.negate()], None, f'{nt} depends on {dependency.term}')
            result.append(dependency.incompatability)

        return result

    def __repr__(self):
        return f"V({self.term})"


@dataclass
class PackageDependency:
    term: Term
    incompatability: Optional[Incompatability] = None


class Problem(ABC):

    @abstractmethod
    def get_dependencies(self, package: str, version: Version) -> List[Term]:
        ...

    @abstractmethod
    def get_versions(self, package: str) -> List[Version]:
        ...


class Solver:

    def __init__(self, problem: Problem, root_package: str = 'root'):
        self._root_package = root_package
        self._problem = problem
        self._solution = PartialSolution(root_package)
        self._package_versions: Dict[str, List[PackageVersion]] = {}

        # incompatabilities by package name
        self._incompatabilities: DefaultDict[str, List[Incompatability]] = defaultdict(list)

    def _add_incompatability(self, incompatability: Incompatability):
        if incompatability.added:
            return
        incompatability.added = True

        print(f'adding incompatability: {incompatability}')

        for term in incompatability.terms:
            term_incompatabilities = self._incompatabilities[term.package]
            if incompatability not in term_incompatabilities:
                term_incompatabilities.append(incompatability)

    def solve(self) -> Dict[str, Version]:
        root_term = self.package_versions(self._root_package)[0].term
        self._add_incompatability(
            Incompatability.create([root_term.negate()], external_cause='Root Project'))

        self._solution.require([root_term.package])

        next_package = root_term.package

        while next_package is not None:
            print(f"trying to solve for {next_package}, already decided on: {self._solution.decisions()}")
            self._propagate(next_package)
            next_package = self._make_next_decision()

        return self._solution.decisions()

    def _propagate(self, next_package: str):

        print("#### unit propagation ####")

        changed = {next_package}

        while changed:
            print(f"enter unit propogation loop with changed = {changed}")
            package = changed.pop()

            for incompatability in reversed(self._incompatabilities[package]):
                satisfaction: IncompatabilitySatisfaction = incompatability.check_satisfaction(self._solution)
                if satisfaction.is_full():

                    print(f"incompatability: {incompatability} satisfied, entering conflict resolution")
                    new_incompatability = self._resolve_conflict(incompatability, satisfaction)
                    print(f"conflict resolution resulted with incompatability {new_incompatability}")
                    satisfaction = new_incompatability.check_satisfaction(self._solution)

                    assert satisfaction.is_almost_full(), \
                        "new incompatability resulted after conflict resolution is not almost satisfied"

                    term = satisfaction.undecided_term
                    self._solution.assign(term.negate(), new_incompatability)
                    changed = {term.package}
                elif satisfaction.is_almost_full():
                    term = satisfaction.undecided_term
                    print(f"incompatability {incompatability} is almost full, undecided_term is {term}")
                    self._solution.assign(term.negate(), incompatability)
                    changed.add(term.package)

    def _is_tautology(self, incompatability: Incompatability) -> bool:
        return not incompatability.terms or (
                len(incompatability.terms) == 1 and incompatability.terms[0].package == self._root_package)

    def _resolve_conflict(
            self, incompatability: Incompatability, satisfaction: IncompatabilitySatisfaction) -> Incompatability:

        print("#### conflict resolution ####")

        original_incompatability = incompatability
        while True:
            print(f"enter conflict resolution loop with {incompatability}")
            if self._is_tautology(incompatability):
                raise UnsolvableProblemException(incompatability)

            satisfier = satisfaction.satisfier
            term = incompatability.term_for(satisfier.term.package)

            prev_satisfier = satisfaction.prev_satisfier
            prev_satisfier_level = prev_satisfier.decision_level if prev_satisfier else 0

            print(f"satisfier: {satisfier}, prev_satisfier: {prev_satisfier}")

            if satisfier.is_decision() or prev_satisfier_level < satisfier.decision_level:
                self._solution.backtrack(prev_satisfier_level)
                if incompatability is not original_incompatability:
                    self._add_incompatability(incompatability)
                return incompatability

            prior_cause_terms = [
                term for term in chain(incompatability.terms, satisfier.cause.terms)
                if term.package != satisfier.term.package]

            if not satisfier.term.satisfies(term.constraint) or not prior_cause_terms:
                prior_cause_terms.append(
                    Term(satisfier.term.package,
                         satisfier.term.constraint.difference(term.constraint).inverse(),
                         satisfier.term.optional and term.optional)
                )

            incompatability = Incompatability.create(
                prior_cause_terms, internal_cause=(satisfier.cause, incompatability))

            print(f"root cause: {incompatability}")

            if not self._is_tautology(incompatability):
                satisfaction = incompatability.check_satisfaction(self._solution)

    def package_versions(self, package: str) -> List[PackageVersion]:
        versions = self._package_versions.get(package)
        if not versions:
            versions = self._package_versions[package] = [
                PackageVersion(Term(package, SpecificVersion(ver)))
                for ver in self._problem.get_versions(package)]

            sorted_versions = sorted(versions, key=lambda v: v.version)

            for i in range(len(versions) - 1):
                sorted_versions[i].next = sorted_versions[i + 1]

            if len(sorted_versions) == 1:
                sorted_versions[0].generalized_constraint = AnyVersion
            elif sorted_versions:
                for i in range(1, len(versions) - 1):
                    sorted_versions[i].generalized_constraint = VersionRange(
                        min=SpecificVersion(sorted_versions[i - 1].version),
                        max=SpecificVersion(sorted_versions[i + 1].version))
                sorted_versions[0].generalized_constraint = VersionRange(
                    max=SpecificVersion(sorted_versions[1].version))
                sorted_versions[-1].generalized_constraint = VersionRange(
                    min=SpecificVersion(sorted_versions[-1].version),
                    includes_min=True)

        return versions

    def _make_next_decision(self) -> Optional[str]:
        print("#### decision ####")
        undecided_packages = self._solution.undecided_packages()
        if not undecided_packages:
            return None

        print(f"undecided packages: {undecided_packages}")

        package_matching_versions: Dict[str, List[PackageVersion]] = {}
        for package in undecided_packages:
            acc_assignment = self._solution.assignments_by_package[package][-1].accumulated
            versions = self.package_versions(package)
            package_matching_versions[package] = [pver for pver in versions if
                                                  acc_assignment.allows_version(pver.version)]

        package = min(undecided_packages, key=lambda pack: len(package_matching_versions[pack]))

        print(f"choosing to try and assign {package} with constraint: {self._solution.assignments_by_package[package][-1].accumulated}")
        versions = package_matching_versions[package]
        if not versions:
            acc_assignment = self._solution.assignments_by_package[package][-1].accumulated
            print(f"could not find version that match {acc_assignment}")
            self._add_incompatability(
                Incompatability.create([Term(package, acc_assignment)],
                                       external_cause=f'No Versions matching {acc_assignment}'))
            return package

        version = versions[0]
        print(f"version: {version} match our term")

        if not version.dependencies:
            version.dependencies = {
                d.package: PackageDependency(d)
                for d in self._problem.get_dependencies(package, version.version)}

        incompatabilities = self._add_dependency_incompatabilities(version)

        assignment: Assignment = self._solution.make_assignment(Term(package, SpecificVersion(version.version)), None)
        assignments = self._solution.assignments_by_package
        assignments[package].append(assignment)
        print(f"checking if we can still assign {version} after the new incompatabilities: {incompatabilities}")
        conflict = any(ic.check_satisfaction(self._solution).is_full() for ic in incompatabilities)
        assignments[package].pop()

        if not conflict:
            print("we can!")
            self._solution.assign(assignment)
            self._solution.require(version.dependencies.keys())
        else:
            print("we cant..")

        return package

    def _add_dependency_incompatabilities(self, version: PackageVersion) -> List[Incompatability]:
        incompatabilities = version.compute_incompatabilities()
        for incompatability in incompatabilities:
            self._add_incompatability(incompatability)

        return incompatabilities