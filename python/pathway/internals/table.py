# Copyright © 2023 Pathway

from __future__ import annotations

import functools
import warnings
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast, overload

import pathway.internals.column as clmn
import pathway.internals.expression as expr
from pathway.internals import dtype as dt, groupby, thisclass, universes
from pathway.internals.arg_handlers import (
    arg_handler,
    groupby_handler,
    reduce_args_handler,
    select_args_handler,
)
from pathway.internals.column_properties import ColumnProperties
from pathway.internals.decorators import (
    contextualized_operator,
    empty_from_schema,
    table_to_datasink,
)
from pathway.internals.desugaring import (
    RestrictUniverseDesugaring,
    combine_args_kwargs,
    desugar,
)
from pathway.internals.expression_visitor import collect_tables
from pathway.internals.helpers import SetOnceProperty, StableSet
from pathway.internals.join import Joinable
from pathway.internals.operator import DebugOperator, OutputHandle
from pathway.internals.operator_input import OperatorInput
from pathway.internals.parse_graph import G
from pathway.internals.runtime_type_check import runtime_type_check
from pathway.internals.schema import Schema, schema_from_columns, schema_from_types
from pathway.internals.table_like import TableLike
from pathway.internals.table_slice import TableSlice
from pathway.internals.trace import trace_user_frame
from pathway.internals.type_interpreter import TypeInterpreterState
from pathway.internals.universe import Universe

if TYPE_CHECKING:
    from pathway.internals.datasink import DataSink

# To run doctests use
# pytest public/pathway/python/pathway/table.py  --doctest-modules
# .jenkins/bash_scripts/pytest.sh points to this file


TSchema = TypeVar("TSchema", bound=Schema)


class Table(
    Joinable,
    OperatorInput,
    Generic[TSchema],
):
    """Collection of named columns over identical universes.

    Example:

    >>> import pathway as pw
    >>> t1 = pw.debug.parse_to_table('''
    ... age | owner | pet
    ... 10  | Alice | dog
    ... 9   | Bob   | dog
    ... 8   | Alice | cat
    ... 7   | Bob   | dog
    ... ''')
    >>> isinstance(t1, pw.Table)
    True
    """

    if TYPE_CHECKING:
        from pathway.stdlib.indexing import sort  # type: ignore[misc]
        from pathway.stdlib.ordered import diff  # type: ignore[misc]
        from pathway.stdlib.statistical import interpolate  # type: ignore[misc]
        from pathway.stdlib.temporal import (  # type: ignore[misc]
            asof_join,
            asof_join_left,
            asof_join_outer,
            asof_join_right,
            interval_join,
            interval_join_inner,
            interval_join_left,
            interval_join_outer,
            interval_join_right,
            window_join,
            window_join_inner,
            window_join_left,
            window_join_outer,
            window_join_right,
            windowby,
        )

    _columns: dict[str, clmn.Column]
    _context: clmn.RowwiseContext
    _schema: type[Schema]
    _pk_columns: dict[str, clmn.Column]
    _id_column: clmn.IdColumn
    _source: SetOnceProperty[OutputHandle] = SetOnceProperty()
    """Lateinit by operator."""

    def __init__(
        self,
        columns: Mapping[str, clmn.Column],
        universe: Universe,
        pk_columns: Mapping[str, clmn.Column] = {},
        schema: type[Schema] | None = None,
        id_column: clmn.IdColumn | None = None,
    ):
        if schema is None:
            schema = schema_from_columns(columns)
        super().__init__(universe)
        self._columns = dict(columns)
        self._pk_columns = dict(pk_columns)
        self._schema = schema
        self._context = clmn.RowwiseContext(self._universe)
        self._id_column = id_column or clmn.IdColumn(self._context)
        self._substitution = {thisclass.this: self}

    @property
    def id(self) -> expr.ColumnReference:
        """Get reference to pseudocolumn containing id's of a table.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | dog
        ... 9   | Bob   | dog
        ... 8   | Alice | cat
        ... 7   | Bob   | dog
        ... ''')
        >>> t2 = t1.select(ids = t1.id)
        >>> t2.typehints()['ids']
        <class 'pathway.engine.Pointer'>
        >>> pw.debug.compute_and_print(t2.select(test=t2.id == t2.ids), include_id=False)
        test
        True
        True
        True
        True
        """
        return expr.ColumnReference(table=self, column=self._id_column, name="id")

    def column_names(self):
        return self.keys()

    def keys(self):
        return self._columns.keys()

    def _get_column(self, name: str) -> clmn.Column:
        return self._columns[name]

    def _ipython_key_completions_(self):
        return list(self.column_names())

    def __dir__(self):
        return list(super().__dir__()) + list(self.column_names())

    @property
    def schema(self) -> type[Schema]:
        """Get schema of the table.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | dog
        ... 9   | Bob   | dog
        ... 8   | Alice | cat
        ... 7   | Bob   | dog
        ... ''')
        >>> t1.schema
        <pathway.Schema types={'age': <class 'int'>, 'owner': <class 'str'>, 'pet': <class 'str'>}>
        >>> t1.typehints()['age']
        <class 'int'>
        """
        return self._schema

    def _get_colref_by_name(self, name, exception_type) -> expr.ColumnReference:
        if name == "id":
            return self.id
        if name not in self.keys():
            raise exception_type(f"Table has no column with name {name}.")
        return expr.ColumnReference(
            table=self, column=self._get_column(name), name=name
        )

    @overload
    def __getitem__(self, args: str | expr.ColumnReference) -> expr.ColumnReference:
        ...

    @overload
    def __getitem__(self, args: list[str | expr.ColumnReference]) -> Table:
        ...

    @trace_user_frame
    def __getitem__(
        self, args: str | expr.ColumnReference | list[str | expr.ColumnReference]
    ) -> expr.ColumnReference | Table:
        """Get columns by name.

        Warning:
            - Does not allow repetitions of columns.
            - Fails if tries to access nonexistent column.

        Args:
            names: a singe column name or list of columns names to be extracted from `self`.

        Returns:
            Table with specified columns, or column expression (if single argument given).
            Instead of column names, column references are valid here.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | dog
        ... 9   | Bob   | dog
        ... 8   | Alice | cat
        ... 7   | Bob   | dog
        ... ''')
        >>> t2 = t1[["age", "pet"]]
        >>> t2 = t1[["age", t1.pet]]
        >>> pw.debug.compute_and_print(t2, include_id=False)
        age | pet
        7   | dog
        8   | cat
        9   | dog
        10  | dog
        """
        if isinstance(args, expr.ColumnReference):
            if (args.table is not self) and not isinstance(
                args.table, thisclass.ThisMetaclass
            ):
                raise ValueError(
                    "Table.__getitem__ argument has to be a ColumnReference to the same table or pw.this, or a string "
                    + "(or a list of those)."
                )
            return self._get_colref_by_name(args.name, KeyError)
        elif isinstance(args, str):
            return self._get_colref_by_name(args, KeyError)
        else:
            return self.select(*[self[name] for name in args])

    @trace_user_frame
    @staticmethod
    @runtime_type_check
    def from_columns(
        *args: expr.ColumnReference, **kwargs: expr.ColumnReference
    ) -> Table:
        """Build a table from columns.

        All columns must have the same ids. Columns' names must be pairwise distinct.

        Args:
            args: List of columns.
            kwargs: Columns with their new names.

        Returns:
            Table: Created table.


        Example:

        >>> import pathway as pw
        >>> t1 = pw.Table.empty(age=float, pet=float)
        >>> t2 = pw.Table.empty(foo=float, bar=float).with_universe_of(t1)
        >>> t3 = pw.Table.from_columns(t1.pet, qux=t2.foo)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        pet | qux
        """
        all_args = cast(
            dict[str, expr.ColumnReference], combine_args_kwargs(args, kwargs)
        )
        if not all_args:
            raise ValueError("Table.from_columns() cannot have empty arguments list")
        else:
            arg = next(iter(all_args.values()))
            table: Table = arg.table
            for arg in all_args.values():
                if not G.universe_solver.query_are_equal(
                    table._universe, arg.table._universe
                ):
                    raise ValueError(
                        "Universes of all arguments of Table.from_columns() have to be equal.\n"
                        + "Consider using Table.promise_universes_are_equal() to assert it.\n"
                        + "(However, untrue assertion might result in runtime errors.)"
                    )
            return table.select(*args, **kwargs)

    @trace_user_frame
    @runtime_type_check
    def concat_reindex(self, *tables: Table) -> Table:
        """Concatenate contents of several tables.

        This is similar to PySpark union. All tables must have the same schema. Each row is reindexed.

        Args:
            tables: List of tables to concatenate. All tables must have the same schema.

        Returns:
            Table: The concatenated table. It will have new, synthetic ids.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | pet
        ... 1 | Dog
        ... 7 | Cat
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...   | pet
        ... 1 | Manul
        ... 8 | Octopus
        ... ''')
        >>> t3 = t1.concat_reindex(t2)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        pet
        Cat
        Dog
        Manul
        Octopus
        """
        reindexed = [
            table.with_id_from(table.id, i) for i, table in enumerate([self, *tables])
        ]
        universes.promise_are_pairwise_disjoint(*reindexed)
        return Table.concat(*reindexed)

    @trace_user_frame
    @staticmethod
    @runtime_type_check
    def empty(**kwargs: dt.DType) -> Table:
        """Creates an empty table with a schema specified by kwargs.

        Args:
            kwargs: Dict whose keys are column names and values are column types.

        Returns:
            Table: Created empty table.


        Example:

        >>> import pathway as pw
        >>> t1 = pw.Table.empty(age=float, pet=float)
        >>> pw.debug.compute_and_print(t1, include_id=False)
        age | pet
        """
        ret = empty_from_schema(schema_from_types(None, **kwargs))
        G.universe_solver.register_as_empty(ret._universe)
        return ret

    @trace_user_frame
    @desugar
    @arg_handler(handler=select_args_handler)
    @contextualized_operator
    def select(self, *args: expr.ColumnReference, **kwargs: Any) -> Table:
        """Build a new table with columns specified by kwargs.

        Output columns' names are keys(kwargs). values(kwargs) can be raw values, boxed
        values, columns. Assigning to id reindexes the table.


        Args:
            args: Column references.
            kwargs: Column expressions with their new assigned names.


        Returns:
            Table: Created table.


        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... pet
        ... Dog
        ... Cat
        ... ''')
        >>> t2 = t1.select(animal=t1.pet, desc="fluffy")
        >>> pw.debug.compute_and_print(t2, include_id=False)
        animal | desc
        Cat    | fluffy
        Dog    | fluffy
        """
        new_columns = []

        all_args = combine_args_kwargs(args, kwargs)

        for new_name, expression in all_args.items():
            self._validate_expression(expression)
            column = self._eval(expression)
            new_columns.append((new_name, column))

        return self._with_same_universe(*new_columns)

    @trace_user_frame
    def __add__(self, other: Table) -> Table:
        """Build a union of `self` with `other`.

        Semantics: Returns a table C, such that
            - C.columns == self.columns + other.columns
            - C.id == self.id == other.id

        Args:
            other: The other table. `self.id` must be equal `other.id` and
            `self.columns` and `other.columns` must be disjoint (or overlapping names
            are THE SAME COLUMN)

        Returns:
            Table: Created table.


        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...    pet
        ... 1  Dog
        ... 7  Cat
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...    age
        ... 1   10
        ... 7    3
        ... ''').with_universe_of(t1)
        >>> t3 = t1 + t2
        >>> pw.debug.compute_and_print(t3, include_id=False)
        pet | age
        Cat | 3
        Dog | 10
        """
        if not G.universe_solver.query_are_equal(self._universe, other._universe):
            raise ValueError(
                "Universes of all arguments of Table.__add__() have to be equal.\n"
                + "Consider using Table.promise_universes_are_equal() to assert it.\n"
                + "(However, untrue assertion might result in runtime errors.)"
            )
        return self.select(*self, *other)

    @property
    def slice(self) -> TableSlice:
        """Creates a collection of references to self columns.
        Supports basic column manipulation methods.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | dog
        ... 9   | Bob   | dog
        ... 8   | Alice | cat
        ... 7   | Bob   | dog
        ... ''')
        >>> t1.slice.without("age")
        TableSlice({'owner': <table1>.owner, 'pet': <table1>.pet})
        """
        return TableSlice(dict(**self), self)

    @trace_user_frame
    @desugar
    @runtime_type_check
    def filter(self, filter_expression: expr.ColumnExpression) -> Table[TSchema]:
        """Filter a table according to `filter` condition.


        Args:
            filter: `ColumnExpression` that specifies the filtering condition.

        Returns:
            Table: Result has the same schema as `self` and its ids are subset of `self.id`.


        Example:

        >>> import pathway as pw
        >>> vertices = pw.debug.parse_to_table('''
        ... label outdegree
        ...     1         3
        ...     7         0
        ... ''')
        >>> filtered = vertices.filter(vertices.outdegree == 0)
        >>> pw.debug.compute_and_print(filtered, include_id=False)
        label | outdegree
        7     | 0
        """
        filter_type = self.eval_type(filter_expression)
        if filter_type != dt.BOOL:
            raise TypeError(
                f"Filter argument of Table.filter() has to be bool, found {filter_type}."
            )
        ret = self._filter(filter_expression)
        if (
            filter_col := expr.get_column_filtered_by_is_none(filter_expression)
        ) is not None and filter_col.table == self:
            name = filter_col.name
            dtype = self._columns[name].dtype
            ret = ret.update_types(**{name: dt.unoptionalize(dtype)})
        return ret

    @contextualized_operator
    def _filter(self, filter_expression: expr.ColumnExpression) -> Table[TSchema]:
        self._validate_expression(filter_expression)
        filtering_column = self._eval(filter_expression)
        assert self._universe == filtering_column.universe

        universe = self._universe.subset()
        context = clmn.FilterContext(universe, filtering_column, self._universe)

        return self._table_with_context(context)

    @trace_user_frame
    @desugar
    @runtime_type_check
    @contextualized_operator
    def _forget(
        self,
        threshold_column: expr.ColumnExpression,
        time_column: expr.ColumnExpression,
        mark_forgetting_records: bool,
    ) -> Table:
        universe = self._universe.subset()
        context = clmn.ForgetContext(
            universe,
            self._universe,
            self._eval(threshold_column),
            self._eval(time_column),
            mark_forgetting_records,
        )
        return self._table_with_context(context)

    @trace_user_frame
    @desugar
    @runtime_type_check
    @contextualized_operator
    def _filter_out_results_of_forgetting(
        self,
    ) -> Table:
        universe = self._universe.subset()
        context = clmn.FilterOutForgettingContext(
            universe,
            self._universe,
        )
        return self._table_with_context(context)

    @trace_user_frame
    @desugar
    @runtime_type_check
    @contextualized_operator
    def _freeze(
        self,
        threshold_column: expr.ColumnExpression,
        time_column: expr.ColumnExpression,
    ) -> Table:
        universe = self._universe.subset()
        context = clmn.FreezeContext(
            universe,
            self._universe,
            self._eval(threshold_column),
            self._eval(time_column),
        )
        return self._table_with_context(context)

    @trace_user_frame
    @desugar
    @runtime_type_check
    @contextualized_operator
    def _buffer(
        self,
        threshold_column: expr.ColumnExpression,
        time_column: expr.ColumnExpression,
    ) -> Table:
        universe = self._universe.subset()
        context = clmn.BufferContext(
            universe,
            self._universe,
            self._eval(threshold_column),
            self._eval(time_column),
        )
        return self._table_with_context(context)

    @contextualized_operator
    @runtime_type_check
    def difference(self, other: Table) -> Table[TSchema]:
        r"""Restrict self universe to keys not appearing in the other table.

        Args:
            other: table with ids to remove from self.

        Returns:
            Table: table with restricted universe, with the same set of columns


        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | age  | owner  | pet
        ... 1 | 10   | Alice  | 1
        ... 2 | 9    | Bob    | 1
        ... 3 | 8    | Alice  | 2
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...   | cost
        ... 2 | 100
        ... 3 | 200
        ... 4 | 300
        ... ''')
        >>> t3 = t1.difference(t2)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        age | owner | pet
        10  | Alice | 1
        """
        universe = G.universe_solver.get_difference(self._universe, other._universe)
        context = clmn.DifferenceContext(
            universe=universe,
            left=self._universe,
            right=other._universe,
        )
        return self._table_with_context(context)

    @contextualized_operator
    @runtime_type_check
    def intersect(self, *tables: Table) -> Table[TSchema]:
        """Restrict self universe to keys appearing in all of the tables.

        Args:
            tables: tables keys of which are used to restrict universe.

        Returns:
            Table: table with restricted universe, with the same set of columns


        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | age  | owner  | pet
        ... 1 | 10   | Alice  | 1
        ... 2 | 9    | Bob    | 1
        ... 3 | 8    | Alice  | 2
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...   | cost
        ... 2 | 100
        ... 3 | 200
        ... 4 | 300
        ... ''')
        >>> t3 = t1.intersect(t2)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        age | owner | pet
        8   | Alice | 2
        9   | Bob   | 1
        """
        intersecting_universes = (
            self._universe,
            *tuple(table._universe for table in tables),
        )
        universe = G.universe_solver.get_intersection(*intersecting_universes)
        if universe in intersecting_universes:
            context: clmn.Context = clmn.RestrictContext(
                universe=universe,
                orig_universe=self._universe,
            )
        else:
            context = clmn.IntersectContext(
                universe=universe,
                intersecting_universes=intersecting_universes,
            )

        return self._table_with_context(context)

    @contextualized_operator
    @runtime_type_check
    def restrict(self, other: TableLike) -> Table[TSchema]:
        """Restrict self universe to keys appearing in other.

        Args:
            other: table which universe is used to restrict universe of self.

        Returns:
            Table: table with restricted universe, with the same set of columns


        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table(
        ...     '''
        ...   | age  | owner  | pet
        ... 1 | 10   | Alice  | 1
        ... 2 | 9    | Bob    | 1
        ... 3 | 8    | Alice  | 2
        ... '''
        ... )
        >>> t2 = pw.debug.parse_to_table(
        ...     '''
        ...   | cost
        ... 2 | 100
        ... 3 | 200
        ... '''
        ... )
        >>> t2.promise_universe_is_subset_of(t1)
        <pathway.Table schema={'cost': <class 'int'>}>
        >>> t3 = t1.restrict(t2)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        age | owner | pet
        8   | Alice | 2
        9   | Bob   | 1
        """
        if not G.universe_solver.query_is_subset(other._universe, self._universe):
            raise ValueError(
                "Table.restrict(): other universe has to be a subset of self universe."
                + "Consider using Table.promise_universe_is_subset_of() to assert it."
            )
        context = clmn.RestrictContext(
            universe=other._universe,
            orig_universe=self._universe,
        )

        columns = {
            name: self._wrap_column_in_context(context, column, name)
            for name, column in self._columns.items()
        }

        return Table(
            columns=columns,
            universe=other._universe,
            pk_columns=self._pk_columns,
            id_column=clmn.IdColumn(context),
        )

    @contextualized_operator
    @runtime_type_check
    def copy(self) -> Table[TSchema]:
        """Returns a copy of a table.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | dog
        ... 9   | Bob   | dog
        ... 8   | Alice | cat
        ... 7   | Bob   | dog
        ... ''')
        >>> t2 = t1.copy()
        >>> pw.debug.compute_and_print(t2, include_id=False)
        age | owner | pet
        7   | Bob   | dog
        8   | Alice | cat
        9   | Bob   | dog
        10  | Alice | dog
        >>> t1 is t2
        False
        """

        columns = {
            name: self._wrap_column_in_context(self._context, column, name)
            for name, column in self._columns.items()
        }

        return Table(
            columns=columns,
            universe=self._universe,
            pk_columns=self._pk_columns,
        )

    @trace_user_frame
    @desugar
    @arg_handler(handler=groupby_handler)
    @runtime_type_check
    def groupby(
        self,
        *args: expr.ColumnReference,
        id: expr.ColumnReference | None = None,
        sort_by: expr.ColumnReference | None = None,
        _filter_out_results_of_forgetting: bool = False,
    ) -> groupby.GroupedTable:
        """Groups table by columns from args.

        Note:
            Usually followed by `.reduce()` that aggregates the result and returns a table.

        Args:
            args: columns to group by.
            id: if provided, is the column used to set id's of the rows of the result

        Returns:
            GroupedTable: Groupby object.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | dog
        ... 9   | Bob   | dog
        ... 8   | Alice | cat
        ... 7   | Bob   | dog
        ... ''')
        >>> t2 = t1.groupby(t1.pet, t1.owner).reduce(t1.owner, t1.pet, ageagg=pw.reducers.sum(t1.age))
        >>> pw.debug.compute_and_print(t2, include_id=False)
        owner | pet | ageagg
        Alice | cat | 8
        Alice | dog | 10
        Bob   | dog | 16
        """
        if id is not None:
            if len(args) == 0:
                args = (id,)
            elif len(args) > 1:
                raise ValueError(
                    "Table.groupby() cannot have id argument when grouping by multiple columns."
                )
            elif args[0]._column != id._column:
                raise ValueError(
                    "Table.groupby() received id argument and is grouped by a single column,"
                    + " but the arguments are not equal.\n"
                    + "Consider using <table>.groupby(id=...), skipping the positional argument."
                )

        for arg in args:
            if not isinstance(arg, expr.ColumnReference):
                if isinstance(arg, str):
                    raise ValueError(
                        f"Expected a ColumnReference, found a string. Did you mean <table>.{arg}"
                        + f" instead of {repr(arg)}?"
                    )
                else:
                    raise ValueError(
                        "All Table.groupby() arguments have to be a ColumnReference."
                    )

        return groupby.GroupedTable.create(
            table=self,
            grouping_columns=args,
            set_id=id is not None,
            sort_by=sort_by,
            _filter_out_results_of_forgetting=_filter_out_results_of_forgetting,
        )

    @trace_user_frame
    @desugar
    @arg_handler(handler=reduce_args_handler)
    def reduce(
        self, *args: expr.ColumnReference, **kwargs: expr.ColumnExpression
    ) -> Table:
        """Reduce a table to a single row.

        Equivalent to `self.groupby().reduce(*args, **kwargs)`.

        Args:
            args: reducer to reduce the table with
            kwargs: reducer to reduce the table with. Its key is the new name of a column.

        Returns:
            Table: Reduced table.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | dog
        ... 9   | Bob   | dog
        ... 8   | Alice | cat
        ... 7   | Bob   | dog
        ... ''')
        >>> t2 = t1.reduce(ageagg=pw.reducers.argmin(t1.age))
        >>> pw.debug.compute_and_print(t2, include_id=False) # doctest: +ELLIPSIS
        ageagg
        ^...
        >>> t3 = t2.select(t1.ix(t2.ageagg).age, t1.ix(t2.ageagg).pet)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        age | pet
        7   | dog
        """
        return self.groupby().reduce(*args, **kwargs)

    @trace_user_frame
    def ix(
        self, expression: expr.ColumnExpression, *, optional: bool = False, context=None
    ) -> Table:
        """Reindexes the table using expression values as keys. Uses keys from context, or tries to infer
        proper context from the expression.
        If optional is True, then None in expression values result in None values in the result columns.
        Missing values in table keys result in RuntimeError.

        Context can be anything that allows for `select` or `reduce`, or `pathway.this` construct
        (latter results in returning a delayed operation, and should be only used when using `ix` inside
        join().select() or groupby().reduce() sequence).

        Returns:
            Reindexed table with the same set of columns.

        Example:

        >>> import pathway as pw
        >>> t_animals = pw.debug.parse_to_table('''
        ...   | epithet    | genus
        ... 1 | upupa      | epops
        ... 2 | acherontia | atropos
        ... 3 | bubo       | scandiacus
        ... 4 | dynastes   | hercules
        ... ''')
        >>> t_birds = pw.debug.parse_to_table('''
        ...   | desc
        ... 2 | hoopoe
        ... 4 | owl
        ... ''')
        >>> ret = t_birds.select(t_birds.desc, latin=t_animals.ix(t_birds.id).genus)
        >>> pw.debug.compute_and_print(ret, include_id=False)
        desc   | latin
        hoopoe | atropos
        owl    | hercules
        """

        if context is None:
            all_tables = collect_tables(expression)
            if len(all_tables) == 0:
                context = thisclass.this
            elif all(tab == all_tables[0] for tab in all_tables):
                context = all_tables[0]
        if context is None:
            for tab in all_tables:
                if not isinstance(tab, Table):
                    raise ValueError("Table expected here.")
            if len(all_tables) == 0:
                raise ValueError("Const value provided.")
            context = all_tables[0]
            for tab in all_tables:
                assert context._universe.is_equal_to(tab._universe)
        if isinstance(context, groupby.GroupedJoinable):
            context = thisclass.this
        if isinstance(context, thisclass.ThisMetaclass):
            return context._delayed_op(
                lambda table, expression: self.ix(
                    expression=expression, optional=optional, context=table
                ),
                expression=expression,
                qualname=f"{self}.ix(...)",
                name="ix",
            )
        restrict_universe = RestrictUniverseDesugaring(context)
        expression = restrict_universe.eval_expression(expression)
        key_col = context.select(tmp=expression).tmp
        key_dtype = self.eval_type(key_col)
        if (
            optional and not dt.dtype_issubclass(key_dtype, dt.Optional(dt.POINTER))
        ) or (not optional and not isinstance(key_dtype, dt.Pointer)):
            raise TypeError(
                f"Pathway supports indexing with Pointer type only. The type used was {key_dtype}."
            )
        if optional and isinstance(key_dtype, dt.Optional):
            self_ = self.update_types(
                **{name: dt.Optional(self.typehints()[name]) for name in self.keys()}
            )
        else:
            self_ = self
        return self_._ix(key_col, optional)

    @contextualized_operator
    def _ix(
        self,
        key_expression: expr.ColumnReference,
        optional: bool,
    ) -> Table:
        key_universe_table = key_expression._table
        universe = key_universe_table._universe
        key_column = key_expression._column

        context = clmn.IxContext(universe, self._universe, key_column, optional)

        return self._table_with_context(context)

    def __lshift__(self, other: Table) -> Table:
        """Alias to update_cells method.

        Updates cells of `self`, breaking ties in favor of the values in `other`.

        Semantics:
            - result.columns == self.columns
            - result.id == self.id
            - conflicts are resolved preferring other's values

        Requires:
            - other.columns ⊆ self.columns
            - other.id ⊆ self.id

        Args:
            other:  the other table.

        Returns:
            Table: `self` updated with cells form `other`.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | age | owner | pet
        ... 1 | 10  | Alice | 1
        ... 2 | 9   | Bob   | 1
        ... 3 | 8   | Alice | 2
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...   | age | owner | pet
        ... 1 | 10  | Alice | 30
        ... ''')
        >>> pw.universes.promise_is_subset_of(t2, t1)
        >>> t3 = t1 << t2
        >>> pw.debug.compute_and_print(t3, include_id=False)
        age | owner | pet
        8   | Alice | 2
        9   | Bob   | 1
        10  | Alice | 30
        """
        return self.update_cells(other)

    @trace_user_frame
    @runtime_type_check
    def concat(self, *others: Table[TSchema]) -> Table[TSchema]:
        """Concats `self` with every `other` ∊ `others`.

        Semantics:
        - result.columns == self.columns == other.columns
        - result.id == self.id ∪ other.id

        if self.id and other.id collide, throws an exception.

        Requires:
        - other.columns == self.columns
        - self.id disjoint with other.id

        Args:
            other:  the other table.

        Returns:
            Table: The concatenated table. Id's of rows from original tables are preserved.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | age | owner | pet
        ... 1 | 10  | Alice | 1
        ... 2 | 9   | Bob   | 1
        ... 3 | 8   | Alice | 2
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...    | age | owner | pet
        ... 11 | 11  | Alice | 30
        ... 12 | 12  | Tom   | 40
        ... ''')
        >>> pw.universes.promise_are_pairwise_disjoint(t1, t2)
        >>> t3 = t1.concat(t2)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        age | owner | pet
        8   | Alice | 2
        9   | Bob   | 1
        10  | Alice | 1
        11  | Alice | 30
        12  | Tom   | 40
        """
        for other in others:
            if other.keys() != self.keys():
                raise ValueError(
                    "columns do not match in the argument of Table.concat()"
                )

        schema = {
            key: functools.reduce(
                dt.types_lca,
                [other.schema._dtypes()[key] for other in others],
                self.schema._dtypes()[key],
            )
            for key in self.keys()
        }

        return Table._concat(
            self.cast_to_types(**schema),
            *[other.cast_to_types(**schema) for other in others],
        )

    @trace_user_frame
    @contextualized_operator
    def _concat(self, *others: Table[TSchema]) -> Table[TSchema]:
        union_universes = (self._universe, *(other._universe for other in others))
        if not G.universe_solver.query_are_disjoint(*union_universes):
            raise ValueError(
                "Universes of the arguments of Table.concat() have to be disjoint.\n"
                + "Consider using Table.promise_universes_are_disjoint() to assert it.\n"
                + "(However, untrue assertion might result in runtime errors.)"
            )
        universe = G.universe_solver.get_union(*union_universes)
        context = clmn.ConcatUnsafeContext(
            universe=universe,
            union_universes=union_universes,
            updates=tuple(
                {col_name: other._columns[col_name] for col_name in self.keys()}
                for other in others
            ),
        )
        columns = {
            name: self._wrap_column_in_context(context, column, name)
            for name, column in self._columns.items()
        }
        ret: Table = Table(
            columns=columns,
            universe=universe,
            pk_columns=self._pk_columns,
            id_column=clmn.IdColumn(context),
        )
        return ret

    @trace_user_frame
    @runtime_type_check
    def update_cells(self, other: Table) -> Table:
        """Updates cells of `self`, breaking ties in favor of the values in `other`.

        Semantics:
            - result.columns == self.columns
            - result.id == self.id
            - conflicts are resolved preferring other's values

        Requires:
            - other.columns ⊆ self.columns
            - other.id ⊆ self.id

        Args:
            other:  the other table.

        Returns:
            Table: `self` updated with cells form `other`.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | age | owner | pet
        ... 1 | 10  | Alice | 1
        ... 2 | 9   | Bob   | 1
        ... 3 | 8   | Alice | 2
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...     age | owner | pet
        ... 1 | 10  | Alice | 30
        ... ''')
        >>> pw.universes.promise_is_subset_of(t2, t1)
        >>> t3 = t1.update_cells(t2)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        age | owner | pet
        8   | Alice | 2
        9   | Bob   | 1
        10  | Alice | 30
        """
        if names := (set(other.keys()) - set(self.keys())):
            raise ValueError(
                f"Columns of the argument in Table.update_cells() not present in the updated table: {list(names)}."
            )

        if self._universe == other._universe:
            warnings.warn(
                "Key sets of self and other in update_cells are the same."
                + " Using with_columns instead of update_cells."
            )
            return self.with_columns(*(other[name] for name in other))

        schema = {
            key: dt.types_lca(self.schema.__dtypes__[key], other.schema.__dtypes__[key])
            for key in other.keys()
        }
        return Table._update_cells(
            self.cast_to_types(**schema), other.cast_to_types(**schema)
        )

    @trace_user_frame
    @contextualized_operator
    @runtime_type_check
    def _update_cells(self, other: Table) -> Table:
        if not other._universe.is_subset_of(self._universe):
            raise ValueError(
                "Universe of the argument of Table.update_cells() needs to be "
                + "a subset of the universe of the updated table.\n"
                + "Consider using Table.promise_is_subset_of() to assert this.\n"
                + "(However, untrue assertion might result in runtime errors.)"
            )

        union_universes = [self._universe]
        if other._universe != self._universe:
            union_universes.append(other._universe)
        context = clmn.UpdateCellsContext(
            universe=self._universe,
            union_universes=tuple(union_universes),
            updates={name: other._columns[name] for name in other.keys()},
        )
        columns = {
            name: self._wrap_column_in_context(context, column, name)
            for name, column in self._columns.items()
        }
        return Table(
            columns=columns,
            universe=self._universe,
            pk_columns=self._pk_columns,
            id_column=clmn.IdColumn(context),
        )

    @trace_user_frame
    @runtime_type_check
    def update_rows(self, other: Table[TSchema]) -> Table[TSchema]:
        """Updates rows of `self`, breaking ties in favor for the rows in `other`.

        Semantics:
        - result.columns == self.columns == other.columns
        - result.id == self.id ∪ other.id

        Requires:
        - other.columns == self.columns

        Args:
            other:  the other table.

        Returns:
            Table: `self` updated with rows form `other`.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | age | owner | pet
        ... 1 | 10  | Alice | 1
        ... 2 | 9   | Bob   | 1
        ... 3 | 8   | Alice | 2
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...    | age | owner | pet
        ... 1  | 10  | Alice | 30
        ... 12 | 12  | Tom   | 40
        ... ''')
        >>> t3 = t1.update_rows(t2)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        age | owner | pet
        8   | Alice | 2
        9   | Bob   | 1
        10  | Alice | 30
        12  | Tom   | 40
        """
        if other.keys() != self.keys():
            raise ValueError(
                "Columns do not match between argument of Table.update_rows() and the updated table."
            )
        if self._universe.is_subset_of(other._universe):
            warnings.warn(
                "Universe of self is a subset of universe of other in update_rows. Returning other."
            )
            return other

        schema = {
            key: dt.types_lca(self.schema.__dtypes__[key], other.schema.__dtypes__[key])
            for key in self.keys()
        }
        return Table._update_rows(
            self.cast_to_types(**schema), other.cast_to_types(**schema)
        )

    @trace_user_frame
    @contextualized_operator
    @runtime_type_check
    def _update_rows(self, other: Table[TSchema]) -> Table[TSchema]:
        union_universes = (self._universe, other._universe)
        universe = G.universe_solver.get_union(*union_universes)
        context_cls = (
            clmn.UpdateCellsContext
            if universe == self._universe
            else clmn.UpdateRowsContext
        )
        context = context_cls(
            universe=universe,
            union_universes=union_universes,
            updates={col_name: other._columns[col_name] for col_name in self.keys()},
        )
        columns = {
            name: self._wrap_column_in_context(context, column, name)
            for name, column in self._columns.items()
        }
        ret: Table = Table(
            columns=columns,
            universe=universe,
            pk_columns=self._pk_columns,
            id_column=clmn.IdColumn(context),
        )
        return ret

    @trace_user_frame
    @desugar
    def with_columns(self, *args: expr.ColumnReference, **kwargs: Any) -> Table:
        """Updates columns of `self`, according to args and kwargs.
        See `table.select` specification for evaluation of args and kwargs.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | age | owner | pet
        ... 1 | 10  | Alice | 1
        ... 2 | 9   | Bob   | 1
        ... 3 | 8   | Alice | 2
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...   | owner | pet | size
        ... 1 | Tom   | 1   | 10
        ... 2 | Bob   | 1   | 9
        ... 3 | Tom   | 2   | 8
        ... ''').with_universe_of(t1)
        >>> t3 = t1.with_columns(*t2)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        age | owner | pet | size
        8   | Tom   | 2   | 8
        9   | Bob   | 1   | 9
        10  | Tom   | 1   | 10
        """
        other = self.select(*args, **kwargs)
        columns = dict(self)
        columns.update(other)
        return self.select(**columns)

    @trace_user_frame
    @desugar
    @runtime_type_check
    def with_id(self, new_index: expr.ColumnReference) -> Table:
        """Set new ids based on another column containing id-typed values.

        To generate ids based on arbitrary valued columns, use `with_id_from`.

        Values assigned must be row-wise unique.

        Args:
            new_id: column to be used as the new index.

        Returns:
            Table with updated ids.

        Example:

        >>> import pytest; pytest.xfail("with_id is hard to test")
        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | age | owner | pet
        ... 1 | 10  | Alice | 1
        ... 2 | 9   | Bob   | 1
        ... 3 | 8   | Alice | 2
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...   | new_id
        ... 1 | 2
        ... 2 | 3
        ... 3 | 4
        ... ''')
        >>> t3 = t1.promise_universe_is_subset_of(t2).with_id(t2.new_id)
        >>> pw.debug.compute_and_print(t3)
            age  owner  pet
        ^2   10  Alice    1
        ^3    9    Bob    1
        ^4    8  Alice    2
        """
        return self._with_new_index(new_index)

    @trace_user_frame
    @desugar
    @runtime_type_check
    def with_id_from(self, *args: expr.ColumnExpressionOrValue) -> Table:
        """Compute new ids based on values in columns.
        Ids computed from `columns` must be row-wise unique.

        Args:
            columns:  columns to be used as primary keys.

        Returns:
            Table: `self` updated with recomputed ids.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...    | age | owner  | pet
        ...  1 | 10  | Alice  | 1
        ...  2 | 9   | Bob    | 1
        ...  3 | 8   | Alice  | 2
        ... ''')
        >>> t2 = t1 + t1.select(old_id=t1.id)
        >>> t3 = t2.with_id_from(t2.age)
        >>> pw.debug.compute_and_print(t3) # doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
             | age | owner | pet | old_id
        ^... | 8   | Alice | 2   | ^...
        ^... | 9   | Bob   | 1   | ^...
        ^... | 10  | Alice | 1   | ^...
        >>> t4 = t3.select(t3.age, t3.owner, t3.pet, same_as_old=(t3.id == t3.old_id),
        ...     same_as_new=(t3.id == t3.pointer_from(t3.age)))
        >>> pw.debug.compute_and_print(t4) # doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
             | age | owner | pet | same_as_old | same_as_new
        ^... | 8   | Alice | 2   | False       | True
        ^... | 9   | Bob   | 1   | False       | True
        ^... | 10  | Alice | 1   | False       | True
        """
        # new_index should be a column, so a little workaround
        new_index = self.select(ref_column=self.pointer_from(*args)).ref_column
        if all(isinstance(arg, expr.ColumnReference) for arg in args):
            args_typed: tuple[expr.ColumnReference] = args  # type: ignore
            pk_columns = {arg.name: self._eval(arg) for arg in args_typed}
        else:
            pk_columns = {}
        return self._with_new_index(
            new_index=new_index,
            pk_columns=pk_columns,
        )

    @trace_user_frame
    @contextualized_operator
    @runtime_type_check
    def _with_new_index(
        self,
        new_index: expr.ColumnExpression,
        pk_columns: dict[str, clmn.ColumnWithExpression] = {},
    ) -> Table:
        self._validate_expression(new_index)
        index_type = self.eval_type(new_index)
        if not isinstance(index_type, dt.Pointer):
            raise TypeError(
                f"Pathway supports reindexing Tables with Pointer type only. The type used was {index_type}."
            )
        reindex_column = self._eval(new_index)
        assert self._universe == reindex_column.universe

        universe = Universe()
        context = clmn.ReindexContext(universe, reindex_column)

        columns = {
            name: self._wrap_column_in_context(context, column, name)
            for name, column in self._columns.items()
        }

        return Table(
            columns=columns,
            universe=universe,
            pk_columns=pk_columns,
            id_column=clmn.IdColumn(context),
        )

    @trace_user_frame
    @desugar
    @contextualized_operator
    @runtime_type_check
    def rename_columns(self, **kwargs: str | expr.ColumnReference) -> Table:
        """Rename columns according to kwargs.

        Columns not in keys(kwargs) are not changed. New name of a column must not be `id`.

        Args:
            kwargs:  mapping from old column names to new names.

        Returns:
            Table: `self` with columns renamed.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | 1
        ... 9   | Bob   | 1
        ... 8   | Alice | 2
        ... ''')
        >>> t2 = t1.rename_columns(years_old=t1.age, animal=t1.pet)
        >>> pw.debug.compute_and_print(t2, include_id=False)
        owner | years_old | animal
        Alice | 8         | 2
        Alice | 10        | 1
        Bob   | 9         | 1
        """
        mapping: dict[str, str] = {}
        for new_name, old_name_col in kwargs.items():
            if isinstance(old_name_col, expr.ColumnReference):
                old_name = old_name_col.name
            else:
                old_name = old_name_col
            if old_name not in self._columns:
                raise ValueError(f"Column {old_name} does not exist in a given table.")
            mapping[new_name] = old_name
        renamed_columns = self._columns.copy()
        for new_name, old_name in mapping.items():
            renamed_columns.pop(old_name)
        for new_name, old_name in mapping.items():
            renamed_columns[new_name] = self._columns[old_name]

        columns_wrapped = {
            name: self._wrap_column_in_context(
                self._context, column, mapping[name] if name in mapping else name
            )
            for name, column in renamed_columns.items()
        }
        return self._with_same_universe(*columns_wrapped.items())

    @runtime_type_check
    def rename_by_dict(
        self, names_mapping: dict[str | expr.ColumnReference, str]
    ) -> Table:
        """Rename columns according to a dictionary.

        Columns not in keys(kwargs) are not changed. New name of a column must not be `id`.

        Args:
            names_mapping: mapping from old column names to new names.

        Returns:
            Table: `self` with columns renamed.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | 1
        ... 9   | Bob   | 1
        ... 8   | Alice | 2
        ... ''')
        >>> t2 = t1.rename_by_dict({"age": "years_old", t1.pet: "animal"})
        >>> pw.debug.compute_and_print(t2, include_id=False)
        owner | years_old | animal
        Alice | 8         | 2
        Alice | 10        | 1
        Bob   | 9         | 1
        """
        return self.rename_columns(
            **{new_name: self[old_name] for old_name, new_name in names_mapping.items()}
        )

    @runtime_type_check
    def with_prefix(self, prefix: str) -> Table:
        """Rename columns by adding prefix to each name of column.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | 1
        ... 9   | Bob   | 1
        ... 8   | Alice | 2
        ... ''')
        >>> t2 = t1.with_prefix("u_")
        >>> pw.debug.compute_and_print(t2, include_id=False)
        u_age | u_owner | u_pet
        8     | Alice   | 2
        9     | Bob     | 1
        10    | Alice   | 1
        """
        return self.rename_by_dict({name: prefix + name for name in self.keys()})

    @runtime_type_check
    def with_suffix(self, suffix: str) -> Table:
        """Rename columns by adding suffix to each name of column.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | 1
        ... 9   | Bob   | 1
        ... 8   | Alice | 2
        ... ''')
        >>> t2 = t1.with_suffix("_current")
        >>> pw.debug.compute_and_print(t2, include_id=False)
        age_current | owner_current | pet_current
        8           | Alice         | 2
        9           | Bob           | 1
        10          | Alice         | 1
        """
        return self.rename_by_dict({name: name + suffix for name in self.keys()})

    @trace_user_frame
    @runtime_type_check
    def rename(
        self,
        names_mapping: dict[str | expr.ColumnReference, str] | None = None,
        **kwargs: expr.ColumnExpression,
    ) -> Table:
        """Rename columns according either a dictionary or kwargs.

        If a mapping is provided using a dictionary, ``rename_by_dict`` will be used.
        Otherwise, ``rename_columns`` will be used with kwargs.
        Columns not in keys(kwargs) are not changed. New name of a column must not be ``id``.

        Args:
            names_mapping: mapping from old column names to new names.
            kwargs:  mapping from old column names to new names.

        Returns:
            Table: `self` with columns renamed.
        """
        if names_mapping is not None:
            return self.rename_by_dict(names_mapping=names_mapping)
        return self.rename_columns(**kwargs)

    @trace_user_frame
    @desugar
    @contextualized_operator
    @runtime_type_check
    def without(self, *columns: str | expr.ColumnReference) -> Table:
        """Selects all columns without named column references.

        Args:
            columns: columns to be dropped provided by `table.column_name` notation.

        Returns:
            Table: `self` without specified columns.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age  | owner  | pet
        ...  10  | Alice  | 1
        ...   9  | Bob    | 1
        ...   8  | Alice  | 2
        ... ''')
        >>> t2 = t1.without(t1.age, pw.this.pet)
        >>> pw.debug.compute_and_print(t2, include_id=False)
        owner
        Alice
        Alice
        Bob
        """
        new_columns = self._columns.copy()
        for col in columns:
            if isinstance(col, expr.ColumnReference):
                new_columns.pop(col.name)
            else:
                assert isinstance(col, str)
                new_columns.pop(col)
        columns_wrapped = {
            name: self._wrap_column_in_context(self._context, column, name)
            for name, column in new_columns.items()
        }
        return self._with_same_universe(*columns_wrapped.items())

    @trace_user_frame
    @desugar
    @runtime_type_check
    def having(self, *indexers: expr.ColumnReference) -> Table[TSchema]:
        """Removes rows so that indexed.ix(indexer) is possible when some rows are missing,
        for each indexer in indexers"""
        rets: list[Table] = []
        for indexer in indexers:
            rets.append(self._having(indexer))
        if len(rets) == 0:
            return self
        elif len(rets) == 1:
            [ret] = rets
            return ret
        else:
            return rets[0].intersect(*rets[1:])

    @trace_user_frame
    @runtime_type_check
    def update_types(self, **kwargs: Any) -> Table:
        """Updates types in schema. Has no effect on the runtime."""

        for name in kwargs.keys():
            if name not in self.keys():
                raise ValueError(
                    "Table.update_types() argument name has to be an existing table column name."
                )
        from pathway.internals.common import declare_type

        return self.with_columns(
            **{key: declare_type(val, self[key]) for key, val in kwargs.items()}
        )

    @runtime_type_check
    def cast_to_types(self, **kwargs: Any) -> Table:
        """Casts columns to types."""

        for name in kwargs.keys():
            if name not in self.keys():
                raise ValueError(
                    "Table.cast_to_types() argument name has to be an existing table column name."
                )
        from pathway.internals.common import cast

        return self.with_columns(
            **{key: cast(val, self[key]) for key, val in kwargs.items()}
        )

    @contextualized_operator
    @runtime_type_check
    def _having(self, indexer: expr.ColumnReference) -> Table[TSchema]:
        universe = indexer.table._universe.subset()
        context = clmn.HavingContext(
            universe=universe, orig_universe=self._universe, key_column=indexer._column
        )

        return self._table_with_context(context)

    @trace_user_frame
    @runtime_type_check
    def with_universe_of(self, other: TableLike) -> Table:
        """Returns a copy of self with exactly the same universe as others.

        Semantics: Required precondition self.universe == other.universe
        Used in situations where Pathway cannot deduce equality of universes, but
        those are equal as verified during runtime.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | pet
        ... 1 | Dog
        ... 7 | Cat
        ... ''')
        >>> t2 = pw.debug.parse_to_table('''
        ...   | age
        ... 1 | 10
        ... 7 | 3
        ... ''').with_universe_of(t1)
        >>> t3 = t1 + t2
        >>> pw.debug.compute_and_print(t3, include_id=False)
        pet | age
        Cat | 3
        Dog | 10
        """
        if self._universe == other._universe:
            return self.copy()
        universes.promise_are_equal(self, other)
        return self._unsafe_promise_universe(other)

    @trace_user_frame
    @runtime_type_check
    def flatten(self, *args: expr.ColumnReference, **kwargs: Any) -> Table:
        """Performs a flatmap operation on a column or expression given as a first
        argument. Datatype of this column or expression has to be iterable.
        Other columns specified in the method arguments are duplicated
        as many times as the length of the iterable.

        It is possible to get ids of source rows by using `table.id` column, e.g.
        `table.flatten(table.column_to_be_flattened, original_id = table.id)`.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...   | pet  |  age
        ... 1 | Dog  |   2
        ... 7 | Cat  |   5
        ... ''')
        >>> t2 = t1.flatten(t1.pet)
        >>> pw.debug.compute_and_print(t2, include_id=False)
        pet
        C
        D
        a
        g
        o
        t
        >>> t3 = t1.flatten(t1.pet, t1.age)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        pet | age
        C   | 5
        D   | 2
        a   | 5
        g   | 2
        o   | 2
        t   | 5
        """
        intermediate_table = self.select(*args, **kwargs)
        all_args = combine_args_kwargs(args, kwargs)
        if not all_args:
            raise ValueError("Table.flatten() cannot have empty arguments list.")

        all_names_iter = iter(all_args.keys())
        flatten_name = next(all_names_iter)
        return intermediate_table._flatten(flatten_name)

    @desugar
    @contextualized_operator
    def _flatten(
        self,
        flatten_name: str,
    ) -> Table:
        flatten_column = self._columns[flatten_name]
        assert isinstance(flatten_column, clmn.ColumnWithExpression)

        universe = Universe()
        flatten_result_column = clmn.MaterializedColumn(
            universe,
            ColumnProperties(
                dtype=clmn.FlattenContext.get_flatten_column_dtype(flatten_column),
            ),
        )
        context = clmn.FlattenContext(
            universe=universe,
            orig_universe=self._universe,
            flatten_column=flatten_column,
            flatten_result_column=flatten_result_column,
        )

        columns = {
            name: self._wrap_column_in_context(context, column, name)
            for name, column in self._columns.items()
            if name != flatten_name
        }

        return Table(
            columns={
                flatten_name: flatten_result_column,
                **columns,
            },
            universe=universe,
            pk_columns={},
            id_column=clmn.IdColumn(context),
        )

    @trace_user_frame
    @desugar
    @contextualized_operator
    @runtime_type_check
    def _sort_experimental(
        self,
        key: expr.ColumnExpression,
        instance: expr.ColumnExpression | None = None,
    ) -> Table:
        if not isinstance(instance, expr.ColumnExpression):
            instance = expr.ColumnConstExpression(instance)
        prev_column = clmn.MaterializedColumn(
            self._universe, ColumnProperties(dtype=dt.Optional(dt.POINTER))
        )
        next_column = clmn.MaterializedColumn(
            self._universe, ColumnProperties(dtype=dt.Optional(dt.POINTER))
        )
        context = clmn.SortingContext(
            self._universe,
            self._eval(key),
            self._eval(instance),
            prev_column,
            next_column,
        )
        return Table(
            columns={
                "prev": prev_column,
                "next": next_column,
            },
            universe=self._universe,
            pk_columns={},
            id_column=clmn.IdColumn(context),
        )

    def _set_source(self, source: OutputHandle):
        self._source = source
        self._id_column.lineage = clmn.ColumnLineage(name="id", source=source)
        for name, column in self._columns.items():
            if not hasattr(column, "lineage"):
                column.lineage = clmn.ColumnLineage(name=name, source=source)
        universe = self._universe
        if not hasattr(universe, "lineage"):
            universe.lineage = clmn.Lineage(source=source)

    @contextualized_operator
    def _unsafe_promise_universe(self, other: TableLike) -> Table:
        context = clmn.PromiseSameUniverseContext(other._universe, self._universe)
        columns = {
            name: self._wrap_column_in_context(context, column, name)
            for name, column in self._columns.items()
        }

        return Table(
            columns=columns,
            universe=context.universe,
            pk_columns=self._pk_columns,
            id_column=clmn.IdColumn(context),
        )

    def _validate_expression(self, expression: expr.ColumnExpression):
        for dep in expression._dependencies_above_reducer():
            if self._universe != dep.to_colref()._column.universe:
                raise ValueError(
                    f"You cannot use {dep.to_colref()} in this context."
                    + " Its universe is different than the universe of the table the method"
                    + " was called on. You can use <table1>.with_universe_of(<table2>)"
                    + " to assign universe of <table2> to <table1> if you're sure their"
                    + " sets of keys are equal."
                )

    def _wrap_column_in_context(
        self,
        context: clmn.Context,
        column: clmn.Column,
        name: str,
        lineage: clmn.Lineage | None = None,
    ) -> clmn.Column:
        """Contextualize column by wrapping it in expression."""
        expression = expr.ColumnReference(table=self, column=column, name=name)
        return expression._column_with_expression_cls(
            context=context,
            universe=context.universe,
            expression=expression,
            lineage=lineage,
        )

    def _table_with_context(self, context: clmn.Context) -> Table:
        columns = {
            name: self._wrap_column_in_context(context, column, name)
            for name, column in self._columns.items()
        }

        return Table(
            columns=columns,
            universe=context.universe,
            pk_columns=self._pk_columns,
            id_column=clmn.IdColumn(context),
        )

    @functools.cached_property
    def _table_restricted_context(self) -> clmn.TableRestrictedRowwiseContext:
        return clmn.TableRestrictedRowwiseContext(self._universe, self)

    def _eval(
        self, expression: expr.ColumnExpression, context: clmn.Context | None = None
    ) -> clmn.ColumnWithExpression:
        """Desugar expression and wrap it in given context."""
        if context is None:
            context = self._context
        column = expression._column_with_expression_cls(
            context=context,
            universe=context.universe,
            expression=expression,
        )
        return column

    @classmethod
    def _from_schema(cls, schema: type[Schema]) -> Table:
        universe = Universe()
        columns = {
            name: clmn.MaterializedColumn(
                universe,
                schema.column_properties(name),
            )
            for name in schema.column_names()
        }
        return cls(columns=columns, universe=universe, pk_columns={}, schema=schema)

    def __repr__(self) -> str:
        return f"<pathway.Table schema={dict(self.typehints())}>"

    def _with_same_universe(
        self,
        *columns: tuple[str, clmn.Column],
        schema: type[Schema] | None = None,
    ) -> Table:
        return Table(
            columns=dict(columns),
            pk_columns=self._pk_columns,
            universe=self._universe,
            schema=schema,
            id_column=clmn.IdColumn(self._context),
        )

    def _sort_columns_by_other(self, other: Table):
        assert self.keys() == other.keys()
        self._columns = {name: self._columns[name] for name in other.keys()}

    def _operator_dependencies(self) -> StableSet[Table]:
        return StableSet([self])

    def debug(self, name: str):
        G.add_operator(
            lambda id: DebugOperator(name, id),
            lambda operator: operator(self),
        )
        return self

    def to(self, sink: DataSink) -> None:
        table_to_datasink(self, sink)

    def _materialize(self, universe: Universe):
        columns = {
            name: clmn.MaterializedColumn(universe, column.properties)
            for (name, column) in self._columns.items()
        }
        return Table(
            columns=columns,
            universe=universe,
            pk_columns=self._pk_columns,
            schema=self.schema,
        )

    @trace_user_frame
    def pointer_from(self, *args: Any, optional=False):
        """Pseudo-random hash of its argument. Produces pointer types. Applied column-wise.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ...    age  owner  pet
        ... 1   10  Alice  dog
        ... 2    9    Bob  dog
        ... 3    8  Alice  cat
        ... 4    7    Bob  dog''')
        >>> g = t1.groupby(t1.owner).reduce(refcol = t1.pointer_from(t1.owner)) # g.id == g.refcol
        >>> pw.debug.compute_and_print(g.select(test = (g.id == g.refcol)), include_id=False)
        test
        True
        True
        """
        # XXX verify types for the table primary_keys
        return expr.PointerExpression(self, *args, optional=optional)

    @trace_user_frame
    def ix_ref(
        self, *args: expr.ColumnExpressionOrValue, optional: bool = False, context=None
    ):
        """Reindexes the table using expressions as primary keys.
        Uses keys from context, or tries to infer proper context from the expression.
        If optional is True, then None in expression values result in None values in the result columns.
        Missing values in table keys result in RuntimeError.

        Context can be anything that allows for `select` or `reduce`, or `pathway.this` construct
        (latter results in returning a delayed operation, and should be only used when using `ix` inside
        join().select() or groupby().reduce() sequence).


        Args:
            args: Column references.

        Returns:
            Row: indexed row.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... name   | pet
        ... Alice  | dog
        ... Bob    | cat
        ... Carole | cat
        ... David  | dog
        ... ''')
        >>> t2 = t1.with_id_from(pw.this.name)
        >>> t2 = t2.select(*pw.this, new_value=pw.this.ix_ref("Alice").pet)
        >>> pw.debug.compute_and_print(t2, include_id=False)
        name   | pet | new_value
        Alice  | dog | dog
        Bob    | cat | dog
        Carole | cat | dog
        David  | dog | dog

        Tables obtained by a groupby/reduce scheme always have primary keys:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... name   | pet
        ... Alice  | dog
        ... Bob    | cat
        ... Carole | cat
        ... David  | cat
        ... ''')
        >>> t2 = t1.groupby(pw.this.pet).reduce(pw.this.pet, count=pw.reducers.count())
        >>> t3 = t1.select(*pw.this, new_value=t2.ix_ref(t1.pet).count)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        name   | pet | new_value
        Alice  | dog | 1
        Bob    | cat | 3
        Carole | cat | 3
        David  | cat | 3

        Single-row tables can be accessed via `ix_ref()`:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... name   | pet
        ... Alice  | dog
        ... Bob    | cat
        ... Carole | cat
        ... David  | cat
        ... ''')
        >>> t2 = t1.reduce(count=pw.reducers.count())
        >>> t3 = t1.select(*pw.this, new_value=t2.ix_ref(context=t1).count)
        >>> pw.debug.compute_and_print(t3, include_id=False)
        name   | pet | new_value
        Alice  | dog | 4
        Bob    | cat | 4
        Carole | cat | 4
        David  | cat | 4
        """
        return self.ix(
            self.pointer_from(*args, optional=optional),
            optional=optional,
            context=context,
        )

    def _subtables(self) -> StableSet[Table]:
        return StableSet([self])

    def _substitutions(
        self,
    ) -> tuple[Table, dict[expr.InternalColRef, expr.ColumnExpression]]:
        return self, {}

    def typehints(self) -> Mapping[str, Any]:
        """
        Return the types of the columns as a dictionary.

        Example:

        >>> import pathway as pw
        >>> t1 = pw.debug.parse_to_table('''
        ... age | owner | pet
        ... 10  | Alice | dog
        ... 9   | Bob   | dog
        ... 8   | Alice | cat
        ... 7   | Bob   | dog
        ... ''')
        >>> t1.typehints()
        mappingproxy({'age': <class 'int'>, 'owner': <class 'str'>, 'pet': <class 'str'>})
        """
        return self.schema.typehints()

    def eval_type(self, expression: expr.ColumnExpression) -> dt.DType:
        return (
            self._context._get_type_interpreter()
            .eval_expression(expression, state=TypeInterpreterState())
            ._dtype
        )