# Workflow n8n de synchronisation Airtable vers Grist

Synchronisation unidirectionnelle d'une table Airtable vers une table Grist,
exécutée dans n8n avec des nœuds HTTP Request : créations et mises à jour
rapprochées via la colonne Grist `Airtable_Id`, sans suppression.

Ce dépôt découvre les schémas, propose un mapping à valider, simule la
synchronisation en lecture seule et génère le workflow n8n à importer.

**Le workflow généré et versionné ici concerne la synchronisation de l'enquête IRVE 2026, dont les données sont maintenues dans Airtable et doivent être répercutées dans Grist pour être ingérées par Superset.**

## Motivation

Pour le cas d'usage FDR-IRVE, un questionnaire a été réalisé sur Airtable Fillout. Les réponses sont enregistrées dans Airtable. 
La valorisation de l'enquête est réalisée sur Superset. 
Pour faire le lien entre Airtable et Superset, il a été décidé de passer via Grist :

- Grist permet l'import d'une base Airtable, mais ne permet pas une synchronisation continue.
- n8n dispose de connecteurs pour Airtable et Grist. Cependant, ces connecteurs ont de nombreuses limitations:
  - Il n'y a pas de mode "discovery" qui permet de découvrir automatiquement la structure des tables et des colonnes.
  - Les connecteurs ne gèrent pas nativement la synchronisation continue des données.
  - Il n'y a pas de gestion intégrée des conflits ou des erreurs de synchronisation.

L'interface de n8n n'était donc de base pas adaptée pour gérer la synchronisation d'une table préexistante complexe avec plus de 120 champs. L'utilisation d'un LLM pour générer automatiquement le mapping entre les colonnes Airtable et Grist permet de surmonter cette limitation.

## Documentation

- [Guide pratique](./docs/howto.md) : prérequis n8n, déploiement, changement
  de document Grist.
- [Référence](./docs/reference.md) : découverte, validation du mapping,
  simulation, fonctionnement détaillé du workflow, garde-fous et règles de
  synchronisation.

## Démarrage rapide

```sh
uv sync
uv run python -m unittest discover -s tests -q
```

Les tokens se placent hors du dépôt, dans
`$HOME/.config/fdr-irve/secrets.env` (voir la [référence](./docs/reference.md#installation-et-secrets)).
