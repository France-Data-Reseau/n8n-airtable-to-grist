# Référence : Airtable vers Grist

Documentation détaillée de la synchronisation unidirectionnelle dans n8n :
créations et mises à jour via `Airtable_Id`, sans suppression.
Pour les tâches courantes, voir le [guide pratique](./howto.md).

## Étape 1 : découverte et proposition de mapping

Cette étape est implémentée en Python, avec uv et sans dépendance externe.
Elle effectue uniquement des requêtes GET sur les **schémas** :

- tables et champs de la base Airtable ;
- tables et colonnes du document Grist, y compris les tables nécessaires à
  l'examen des références.

Elle ne lit aucun enregistrement métier et ne modifie aucune base.
Le schéma inclut les métadonnées des champs (types, options, formules et références)
renvoyées par les API ; il doit être traité comme potentiellement confidentiel.
Seuls les champs de la paire de tables configurée font l'objet du mapping.

### Installation et secrets

Prérequis : uv, Python 3.11 ou supérieur, accès réseau HTTPS aux deux services.

```sh
uv sync
```

Les identifiants des bases et tables sont dans [config.toml](../config.toml).
Les tokens ne doivent jamais être ajoutés à ce fichier ni au dépôt.

Copier le modèle [.env.example](../.env.example) **hors du dépôt**, par exemple :

```sh
mkdir -p "$HOME/.config/fdr-irve"
cp .env.example "$HOME/.config/fdr-irve/secrets.env"
chmod 600 "$HOME/.config/fdr-irve/secrets.env"
```

Renseigner `AIRTABLE_TOKEN` et `GRIST_TOKEN` dans cette copie, avec un éditeur
local, sans les coller dans le terminal, le chat ou les logs.
Le fichier utilise la syntaxe `NOM=valeur`, sans `export`.

- Airtable : scopes `schema.bases:read` et `data.records:read`, accès à la base.
  La découverte n'utilise que le premier scope.
- Grist : accès au document configuré. Le token d'écriture convient, mais
  aucune écriture n'est effectuée par ce script.

### Exécution

```sh
uv run --env-file "$HOME/.config/fdr-irve/secrets.env" \
  python -m fdr_irve discover --config config.toml --output-dir generated
```

Les variables peuvent aussi être fournies par l'environnement du processus.
Les requêtes expirent après 30 secondes ; les redirections sont refusées pour
ne pas transmettre les tokens à un autre destinataire. Une erreur HTTP, réseau
ou de schéma est signalée, sans afficher le token ni le corps de la réponse.
Il n'y a pas de nouvelle tentative automatique pendant la découverte.

Sorties locales, ignorées par Git :

- `generated/schema.json` : schémas des deux bases ;
- `generated/mapping.proposed.json` : proposition **non validée**.

Le script refuse d'écraser ces fichiers. Pour refaire une découverte, choisir
un autre dossier sous `generated/`, par exemple `--output-dir generated/revision-2`.
Les schémas sont lus successivement : ne pas modifier la structure des bases
pendant la découverte.

Codes de sortie : `0` découverte réussie (mapping toujours à valider), `1` erreur
technique, `2` fichiers générés mais prérequis bloquants détectés dans le mapping.

### Validation humaine du mapping

Les suggestions se fondent sur le nom Airtable et le libellé ou l'identifiant
Grist, en ignorant casse, accents et ponctuation, sauf les signes `<`, `>` et `=`
qui distinguent notamment les tranches de puissance. Ce n'est pas une validation
sémantique : tous les indicateurs `approved` restent à `false`.

Examiner chaque entrée `fields` :

1. Confirmer les identifiants source et cible et les unités des nombres.
2. Résoudre les statuts `unmatched`, `ambiguous`, `requires_configuration` et
   `read_only`. Deux champs source ne doivent pas écrire dans la même colonne.
3. Vérifier les sélections et leurs choix autorisés.
4. Pour les références, choisir la clé de rapprochement dans la table liée
   et la cardinalité ; **un ID Airtable n'est pas un identifiant de ligne Grist**.
5. Ne pas écrire dans les colonnes calculées. Examiner les formules de
   déclenchement éventuelles des colonnes de données.
6. Vérifier les `blockers`, puis confirmer `record_key` et les champs retenus.

La colonne Grist de rapprochement, identifiée comme `Airtable_Id` dans le
document cible, doit être de type `Text` et modifiable.
Le champ Airtable `updatedat` doit être de type `lastModifiedTime` et surveiller
tous les champs utiles. Sa couverture exacte reste à vérifier dans Airtable.
Les changements de formules, de lookups ou de données dans les tables liées
peuvent ne pas modifier cet horodatage : leur stratégie sera arrêtée après
examen du schéma.

Conserver la proposition originale. Les décisions de revue sont versionnées
séparément, sans tokens ni données d'enregistrements. Le moteur de simulation
compile ces décisions avec la proposition et implémente les conversions
retenues. Passer `approved` à `true` ne déclenche aucune action distante.
Le workflow n8n est fourni séparément, désactivé et avec ses écritures bloquées
par défaut.

### Décisions de revue en cours

Le fichier [mapping.decisions.json](../mapping.decisions.json) consigne les
décisions humaines, sans modifier la proposition originale. La commande
`simulate` le charge et vérifie son périmètre ; la commande de découverte
ne le charge pas et ne l'applique pas.

Après suppression de `AODE 2` et `AODE 3`, le schéma observé comporte 120
correspondances simples, 9 colonnes Grist calculées, une référence `AODE`,
un bouton sans cible et le champ technique `updatedat`.

La révision 4 cible le document Grist `cK74vdT499GNpN2Gm2diUS`, de même
structure. Seules les colonnes de données `AODE_2` et `AODE_3`, absentes
d'Airtable, y subsistent : elles ne sont pas mappées et ne sont jamais écrites.

Décisions confirmées :

- conserver tel quel le Markdown des trois champs `Fabricant(s) BdR` ;
- conserver l'instant de `date_d'envoi_du_formulaire`, avec `Europe/Paris`
  comme repli seulement pour une valeur sans fuseau ;
- résoudre la liste `AODE` via `AODE.Airtable_Id`, sans écriture dans la
  table liée ; signaler et retenter la ligne d'enquête en cas de clé liée
  absente ou en doublon.

Les neuf colonnes calculées sont exclues des écritures de synchronisation :

- conserver les formules existantes de `Sigle`, `Panel`, `Siren`, `Type`,
  `Departement` et `Region` ;
- ne rien modifier pour `AODE_Amenageur`, `Repondant` et `Declencheur_envoi`,
  y compris leurs formules actuellement commentées.

Le périmètre global est validé pour une **simulation en lecture seule** :
120 champs directs et la référence AODE. Cela n'autorise ni une écriture
de données ni une modification de la structure Grist.

## Étape 2 : simulation complète sans écriture

```sh
uv run --env-file "$HOME/.config/fdr-irve/secrets.env" \
  python -m fdr_irve simulate \
  --proposal generated/revision-4/mapping.proposed.json \
  --decisions mapping.decisions.json \
  --output-dir generated/simulation-2
```

Le chemin du schéma examiné est dans `reviewed_schema` des décisions,
relatif au dossier du fichier de décisions. Utiliser la proposition de la
révision 4, pas une proposition antérieure qui cible l'ancien document.
Chaque nouvelle exécution nécessite un dossier de sortie distinct.

La simulation :

1. Vérifie la validation globale, le nombre de champs, la clé et les exclusions.
2. Relit les schémas distants et refuse les changements du contrat examiné
   (types, options, choix, formules, clés de références).
3. Lit toutes les pages Airtable, uniquement pour les champs retenus et
   `updatedat`, avec les identifiants de champ stables.
4. Lit les enregistrements Grist de l'enquête et de la table liée AODE.
5. Calcule les créations potentielles et les seules différences à mettre à jour.
   Une ligne comportant une erreur est entièrement écartée, sans plan partiel.

Les appels HTTP sont exclusivement des GET. Aucune donnée n'est écrite,
aucun curseur n'est avancé et aucune Data Table n8n n'est modifiée.
Les limites API sont signalées sans nouvelle tentative automatique ; la
pagination Airtable espace les appels de 210 ms.

Conversions et garde-fous :

- nombres, montants en euros et pourcentages conservés sans arrondi ni mise
  à l'échelle (`0.25` représente `25 %`) ;
- Markdown conservé ; choix contrôlés contre les listes du mapping ;
- listes Grist encodées comme `["L", ...]`, références résolues en IDs
  numériques de lignes Grist, jamais en IDs Airtable ;
- date seule à minuit UTC en secondes Unix ; horodatage conservé en secondes
  Unix, avec repli `Europe/Paris` pour une valeur sans fuseau ;
- une heure locale ambiguë ou inexistante au changement d'heure est signalée
  plutôt qu'interprétée arbitrairement ;
- champ source absent ignoré ; vide explicite converti en `""` pour texte/choix,
  `false` pour booléen et `null` pour nombre, date ou liste ;
- clés en doublon, références introuvables, valeurs masquées ou erreurs sur
  un champ cible retenu : ligne écartée et ajoutée aux reprises ;
- les lignes Grist sans clé valide restent inchangées et sont signalées ;
  aucun rapprochement automatique ni suppression.

Sorties locales créées avec des permissions `0600` :

- `mapping.approved.json` : mapping compilé, autorisé pour simulation seulement ;
- `simulation.json` : bilan, créations et mises à jour proposées, anciennes et
  nouvelles valeurs des cellules modifiées, erreurs et IDs à retenter.

**Le rapport contient des données métier potentiellement personnelles.**
Ne pas le versionner ni le partager sans revue. Les sorties sont ignorées
par Git ; le terminal affiche uniquement des compteurs.
Les instantanés bruts des enregistrements ne sont pas sauvegardés.

Code de sortie `0` : comparaison terminée sans anomalie ; `1` : erreur technique
ou mapping obsolète ; `2` : rapport généré avec lignes écartées ou anomalies de
clés Grist. Des propositions de mise à jour seules ne constituent pas une erreur.
Les compteurs `missing_modified_timestamp` doivent être examinés avant le
passage à l'incrémental.

Limites : les lectures ne sont pas transactionnelles entre les deux bases ;
le rapport n'est pas un plan à appliquer ultérieurement sans nouvelle lecture.
Les calculs Grist exclus ne sont pas audités, et la couverture de `updatedat`
reste à confirmer dans Airtable. La file de reprise du rapport n'est pas
encore persistée dans n8n.

## Étape 3 : workflow n8n importable

**Version JavaScript, sans Python dans n8n** :
[n8n/airtable-to-grist.javascript.workflow.json](../n8n/airtable-to-grist.javascript.workflow.json).
Elle ne nécessite aucun module npm, aucune autorisation d'import et aucun
runner Python. Les dates sans fuseau utilisent `Intl` et les données IANA du
runtime JavaScript pour `Europe/Paris` ; les heures ambiguës ou inexistantes
aux changements d'heure sont rejetées.

L'ancienne [version Python](../n8n/airtable-to-grist.workflow.json) reste disponible.
Les deux versions embarquent le même mapping validé et les mêmes garde-fous,
sans **aucun token ni enregistrement métier**. Les nombres sont conservés sans
conversion d'unité ; JavaScript rejette explicitement les entiers hors de sa
précision sûre (au-delà de `Number.MAX_SAFE_INTEGER`), plutôt que de les écrire.

Le workflow est **désactivé**, en mode `simulation`, avec un déclencheur
manuel et un déclencheur toutes les 30 minutes (`Europe/Paris`).
La simulation n'écrit ni dans Grist ni dans la Data Table.
Dans la version JavaScript, tous les nœuds Code utilisent JavaScript et
`$input`. Dans la version Python, le Python natif utilise uniquement `_items`
et quelques nœuds JavaScript assemblent les sorties des nœuds précédents.
Tous les appels réseau passent par les nœuds HTTP Request.

### 1. Choisir la version

**Sans Python sur l'instance : importer la version JavaScript et passer
directement à l'étape 2.** Le JavaScript des nœuds Code doit être autorisé
sur l'instance n8n ; aucune configuration Python n'est nécessaire.
Les commandes `uv run python ...` de ce dépôt s'exécutent uniquement en
local pour découvrir les schémas ou régénérer les exports.

Les étapes 2 à 5 s'appliquent aux deux versions. Si un workflow a déjà été
importé, le désactiver avant d'importer l'autre version et ne jamais activer
les deux en même temps. Renseigner à nouveau les credentials et le
`state_table_id`. À mapping identique, la clé et l'empreinte d'état sont
identiques entre les versions : conserver la Data Table et ses reprises.
Ne pas réinitialiser un état existant pour changer de langage.

#### Uniquement pour la version Python

Le workflow cible les nœuds standards n8n 2.x : Code v2 (`pythonNative`),
HTTP Request v4.2, Data Table v1, IF v2.2 et Schedule Trigger v1.2.
Le runner Python natif doit être activé et disposer de Python 3.11+ avec
les données de fuseaux IANA pour `Europe/Paris`.

Autoriser uniquement ces modules standard sur le runner :

```text
N8N_RUNNERS_STDLIB_ALLOW=collections,datetime,json,math,re,typing,zoneinfo
```

En mode runner externe, configurer cette valeur dans `env-overrides` pour
le runner Python dans `/etc/n8n-task-runners.json` (fusionner avec la configuration
existante, ne pas la remplacer). En mode interne, suivre la configuration
de l'instance. Aucune dépendance tierce n'est nécessaire.
Ne pas désactiver la liste des built-ins interdits : le code embarqué évite
les classes personnalisées, `open`, `exec`, `eval`, `getattr` et les autres
built-ins interdits par défaut.

### 2. Créer l'état persistant

Dans le **même projet n8n** que le workflow, créer une Data Table, par exemple
`irve_sync_state`, avec deux colonnes de type **String** :

| Colonne | Contenu |
|---|---|
| `sync_key` | Valeur `state_key` du nœud Configuration |
| `payload` | JSON d'état sous forme de texte |

Créer exactement **une ligne**, avec :

```text
sync_key = appLzJVf0KpaEishq:tblfVBBQ18y1EE622:cK74vdT499GNpN2Gm2diUS:Enquete_2026
```

```json
{"version":1,"cursor":null,"retry_ids":[],"mapping_fingerprint":""}
```

La seconde valeur va dans `payload`, telle quelle. Laisser n8n générer
l'identifiant système `id` de la ligne. Ne pas créer de colonne `id`.
Conserver la persistance du stockage n8n/Data Tables et les sauvegardes.

### 3. Importer et configurer sans activer

1. Importer le fichier JSON avec « Import from File ».
2. Dans le nœud **Configuration**, remplacer uniquement `state_table_id`
   par l'ID de la Data Table créée.
3. Créer deux credentials **Header Auth**, chacun avec le nom de header
   `Authorization` et la valeur `Bearer <token>` :
   - Airtable : sélectionner ce credential dans `Schema Airtable` et `Lire Airtable` ;
   - Grist : sélectionner l'autre dans tous les HTTP Request Grist,
     y compris `Ecrire Grist` et `Relire Grist apres ecriture`.
4. Restreindre les domaines des credentials si cette option est disponible :
   `api.airtable.com` pour Airtable et le serveur Grist configuré pour Grist.
5. Ne pas copier les secrets du fichier local dans le code des nœuds.

Les credentials sont volontairement absents de l'export : ils doivent être
sélectionnés dans l'instance, même si leurs noms correspondent.
Ne pas épingler de données sur les nœuds de ce workflow.

### 4. Tester en simulation dans n8n

Conserver dans Configuration :

```json
{
  "mode": "simulation",
  "write_confirmation": "",
  "serialized_executions_confirmed": false,
  "modified_field_coverage_confirmed": false,
  "force_full_scan": false,
  "overlap_seconds": 120
}
```

Exécuter manuellement le workflow **entier**, sans le publier/activer.
Examiner le nœud **Bilan**, qui n'expose que les compteurs. Le nœud **Plan**
contient les détails transitoires des opérations et erreurs pour revue locale.
Une Data Table fraîche avec `cursor: null` provoque une comparaison complète.
Si un état réel existe déjà, `force_full_scan: true` permet une simulation
complète sans modifier cet état.

Les lectures de schémas précèdent les lectures de données ; une dérive du
contrat de schéma bloque l'exécution. La pagination Airtable est vérifiée,
limitée à 1 000 pages et espacée de 250 ms ; une fin tronquée est refusée.
Les GET ont au maximum 3 tentatives espacées de 2 secondes.
Les neuf colonnes calculées restent exclues.

### 5. Autoriser les écritures, séparément

**L'export n'autorise pas les écritures et cette préparation n'a écrit aucune
donnée dans vos bases.** Ne passer à cette étape qu'après validation du test
dans votre instance et des différences proposées.

Avant le premier passage en écriture :

- vérifier dans Airtable que `updatedat` surveille tous les champs utiles ;
- sauvegarder le document Grist et conserver le rapport de simulation ;
- garantir **une seule exécution de ce workflow à la fois**, y compris ses
  copies, les lancements manuels, les sous-workflows et les relances.

En mode n8n standard, `N8N_CONCURRENCY_PRODUCTION_LIMIT=1` sérialise les
exécutions de production **de toute l'instance**, mais **pas** les exécutions
manuelles, CLI ou sous-workflows. Désactiver le planning avant toute exécution
manuelle en écriture et attendre la fin de l'exécution précédente.
En mode queue/multi-workers, faire valider une sérialisation globale par
l'administrateur : une limite par worker ne suffit pas.
Si l'instance partagée ne permet pas cette garantie, **ne pas activer les
écritures** ; prévoir un verrou externe atomique ou une instance dédiée.

La Data Table n'est **pas un verrou atomique**. Le paramètre de confirmation
ci-dessous atteste une garantie d'exploitation ; il ne crée pas de verrou.

Après ces vérifications, modifier explicitement Configuration :

```json
{
  "mode": "write",
  "write_confirmation": "WRITE_AIRTABLE_TO_GRIST",
  "serialized_executions_confirmed": true,
  "modified_field_coverage_confirmed": true
}
```

La première exécution avec un curseur nul effectue le passage complet. Les
suivantes filtrent Airtable sur `updatedat` **ou la date de création**, avec
120 secondes de recouvrement, **ou les IDs des lignes à retenter**, même si
ces lignes n'ont pas changé depuis l'échec. `force_full_scan` reste normalement
à `false`. Le curseur est l'heure UTC du début de l'exécution, pas celle de fin.
Une clé de reprise non renvoyée par Airtable est conservée et signalée,
jamais assimilée à une suppression.

Le workflow :

1. Persiste un **checkpoint avant toute écriture**, conservant l'ancien
   curseur et tous les IDs des opérations à réaliser ou à retenter.
2. Envoie un PUT Grist par ligne, sans tentative automatique immédiate,
   avec `noparse=true`, `onmany=none`, une clé `require` obligatoire et des
   champs limités au mapping. Les mises à jour ne peuvent pas créer une ligne ;
   les créations ne peuvent pas mettre à jour une ligne déjà existante.
3. Continue les autres lignes en cas d'erreur HTTP d'une opération.
4. Relit Grist et vérifie chaque opération avant de la considérer réussie.
5. Persiste le curseur et les IDs en échec, puis signale les anomalies par
   un nœud **Stop And Error**. Un échec de sauvegarde arrête l'exécution.

Si l'exécution s'interrompt après un succès Grist mais avant le curseur,
le prochain passage relit les clés et valeurs : une création déjà réalisée
ne sera pas recréée. Ne jamais relancer **uniquement** un nœud d'écriture
avec des données anciennes ou épinglées ; relancer le workflow entier.
Une intervention concurrente sur les données peut malgré tout être détectée
à la vérification et mise en reprise.

Après un premier passage en écriture vérifié, publier/activer le workflow
pour le planning de 30 minutes. Choisir un Error Workflow n8n pour recevoir
des alertes selon votre canal ; aucune notification externe n'est préconfigurée.
Une erreur globale de lecture n'avance pas le curseur.

### Confidentialité, état et régénération

L'export désactive la sauvegarde des données d'exécution, en succès comme
en erreur, ainsi que celle des exécutions manuelles et de la progression.
Les données restent transitoirement visibles dans l'éditeur et les nœuds HTTP.
Ne pas épingler/exporter ces données ; ne pas activer leur sauvegarde sans
définir une rétention appropriée. L'état conserve les IDs à retenter et un
bilan chiffré, sans les valeurs des cellules.

Le mapping et les métadonnées des champs sont embarqués dans le workflow.
Toute nouvelle revue nécessite un nouvel export :

```sh
uv run python -m fdr_irve export-n8n --language javascript --output-dir n8n --overwrite
```

Pour régénérer uniquement l'ancienne version Python :

```sh
uv run python -m fdr_irve export-n8n --output-dir n8n --overwrite
```

Cette commande ne fait aucun appel réseau. Elle remplace uniquement le JSON
généré ; les valeurs de Configuration et les sélections de credentials faites
dans n8n doivent être réappliquées après réimport.
Sans `--overwrite`, un fichier existant est protégé.
Une empreinte différente de mapping bloque l'ancien état : arrêter les
exécutions, traiter/conserver les reprises, puis préparer explicitement un
nouveau passage complet. Ne pas effacer l'état d'une exécution en cours.

**Validation disponible dans ce dépôt :** tests des étapes Python et JavaScript,
comparaisons des conversions et résultats entre langages (notamment dates,
pourcentages, listes et références), tests des expressions et du graphe
dans les deux versions avec HTTP/Data Tables simulés
(échecs partiels, vérification, interruption et reprise). L'import et l'exécution
dans votre instance n8n restent à vérifier ; aucun accès administrateur n8n
n'a été fourni et aucun workflow distant n'a été publié.

## Règles de synchronisation

- n8n auto-hébergé ; HTTP Request et nœuds Code JavaScript sans runner Python
  requis. Une variante Python reste disponible.
- Une paire de tables, toutes les 30 minutes, fuseau `Europe/Paris`.
- Simulation sans écriture, validation, premier passage complet, puis incrémental
  via `updatedat`.
- Champ absent dans la réponse Airtable : conserver la valeur Grist.
- Champ présent avec `null`, `""` ou `[]` : effacer si le type Grist le permet.
  `0` et `false` restent des valeurs à synchroniser.
- Attention : Airtable omet souvent les valeurs vides, y compris les cases
  décochées. Un champ omis ne provoquera donc aucun effacement dans Grist,
  même si une valeur existait auparavant.
- Une ligne en erreur est signalée et ignorée, les autres continuent.
  Les échecs seront conservés pour une nouvelle tentative même sans modification
  ultérieure du record Airtable.
- Curseur et file de reprise dans une Data Table n8n. Prévoir une fenêtre de
  recouvrement, la pagination, la gestion des limites API et la protection
  contre les exécutions concurrentes.
- Hypothèse : les lignes existantes sont déjà rapprochées ; pas de rapprochement
  automatique. La simulation contrôlera les doublons et les clés manquantes.
- Aucun DELETE, ni synchronisation Grist vers Airtable.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```

Les tests utilisent uniquement des schémas synthétiques et des réponses HTTP
simulées. Node.js est nécessaire aux tests des expressions et du graphe n8n ;
il ne s'agit pas d'une installation du serveur n8n. La découverte et la
simulation réelles nécessitent vos tokens, à renseigner localement.

## Références API

- [Schéma Airtable](https://airtable.com/developers/web/api/get-base-schema)
- [API Grist : tables et colonnes](https://support.getgrist.com/api/)
- [Code Python natif dans n8n](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.code/)
- [Configuration des task runners](https://docs.n8n.io/hosting/configuration/environment-variables/task-runners/)
- [Contrôle de concurrence n8n](https://docs.n8n.io/hosting/scaling/concurrency-control/)
