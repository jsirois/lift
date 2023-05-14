# Copyright 2022 Science project contributors.
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator

import click
import click_log
from packaging import version

from science import __version__, a_scie, lift
from science.config import parse_config
from science.model import Application, Command, Distribution, File, Url
from science.platform import Platform


@click.group(
    context_settings=dict(auto_envvar_prefix="SCIENCE", help_option_names=["-h", "--help"])
)
@click.version_option(__version__, "-V", "--version", message="%(version)s")
def _main() -> None:
    """Science helps you prepare scies for your application.

    Science provides a high-level configuration file format for a scie application and can build
    scies and export scie lift manifests from these configuration files.

    For more information on the configuration file format, see:
    https://github.com/a-scie/lift/blob/main/docs/manifest.md
    """
    click_log.basic_config()


@dataclass(frozen=True)
class FileMapping:
    @classmethod
    def parse(cls, value: str) -> FileMapping:
        components = value.split("=", 1)
        if len(components) != 2:
            raise ValueError(
                "Invalid file mapping. A file mapping must be of the form "
                f"`(<name>|<key>)=<path>`: {value}"
            )
        return cls(id=components[0], path=Path(components[1]))

    id: str
    path: Path


@contextmanager
def _temporary_directory(cleanup: bool) -> Iterator[Path]:
    if cleanup:
        with tempfile.TemporaryDirectory() as td:
            yield Path(td)
    else:
        yield Path(tempfile.mkdtemp())


def _export(
    application: Application,
    file_mappings: list[FileMapping],
    dest_dir: Path,
    *,
    force: bool = False,
    platforms: Iterable[Platform] | None = None,
    include_provenance: bool = False,
) -> Iterator[tuple[Platform, Path]]:
    for platform in platforms or application.platforms:
        chroot = dest_dir / platform.value
        if force:
            shutil.rmtree(chroot, ignore_errors=True)
        chroot.mkdir(parents=True, exist_ok=False)

        bindings: list[Command] = []
        distributions: list[Distribution] = []
        files: list[File] = []
        file_paths_by_id = {
            file_mapping.id: file_mapping.path.resolve() for file_mapping in file_mappings
        }
        fetch_urls: dict[str, str] = {}
        fetch = any("fetch" == file.source for file in application.files)
        fetch |= any(interpreter.lazy for interpreter in application.interpreters)
        if fetch:
            ptex = a_scie.ptex(chroot, specification=application.ptex, platform=platform)
            file_paths_by_id[ptex.id] = chroot / ptex.name
            files.append(ptex)
            argv1 = (
                application.ptex.argv1
                if application.ptex and application.ptex.argv1
                else "{scie.lift}"
            )
            bindings.append(Command(name="fetch", exe=ptex.placeholder, args=tuple([argv1])))
        bindings.extend(application.bindings)

        for interpreter in application.interpreters:
            distribution = interpreter.provider.distribution(platform)
            if distribution:
                distributions.append(distribution)
                files.append(distribution.file)
                match distribution.source:
                    case Url(url):
                        fetch_urls[distribution.file.name] = url
                    case path:
                        file_paths_by_id[distribution.file.id] = path
        files.extend(application.files)

        for file in files:
            if file.source is None:
                path = file_paths_by_id.get(file.id) or Path.cwd() / file.name
                if not path.exists():
                    raise ValueError(f"The file for {file.id} is not mapped or cannot be found.")
                file.maybe_check_digest(path)
                target = chroot / file.name
                if not target.exists():
                    target.symlink_to(path)

        lift_manifest = chroot / "lift.json"

        build_info = dict[str, Any]()
        if include_provenance:
            build_info.update(
                note="Generated by science.",
                version=__version__,
                url=(
                    f"https://github.com/a-scie/lift/releases/tag/v{__version__}/"
                    f"{Platform.current().qualified_binary_name('science')}"
                ),
            )

        with open(lift_manifest, "w") as lift_manifest_output:
            lift.emit_manifest(
                lift_manifest_output,
                name=application.name,
                description=application.description,
                load_dotenv=application.load_dotenv,
                scie_jump=application.scie_jump,
                platform=platform,
                distributions=distributions,
                interpreter_groups=application.interpreter_groups,
                files=files,
                commands=application.commands,
                bindings=bindings,
                fetch_urls=fetch_urls,
                build_info=build_info,
            )
        yield platform, lift_manifest


@_main.command()
@click.argument("config", type=click.File("rb"))
@click.option(
    "--file",
    "file_mappings",
    type=FileMapping.parse,
    multiple=True,
    default=[],
    envvar="SCIENCE_EXPORT_FILE",
)
@click.option("--dest-dir", type=Path, default=Path.cwd())
@click.option("--force", is_flag=True)
@click.option("--include-provenance", is_flag=True)
def export(
    config: BinaryIO,
    file_mappings: list[FileMapping],
    dest_dir: Path,
    force: bool,
    include_provenance: bool,
) -> None:
    """Export the application configuration as one or more scie lift manifests."""
    application = parse_config(config, source=config.name)
    for _, lift_manifest in _export(
        application, file_mappings, dest_dir, force=force, include_provenance=include_provenance
    ):
        click.echo(lift_manifest)


@_main.command()
@click.argument("config", type=click.File("rb"))
@click.option(
    "--file",
    "file_mappings",
    type=FileMapping.parse,
    multiple=True,
    default=[],
    envvar="SCIENCE_BUILD_FILE",
)
@click.option("--dest-dir", type=Path, default=Path.cwd())
@click.option("--preserve-sandbox", is_flag=True)
@click.option("--use-jump", type=Path)
@click.option("--include-provenance", is_flag=True)
@click.option(
    "--hash",
    "hash_functions",
    type=click.Choice(sorted(hashlib.algorithms_guaranteed)),
    multiple=True,
    default=[],
    envvar="SCIENCE_BUILD_HASH",
)
@click.option("--use-platform-suffix", is_flag=True)
def build(
    config: BinaryIO,
    file_mappings: list[FileMapping],
    dest_dir: Path,
    preserve_sandbox: bool,
    use_jump: Path | None,
    include_provenance: bool,
    hash_functions: list[str],
    use_platform_suffix: bool,
) -> None:
    """Build the application executable(s)."""
    application = parse_config(config, source=config.name)

    current_platform = Platform.current()
    platforms = application.platforms
    use_platform_suffix = use_platform_suffix or platforms != frozenset([current_platform])
    if use_jump and use_platform_suffix:
        click.secho(
            f"Cannot use a custom scie jump build with a multi-platform configuration.", fg="yellow"
        )
        click.secho(
            "Restricting requested platforms of "
            f"{', '.join(platform.value for platform in platforms)} to "
            f"{current_platform.value}",
            fg="yellow",
        )
        platforms = frozenset([current_platform])

    scie_jump_version = application.scie_jump.version if application.scie_jump else None
    if scie_jump_version and scie_jump_version < version.parse("0.9.0"):
        # N.B.: The scie-jump 0.9.0 or later is needed to support cross-building against foreign
        # platform scie-jumps with "-sj".
        sys.exit(
            f"A scie-jump version of {scie_jump_version} was requested but {sys.argv[0]} "
            f"requires at least 0.9.0."
        )

    native_jump_path = (
        a_scie.custom_jump(repo_path=use_jump)
        if use_jump
        else a_scie.jump(platform=current_platform)
    )
    with _temporary_directory(cleanup=not preserve_sandbox) as td:
        for platform, lift_manifest in _export(
            application,
            file_mappings,
            dest_dir=td,
            platforms=platforms,
            include_provenance=include_provenance,
        ):
            jump_path = (
                a_scie.custom_jump(repo_path=use_jump)
                if use_jump
                else a_scie.jump(specification=application.scie_jump, platform=platform)
            )
            platform_export_dir = lift_manifest.parent
            subprocess.run(
                args=[str(native_jump_path), "-sj", str(jump_path), lift_manifest],
                cwd=platform_export_dir,
                stdout=subprocess.DEVNULL,
                check=True,
            )
            src_binary_name = current_platform.binary_name(application.name)
            dst_binary_name = (
                platform.qualified_binary_name(application.name)
                if use_platform_suffix
                else platform.binary_name(application.name)
            )
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst_binary = dest_dir / dst_binary_name
            shutil.move(src=platform_export_dir / src_binary_name, dst=dst_binary)
            if hash_functions:
                digests = tuple(hashlib.new(hash_function) for hash_function in hash_functions)
                with dst_binary.open(mode="rb") as fp:
                    for chunk in iter(lambda: fp.read(io.DEFAULT_BUFFER_SIZE), b""):
                        for digest in digests:
                            digest.update(chunk)
                for digest in digests:
                    dst_binary.with_name(f"{dst_binary.name}.{digest.name}").write_text(
                        f"{digest.hexdigest()} *{dst_binary_name}{os.linesep}"
                    )
            click.echo(dst_binary)


def main():
    # By default, click help messages expose the fact the app is written in Python. The resulting
    # program name (`python -m module` or `__main__.py`) is both confusing and unusable for the end
    # user since both the Python distribution and the code are hidden away in the nce cache. Since
    # we know we run as a scie in normal circumstances, use the SCIE_ARGV0 exported by the
    # scie-jump when present.
    _main(prog_name=os.environ.get("SCIE_ARGV0"))
