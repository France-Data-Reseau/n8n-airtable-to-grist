"""Schema discovery, read-only simulation and n8n workflow export."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .approved import compile_mapping, load_json
from .discovery import Config, DiscoveryError, discover
from .mapping import build_mapping
from .n8n_workflow import build_workflow
from .simulation import simulate


def write_json(path: Path, payload: dict[str, Any], overwrite: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Préparer la synchronisation Airtable vers Grist.")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("discover", help="Lire les schémas et proposer un mapping.")
    command.add_argument("--config", type=Path, default=Path("config.toml"))
    command.add_argument("--output-dir", type=Path, default=Path("generated"))
    simulation_command = commands.add_parser(
        "simulate", help="Comparer tous les enregistrements, sans écriture distante."
    )
    simulation_command.add_argument("--config", type=Path, default=Path("config.toml"))
    simulation_command.add_argument(
        "--proposal", type=Path, default=Path("generated/revision-3/mapping.proposed.json")
    )
    simulation_command.add_argument("--decisions", type=Path, default=Path("mapping.decisions.json"))
    simulation_command.add_argument(
        "--output-dir", type=Path, default=Path("generated/simulation-1")
    )
    export_command = commands.add_parser(
        "export-n8n", help="Générer le workflow n8n sans effectuer d'appel API."
    )
    export_command.add_argument("--config", type=Path, default=Path("config.toml"))
    export_command.add_argument(
        "--proposal", type=Path, default=Path("generated/revision-3/mapping.proposed.json")
    )
    export_command.add_argument("--decisions", type=Path, default=Path("mapping.decisions.json"))
    export_command.add_argument("--output-dir", type=Path, default=Path("n8n"))
    export_command.add_argument(
        "--language", choices=("python", "javascript"), default="python",
        help="Langage des nœuds de traitement dans n8n."
    )
    export_command.add_argument(
        "--overwrite", action="store_true", help="Remplacer uniquement le workflow généré existant."
    )
    args = parser.parse_args(argv)
    names = (
        ("schema.json", "mapping.proposed.json") if args.command == "discover"
        else (
            ("airtable-to-grist.javascript.workflow.json",)
            if args.language == "javascript" else ("airtable-to-grist.workflow.json",)
        ) if args.command == "export-n8n"
        else ("mapping.approved.json", "simulation.json")
    )
    paths = [args.output_dir / name for name in names]
    overwrite = args.command == "export-n8n" and args.overwrite
    report: dict[str, Any] | None = None
    try:
        if not overwrite and any(path.exists() for path in paths):
            raise DiscoveryError(
                "Sortie déjà présente : choisir un autre --output-dir pour préserver le mapping."
            )
        config = Config.load(args.config)
        if args.command == "discover":
            schema = discover(config)
            mapping = build_mapping(config, schema)
            schema["discovered_at"] = datetime.now(timezone.utc).isoformat()
            payloads = (schema, mapping)
        else:
            proposal = load_json(args.proposal)
            decisions = load_json(args.decisions)
            schema_path = decisions.get("reviewed_schema")
            if not isinstance(schema_path, str) or not schema_path:
                raise DiscoveryError("Le chemin du schéma validé est absent des décisions.")
            schema = load_json(args.decisions.parent / schema_path)
            if args.command == "export-n8n":
                mapping = compile_mapping(config, schema, proposal, decisions)
                payloads = (build_workflow(mapping, schema, language=args.language),)
            else:
                mapping, report = simulate(config, schema, proposal, decisions)
                payloads = (mapping, report)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for path, payload in zip(paths, payloads):
            write_json(path, payload, overwrite=overwrite)
    except DiscoveryError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("Erreur : impossible de lire ou d'écrire les fichiers locaux.", file=sys.stderr)
        return 1
    if args.command == "export-n8n":
        print(f"Workflow n8n désactivé, en simulation : {paths[0]}")
        print("Aucun appel API ; aucun credential exporté.")
        return 0
    if report is not None:
        print(f"Mapping validé pour simulation : {paths[0]}")
        print(f"Rapport local confidentiel : {paths[1]}")
        print(json.dumps(report["summary"], ensure_ascii=False))
        print("Aucune écriture distante ; aucun curseur avancé.")
        return 2 if report["errors"] or report["grist_key_anomalies"] else 0
    print(f"Schémas : {paths[0]}")
    print(f"Mapping non validé : {paths[1]}")
    print(f"{len(mapping['fields'])} champs à examiner ; aucune donnée lue ou écrite.")
    for message in mapping["warnings"]:
        print(f"ATTENTION : {message}")
    for message in mapping["blockers"]:
        print(f"BLOCAGE : {message}", file=sys.stderr)
    return 2 if mapping["blockers"] else 0


if __name__ == "__main__":
    sys.exit(main())
