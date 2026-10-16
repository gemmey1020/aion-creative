"""Command-line entry points for Ion Creative."""

import argparse
import asyncio
import json
import sys

from ion_creative.adapters.artcraft import ArtCraftAdapter
from ion_creative.capabilities.catalog import collect_live_capabilities
from ion_creative.desktop.launchers import install_launchers, list_launchers, verify_launchers
from ion_creative.runtime.doctor import doctor_engine
from ion_creative.runtime.install import install_runtime
from ion_creative.runtime.registry import load_runtime_specs


def _versions() -> int:
    specs = load_runtime_specs()
    statuses = {engine: doctor_engine(engine) for engine in specs}
    payload = {
        engine: {
            "pinned_version": spec.version,
            "observed_version": statuses[engine].observed_version,
            "healthy": statuses[engine].healthy,
        }
        for engine, spec in specs.items()
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _doctor(engine: str | None) -> int:
    specs = load_runtime_specs()
    selected = [engine] if engine else list(specs)
    statuses = [doctor_engine(name) for name in selected]
    print(json.dumps([status.to_dict() for status in statuses], indent=2, sort_keys=True))
    return 0 if all(status.healthy for status in statuses) else 1


def _install(engine: str | None, install_all: bool, force: bool) -> int:
    specs = load_runtime_specs()
    engines = list(specs) if install_all else [engine]
    statuses = [install_runtime(specs[name], force=force) for name in engines if name]
    print(json.dumps([status.to_dict() for status in statuses], indent=2, sort_keys=True))
    return 0 if statuses and all(status.healthy for status in statuses) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ion")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("versions", help="Show pinned and observed runtime versions")
    doctor = commands.add_parser("doctor", help="Check pinned runtime executables")
    doctor.add_argument("engine", nargs="?", choices=tuple(load_runtime_specs()))
    runtime = commands.add_parser("runtime", help="Manage pinned Craft runtimes")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    install = runtime_commands.add_parser("install", help="Install a pinned runtime")
    install.add_argument("engine", nargs="?", choices=tuple(load_runtime_specs()))
    install.add_argument(
        "--all", dest="install_all", action="store_true", help="Install all pinned runtimes"
    )
    install.add_argument("--force", action="store_true", help="Reinstall the exact pinned version")
    desktop = commands.add_parser("desktop", help="Manage user-local desktop launchers")
    desktop_commands = desktop.add_subparsers(dest="desktop_command", required=True)
    desktop_commands.add_parser("install-launchers", help="Install Ion-managed Craft launchers")
    desktop_commands.add_parser("list-launchers", help="List Ion-managed Craft launchers")
    desktop_commands.add_parser("verify-launchers", help="Verify installed Craft launchers")
    capabilities = commands.add_parser("capabilities", help="Discover live Craft capabilities")
    capability_commands = capabilities.add_subparsers(dest="capability_command", required=True)
    capability_commands.add_parser("list", help="List normalized live capabilities")
    dump = capability_commands.add_parser("dump", help="Dump full schemas for one engine")
    dump.add_argument("--engine", required=True, choices=tuple(load_runtime_specs()))
    artcraft = commands.add_parser("artcraft", help="Inspect optional ArtCraft configuration")
    artcraft_commands = artcraft.add_subparsers(dest="artcraft_command", required=True)
    artcraft_commands.add_parser("status", help="Report credential state without generating media")
    args = parser.parse_args(argv)
    if args.command == "versions":
        return _versions()
    if args.command == "doctor":
        return _doctor(args.engine)
    if args.command == "runtime" and args.runtime_command == "install":
        if args.install_all == bool(args.engine):
            parser.error("choose one engine or --all")
        return _install(args.engine, args.install_all, args.force)
    if args.command == "desktop":
        if args.desktop_command == "install-launchers":
            statuses = install_launchers()
            print(json.dumps(statuses, indent=2, sort_keys=True))
            return 0 if statuses and all(status.get("installed") for status in statuses) else 1
        if args.desktop_command == "list-launchers":
            print(json.dumps(list_launchers(), indent=2, sort_keys=True))
            return 0
        if args.desktop_command == "verify-launchers":
            statuses = verify_launchers()
            print(json.dumps(statuses, indent=2, sort_keys=True))
            return 0 if statuses and all(status.get("verified") for status in statuses) else 1
    if args.command == "capabilities":
        engine = args.engine if args.capability_command == "dump" else None
        payload = asyncio.run(
            collect_live_capabilities(
                engine=engine,
                include_schemas=args.capability_command == "dump",
            )
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["status"] != "INVALID_ENGINE" else 1
    if args.command == "artcraft" and args.artcraft_command == "status":
        status = ArtCraftAdapter().configuration_status()
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
