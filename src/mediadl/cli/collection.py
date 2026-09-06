"""Collection query compilation independent of terminal rendering."""

from __future__ import annotations

from dataclasses import dataclass

from mediadl.core.errors import InputError
from mediadl.core.numbers import NumericPredicate, parse_count
from mediadl.index.enrichment import MetadataField
from mediadl.selection.filters import (
    FilterSpec,
    MediaFilterEngine,
    MediaSortEngine,
    SortMode,
    parse_duration_seconds,
    parse_filter_date,
)
from mediadl.selection.generic import (
    GenericFilterClause,
    apply_generic_filters,
    parse_generic_filter,
)
from mediadl.selection.ranges import SelectionEngine, SelectionMode, SelectionSpec
from mediadl.sources.models import MediaItemStub


@dataclass(frozen=True, slots=True)
class CollectionQuery:
    filter_spec: FilterSpec
    generic_filters: tuple[GenericFilterClause, ...]
    sort_mode: SortMode
    selection: SelectionSpec
    selection_label: str
    required_metadata: frozenset[MetadataField]

    def apply(
        self,
        items: tuple[MediaItemStub, ...] | list[MediaItemStub],
    ) -> tuple[MediaItemStub, ...]:
        filtered = MediaFilterEngine.apply(items, self.filter_spec)
        filtered = apply_generic_filters(filtered, self.generic_filters)
        ordered = MediaSortEngine.sort(filtered, self.sort_mode)
        return SelectionEngine.select(ordered, self.selection)

    def matches_item(self, item: MediaItemStub) -> bool:
        """Return whether one fully enriched item satisfies this query's filters."""

        filtered = MediaFilterEngine.apply((item,), self.filter_spec)
        if not filtered:
            return False
        return bool(apply_generic_filters(filtered, self.generic_filters))

    @property
    def source_date_order_eligible(self) -> bool:
        """Whether a newest-first source can satisfy date sorting without per-item dates."""

        if self.sort_mode not in {SortMode.LATEST, SortMode.OLDEST}:
            return False
        if (
            self.filter_spec.uploaded_after is not None
            or self.filter_spec.uploaded_before is not None
        ):
            return False
        return not any(
            clause.required_metadata is MetadataField.UPLOAD_DATE for clause in self.generic_filters
        )

    def apply_source_date_order(
        self,
        items: tuple[MediaItemStub, ...] | list[MediaItemStub],
    ) -> tuple[MediaItemStub, ...]:
        """Apply a query when the source itself is already newest-first."""

        if not self.source_date_order_eligible:
            raise InputError("Source-order date optimization is not valid for this query")
        filtered = MediaFilterEngine.apply(items, self.filter_spec)
        filtered = apply_generic_filters(filtered, self.generic_filters)
        ordered = filtered if self.sort_mode is SortMode.LATEST else tuple(reversed(filtered))
        return SelectionEngine.select(ordered, self.selection)

    @property
    def first_match_limit(self) -> int | None:
        """Return an early-stop target for source-ordered filtered first-N queries."""

        if self.sort_mode is not SortMode.SOURCE or self.selection.mode is not SelectionMode.FIRST:
            return None
        if self.filter_spec == FilterSpec() and not self.generic_filters:
            return None
        return self.selection.count

    @property
    def scan_limit(self) -> int | None:
        """Return a safe metadata-scan bound when the result cannot depend on unseen items."""

        if (
            self.sort_mode is not SortMode.SOURCE
            or self.filter_spec != FilterSpec()
            or self.generic_filters
        ):
            return None
        if self.selection.mode is SelectionMode.FIRST:
            return self.selection.count
        if self.selection.mode is SelectionMode.RANGE:
            return self.selection.end
        return None


@dataclass(frozen=True, slots=True)
class CollectionQueryInput:
    first: int | None = None
    last: int | None = None
    range_value: str | None = None
    latest: int | None = None
    oldest: int | None = None
    most_viewed: int | None = None
    least_viewed: int | None = None
    most_liked: int | None = None
    least_liked: int | None = None
    sort_mode: SortMode | None = None
    views_above: str | None = None
    views_below: str | None = None
    views_between: str | None = None
    likes_above: str | None = None
    likes_below: str | None = None
    likes_between: str | None = None
    after: str | None = None
    before: str | None = None
    title_contains: str | None = None
    duration_min: str | None = None
    duration_max: str | None = None
    where: tuple[str, ...] = ()


def compile_collection_query(values: CollectionQueryInput) -> CollectionQuery:
    views = _numeric_filter(
        "views",
        above=values.views_above,
        below=values.views_below,
        between=values.views_between,
    )
    likes = _numeric_filter(
        "likes",
        above=values.likes_above,
        below=values.likes_below,
        between=values.likes_between,
    )
    filter_spec = FilterSpec(
        views=views,
        likes=likes,
        duration_min_seconds=(
            parse_duration_seconds(values.duration_min) if values.duration_min is not None else None
        ),
        duration_max_seconds=(
            parse_duration_seconds(values.duration_max) if values.duration_max is not None else None
        ),
        uploaded_after=parse_filter_date(values.after) if values.after else None,
        uploaded_before=parse_filter_date(values.before) if values.before else None,
        title_contains=values.title_contains,
    )

    selection, implied_sort, label = _selection(values)
    sort_mode = values.sort_mode or implied_sort or SortMode.SOURCE
    if (
        implied_sort is not None
        and values.sort_mode is not None
        and values.sort_mode is not implied_sort
    ):
        raise InputError(
            f"Selection {label} already implies sort={implied_sort.value}; "
            "remove the conflicting --sort option"
        )

    generic_filters = tuple(parse_generic_filter(expression) for expression in values.where)
    required = _required_metadata(filter_spec, sort_mode)
    required.update(
        field
        for field in (clause.required_metadata for clause in generic_filters)
        if field is not None
    )
    return CollectionQuery(
        filter_spec=filter_spec,
        generic_filters=generic_filters,
        sort_mode=sort_mode,
        selection=selection,
        selection_label=label,
        required_metadata=frozenset(required),
    )


def _selection(values: CollectionQueryInput) -> tuple[SelectionSpec, SortMode | None, str]:
    choices: list[tuple[str, int | str | None]] = [
        ("first", values.first),
        ("last", values.last),
        ("range", values.range_value),
        ("latest", values.latest),
        ("oldest", values.oldest),
        ("most-viewed", values.most_viewed),
        ("least-viewed", values.least_viewed),
        ("most-liked", values.most_liked),
        ("least-liked", values.least_liked),
    ]
    active = [(name, value) for name, value in choices if value is not None]
    if len(active) > 1:
        names = ", ".join(name for name, _ in active)
        raise InputError(f"Choose only one collection selector; received: {names}")
    if not active:
        return SelectionSpec.all(), None, "All"

    name, raw = active[0]
    if name == "range":
        assert isinstance(raw, str)
        return SelectionSpec.parse_range(raw), None, f"Range {raw}"

    assert isinstance(raw, int)
    if name == "first":
        return SelectionSpec.first(raw), None, f"First {raw}"
    if name == "last":
        return SelectionSpec.last(raw), None, f"Last {raw}"
    if name == "latest":
        return SelectionSpec.first(raw), SortMode.LATEST, f"Latest {raw}"
    if name == "oldest":
        return SelectionSpec.first(raw), SortMode.OLDEST, f"Oldest {raw}"
    if name == "most-viewed":
        return SelectionSpec.first(raw), SortMode.MOST_VIEWED, f"Most viewed {raw}"
    if name == "least-viewed":
        return SelectionSpec.first(raw), SortMode.LEAST_VIEWED, f"Least viewed {raw}"
    if name == "most-liked":
        return SelectionSpec.first(raw), SortMode.MOST_LIKED, f"Most liked {raw}"
    if name == "least-liked":
        return SelectionSpec.first(raw), SortMode.LEAST_LIKED, f"Least liked {raw}"
    raise InputError(f"Unsupported collection selector: {name}")


def _numeric_filter(
    label: str,
    *,
    above: str | None,
    below: str | None,
    between: str | None,
) -> NumericPredicate | None:
    if between is not None:
        if above is not None or below is not None:
            raise InputError(
                f"Use either --{label}-between or --{label}-above/--{label}-below, not both"
            )
        return NumericPredicate.between(between)
    if above is None and below is None:
        return None
    lower = parse_count(above) if above is not None else None
    upper = parse_count(below) if below is not None else None
    if lower is not None and upper is not None and lower >= upper:
        raise InputError(f"{label.title()} lower threshold must be below upper threshold")
    return NumericPredicate(
        lower=lower,
        upper=upper,
        lower_inclusive=False,
        upper_inclusive=False,
    )


def _required_metadata(filter_spec: FilterSpec, sort_mode: SortMode) -> set[MetadataField]:
    required: set[MetadataField] = set()
    if filter_spec.views is not None or sort_mode in {SortMode.MOST_VIEWED, SortMode.LEAST_VIEWED}:
        required.add(MetadataField.VIEWS)
    if filter_spec.likes is not None or sort_mode in {SortMode.MOST_LIKED, SortMode.LEAST_LIKED}:
        required.add(MetadataField.LIKES)
    if (
        filter_spec.duration_min_seconds is not None
        or filter_spec.duration_max_seconds is not None
        or sort_mode in {SortMode.LONGEST, SortMode.SHORTEST}
    ):
        required.add(MetadataField.DURATION)
    if (
        filter_spec.uploaded_after is not None
        or filter_spec.uploaded_before is not None
        or sort_mode in {SortMode.LATEST, SortMode.OLDEST}
    ):
        required.add(MetadataField.UPLOAD_DATE)
    return required
