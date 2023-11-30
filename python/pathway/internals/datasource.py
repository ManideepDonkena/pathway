# Copyright © 2023 Pathway

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import pandas as pd

from pathway.internals import api
from pathway.internals.schema import Schema, schema_from_pandas


@dataclass(frozen=True)
class DataSourceOptions:
    commit_duration_ms: int | None = None
    unsafe_trusted_ids: bool | None = False


@dataclass(frozen=True, kw_only=True)
class DataSource(ABC):
    schema: type[Schema]
    data_source_options: DataSourceOptions = DataSourceOptions()

    @property
    def connector_properties(self) -> api.ConnectorProperties:
        columns: list[api.ColumnProperties] = []
        for column in self.schema.columns().values():
            columns.append(
                api.ColumnProperties(
                    dtype=column.dtype.map_to_engine(),
                    append_only=column.append_only,
                )
            )

        return api.ConnectorProperties(
            commit_duration_ms=self.data_source_options.commit_duration_ms,
            unsafe_trusted_ids=self.data_source_options.unsafe_trusted_ids,
            column_properties=columns,
        )

    @abstractmethod
    def is_bounded(self) -> bool:
        ...


class StaticDataSource(DataSource, ABC):
    data: Any

    def is_bounded(self) -> bool:
        return True


@dataclass(frozen=True)
class PandasDataSource(StaticDataSource):
    data: pd.DataFrame


@dataclass(frozen=True)
class GenericDataSource(DataSource):
    datastorage: api.DataStorage
    dataformat: api.DataFormat

    def is_bounded(self) -> bool:
        return self.datastorage.mode == api.ConnectorMode.STATIC


@dataclass(frozen=True)
class EmptyDataSource(DataSource):
    def is_bounded(self) -> bool:
        return True


def debug_datasource(debug_data) -> StaticDataSource | None:
    if debug_data is None:
        return None
    elif isinstance(debug_data, pd.DataFrame):
        return PandasDataSource(
            data=debug_data.copy(), schema=schema_from_pandas(debug_data)
        )
    else:
        raise TypeError("not supported type of debug data")
