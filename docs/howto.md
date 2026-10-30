# Guide pratique

Tâches courantes, pas à pas. Pour le détail du fonctionnement et des
garde-fous, voir la [référence](./reference.md).

Les commandes s'exécutent en local, à la racine du dépôt. Les tokens sont
dans `$HOME/.config/fdr-irve/secrets.env`, jamais dans le dépôt.

## Prérequis n8n

À préparer une seule fois, dans le **même projet n8n** que le workflow.

### 1. Deux credentials Header Auth

| Credential | Nom du header | Valeur |
|---|---|---|
| Airtable | `Authorization` | `Bearer <token Airtable>` |
| Grist | `Authorization` | `Bearer <token Grist>` |

- Token Airtable : scopes `schema.bases:read` et `data.records:read`.
- Token Grist : droits d'écriture sur le document cible. Utiliser les services accounts pour limiter le scope. (Possible d'utiliser https://gitlab.com/multi-coop/grist-service-account/ pour la gestion des service accounts)

### 2. Une Data Table d'état

Créer une Data Table, par exemple `irve_sync_state`, avec **deux colonnes
de type String** :

| Colonne | Contenu |
|---|---|
| `sync_key` | identifiant de la synchronisation |
| `payload` | état JSON, sous forme de texte |

Ajouter **une seule ligne** :

- `sync_key` : la valeur `state_key` du nœud **Configuration** du workflow,
  actuellement
  `appLzJVf0KpaEishq:tblfVBBQ18y1EE622:cK74vdT499GNpN2Gm2diUS:Enquete_2026` ;
- `payload` : `{"version":1,"cursor":null,"retry_ids":[],"mapping_fingerprint":""}`.

Ne pas créer de colonne `id` : n8n la gère.

### 3. Relever l'ID de la Data Table

Ouvrir la Data Table dans n8n : son ID est le **dernier segment de l'URL**,
après `/datatables/`. Par exemple, dans
`https://n8n.example.org/projects/abc123/datatables/Pjsqn01dKtgTAiHN`,
l'ID est `Pjsqn01dKtgTAiHN`.

## Déployer sur n8n

1. **Régénérer le workflow** si le mapping ou la configuration a changé :

   ```sh
   uv run python -m fdr_irve export-n8n --language javascript --overwrite
   ```

2. **Importer** [airtable-to-grist.javascript.workflow.json](../n8n/airtable-to-grist.javascript.workflow.json)
   avec « Import from File ». Si une ancienne version existe, la désactiver
   d'abord : un seul workflow actif à la fois.

3. **Configurer** :
   - nœud **Configuration** : remplacer `REPLACE_WITH_STATE_TABLE_ID` par
     l'ID de la Data Table ;
   - nœuds `Schema Airtable` et `Lire Airtable` : credential Airtable ;
   - tous les autres nœuds HTTP Request (Grist) : credential Grist.

4. **Tester en simulation** : exécuter manuellement le workflow entier,
   sans l'activer. Vérifier les compteurs du nœud **Bilan**.

5. **Passer en écriture** : sauvegarder le document Grist, désactiver le
   planning, puis dans **Configuration** :

   ```json
   "mode": "write",
   "write_confirmation": "WRITE_AIRTABLE_TO_GRIST",
   "serialized_executions_confirmed": true,
   "modified_field_coverage_confirmed": true
   ```

   Exécuter une fois manuellement et vérifier le résultat dans Grist.

6. **Activer** le workflow pour l'exécution toutes les 30 minutes.

Après chaque réimport, les étapes 3 à 6 sont à refaire : l'export ne
contient ni credentials, ni ID de Data Table, ni mode écriture.

## Changer de document Grist

Cas où seul l'identifiant du document change (même structure de tables).
L'identifiant est le segment qui suit le domaine dans l'URL Grist :
`https://grist.francedatareseau.fr/<document_id>/...`.

1. Vérifier que le token Grist a accès au nouveau document.

2. Dans [config.toml](../config.toml), remplacer `document_id` dans `[grist]`.

3. Relire les schémas dans un **nouveau dossier** (ici `revision-5`) :

   ```sh
   uv run --env-file "$HOME/.config/fdr-irve/secrets.env" \
     python -m fdr_irve discover --output-dir generated/revision-5
   ```

4. Dans [mapping.decisions.json](../mapping.decisions.json), mettre
   `reviewed_schema` à `generated/revision-5/schema.json`.

5. Simuler, sans écriture, dans un nouveau dossier :

   ```sh
   uv run --env-file "$HOME/.config/fdr-irve/secrets.env" \
     python -m fdr_irve simulate \
     --proposal generated/revision-5/mapping.proposed.json \
     --output-dir generated/simulation-3
   ```

   Contrôler les compteurs affichés (créations, mises à jour, anomalies).

6. Régénérer le workflow :

   ```sh
   uv run python -m fdr_irve export-n8n --language javascript \
     --proposal generated/revision-5/mapping.proposed.json --overwrite
   ```

7. Dans n8n :
   - réimporter et reconfigurer le workflow (voir [Déployer sur n8n](#déployer-sur-n8n)) ;
   - ajouter une **nouvelle ligne** dans la Data Table d'état, avec la
     nouvelle `state_key` du nœud Configuration et le `payload` initial.
     L'ancienne ligne peut rester, elle n'est plus lue.

Le premier passage sur le nouveau document est une synchronisation complète.

Ne pas se contenter de modifier l'URL dans les nœuds n8n : la clé d'état et
l'empreinte du schéma ne correspondraient plus.

Si la structure du document diffère (colonnes ajoutées, renommées ou
supprimées), le mapping doit être revu : voir la
[référence](./reference.md#validation-humaine-du-mapping).

## Dépannage

| Message | Cause probable |
|---|---|
| `Renseigner state_table_id…` | ID de Data Table non renseigné dans Configuration. |
| `Créer exactement une ligne d'état…` | Ligne absente ou en double, `sync_key` différente de `state_key`, mauvaise Data Table ou autre projet n8n. |
| `Écriture refusée…` | Une des trois confirmations du mode écriture manque. |
| Dérive de schéma | La structure Airtable ou Grist a changé : relancer la découverte et revoir le mapping. |
