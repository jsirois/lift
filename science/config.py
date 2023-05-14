# Copyright 2022 Science project contributors.
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import os
import tomllib
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum, auto
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, TypeVar

from packaging import version
from packaging.version import Version

from science.frozendict import FrozenDict
from science.hashing import Fingerprint
from science.model import (
    Application,
    Binding,
    Command,
    Digest,
    Env,
    File,
    FileSource,
    FileType,
    Identifier,
    Interpreter,
    InterpreterGroup,
    Ptex,
    ScieJump,
)
from science.platform import Platform
from science.providers import get_provider

_T = TypeVar("_T")


@dataclass(frozen=True)
class Data:
    source: str
    data: FrozenDict[str, Any]
    path: str = ""

    def config(self, key: str) -> str:
        return f"`[{self.path}] {key}`"

    class __Required(Enum):
        VALUE = auto()

    def get_data(self, key: str, default: dict[str, Any] | __Required = __Required.VALUE) -> Data:
        data = self.get_value(key, expected_type=dict, default=default)
        return Data(
            source=self.source,
            data=FrozenDict(data),
            path=f"{self.path}.{key}" if self.path else key,
        )

    def get_str(self, key: str, default: str | __Required = __Required.VALUE) -> str:
        return self.get_value(key, expected_type=str, default=default)

    def get_int(self, key: str, default: int | __Required = __Required.VALUE) -> int:
        return self.get_value(key, expected_type=int, default=default)

    def get_bool(self, key: str, default: bool | __Required = __Required.VALUE) -> bool:
        return self.get_value(key, expected_type=bool, default=default)

    def get_list(
        self,
        key: str,
        expected_item_type: type[_T],
        default: list[_T] | __Required = __Required.VALUE,
    ) -> list[_T]:
        value = self.get_value(key, expected_type=list, default=default)
        invalid_entries = OrderedDict(
            (index, item)
            for index, item in enumerate(value, start=1)
            if not isinstance(item, expected_item_type)
        )
        if invalid_entries:
            invalid_items = [
                f"item {index}: {item} of type {type(item).__qualname__}"
                for index, item in invalid_entries.items()
            ]
            raise ValueError(
                f"Expected {self.config(key)} defined in {self.source} to be a list with items of "
                f"type {expected_item_type.__qualname__} but got {len(invalid_entries)} out of "
                f"{len(value)} entries of the wrong type:{os.linesep}"
                f"{os.linesep.join(invalid_items)}"
            )
        return value

    def get_data_list(
        self,
        key: str,
        default: list[dict] | __Required = __Required.VALUE,
    ) -> list[Data]:
        return [
            Data(
                source=self.source,
                data=FrozenDict(data),
                path=f"{self.path}.{key}[{index}]" if self.path else key,
            )
            for index, data in enumerate(
                self.get_list(key, expected_item_type=dict, default=default), start=1
            )
        ]

    def get_value(
        self, key: str, expected_type: type[_T], default: _T | __Required = __Required.VALUE
    ) -> _T:
        if key not in self.data:
            if default is self.__Required.VALUE:
                raise ValueError(
                    f"Expected {self.config(key)} of type {expected_type.__qualname__} to be "
                    f"defined in {self.source}."
                )
            return default

        value = self.data[key]
        if not isinstance(value, expected_type):
            raise ValueError(
                f"Expected a {expected_type.__qualname__} for {self.config(key)} but found {value} "
                f"of type {type(value).__qualname__} in {self.source}."
            )
        return value

    def __bool__(self):
        return bool(self.data)


def parse_config(content: BinaryIO, source: str) -> Application:
    return parse_config_data(Data(source=source, data=FrozenDict(tomllib.load(content))))


def parse_config_file(path: Path) -> Application:
    with path.open(mode="rb") as fp:
        return parse_config(fp, source=fp.name)


def parse_config_str(config: str) -> Application:
    return parse_config(BytesIO(config.encode()), source="<string>")


def parse_command(data: Data) -> Command:
    env = Env()
    if env_data := data.get_data("env", default={}):
        remove_exact = frozenset[str](
            env_data.get_list("remove", expected_item_type=str, default=list[str]())
        )
        remove_re = frozenset[str](
            env_data.get_list("remove_re", expected_item_type=str, default=list[str]())
        )
        replace = FrozenDict[str, str](env_data.get_data("replace", default={}).data)
        default = FrozenDict[str, str](env_data.get_data("default", default={}).data)
        env = Env(default=default, replace=replace, remove_exact=remove_exact, remove_re=remove_re)

    return Command(
        name=data.get_str("name", default="") or None,  # N.B.: Normalizes "" to None
        description=data.get_str("description", default="") or None,  # N.B.: Normalizes "" to None
        exe=data.get_str("exe"),
        args=tuple(data.get_list("args", expected_item_type=str, default=[])),
        env=env,
    )


def parse_version_field(data: Data) -> Version | None:
    return version.parse(version_str) if (version_str := data.get_str("version", "")) else None


def parse_digest_field(data: Data) -> Digest | None:
    return (
        Digest(
            size=digest_data.get_int("size"),
            fingerprint=Fingerprint(digest_data.get_str("fingerprint")),
        )
        if (digest_data := data.get_data("digest", {}))
        else None
    )


def parse_config_data(data: Data) -> Application:
    science = data.get_data("science")
    application_name = science.get_str("name")
    description = science.get_str("description", default="")
    load_dotenv = science.get_bool("load_dotenv", default=False)

    platforms = frozenset(
        Platform.parse(platform)
        for platform in science.get_list("platforms", expected_item_type=str, default=["current"])
    )
    if not platforms:
        raise ValueError(
            "There must be at least one platform defined for a science application. Leave "
            "un-configured to request just the current platform."
        )

    scie_jump = (
        ScieJump(
            version=parse_version_field(scie_jump_table), digest=parse_digest_field(scie_jump_table)
        )
        if (scie_jump_table := science.get_data("scie-jump", default={}))
        else ScieJump()
    )

    ptex = (
        Ptex(
            id=Identifier.parse(ptex_table.get_str("id", default="ptex")),
            argv1=ptex_table.get_str("lazy_argv1", default="{scie.lift}"),
            version=parse_version_field(ptex_table),
            digest=parse_digest_field(ptex_table),
        )
        if (ptex_table := science.get_data("ptex", {}))
        else None
    )

    interpreters_by_id = OrderedDict[str, Interpreter]()
    for interpreter in science.get_data_list("interpreters", default=[]):
        identifier = Identifier.parse(interpreter.get_str("id"))
        lazy = interpreter.get_bool("lazy", default=False)
        provider_name = interpreter.get_str("provider")
        if not (provider := get_provider(provider_name)):
            raise ValueError(f"The provider '{provider_name}' is not registered.")
        provider_config = {
            key: value
            for key, value in interpreter.data.items()
            if key not in ("id", "lazy", "provider")
        }
        interpreters_by_id[identifier.value] = Interpreter(
            id=identifier,
            provider=provider.create(identifier=identifier, lazy=lazy, **provider_config),
            lazy=lazy,
        )

    interpreter_groups = []
    for interpreter_group in science.get_data_list("interpreter_groups", default=[]):
        identifier = Identifier.parse(interpreter_group.get_str("id"))
        selector = interpreter_group.get_str("selector")
        members = interpreter_group.get_list("members", expected_item_type=str)
        if len(members) < 2:
            raise ValueError(
                f"At least two interpreter group members are needed to form an interpreter group. "
                f"Given {f'just {next(iter(members))!r}' if members else 'none'} for interpreter "
                f"group {identifier}."
            )
        interpreter_groups.append(
            InterpreterGroup.create(
                id_=identifier,
                selector=selector,
                interpreters=[interpreters_by_id[member] for member in members],
            )
        )

    if interpreter_groups and scie_jump.version and scie_jump.version < Version("0.11.0"):
        raise ValueError(
            f"Cannot use scie-jump {scie_jump.version}.{os.linesep}"
            f"This configuration uses interpreter groups and these require scie-jump v0.11.0 or "
            f"greater."
        )

    files = []
    for file in science.get_data_list("files", default=[]):
        file_name = file.get_str("name")
        digest = parse_digest_field(file)
        file_type = (
            FileType(file_type_name)
            if (file_type_name := file.get_str("type", default=""))
            else None
        )

        source: FileSource = None
        if source_name := file.get_str("source", default=""):
            match source_name:
                case "fetch":
                    source = "fetch"
                case file_name:
                    source = Binding(file_name)

        files.append(
            File(
                name=file_name,
                key=file.get_str("key", default="") or None,
                digest=digest,
                type=file_type,
                is_executable=file.get_bool("executable", default=False),
                eager_extract=file.get_bool("eager_extract", default=False),
                source=source,
            )
        )

    commands = [parse_command(command) for command in science.get_data_list("commands")]
    if not commands:
        raise ValueError("There must be at least one command defined in a science application.")

    bindings = [parse_command(command) for command in science.get_data_list("bindings", default=[])]

    return Application(
        name=application_name,
        description=description,
        load_dotenv=load_dotenv,
        platforms=platforms,
        scie_jump=scie_jump,
        ptex=ptex,
        interpreters=tuple(interpreters_by_id.values()),
        interpreter_groups=tuple(interpreter_groups),
        files=tuple(files),
        commands=frozenset(commands),
        bindings=frozenset(bindings),
    )
