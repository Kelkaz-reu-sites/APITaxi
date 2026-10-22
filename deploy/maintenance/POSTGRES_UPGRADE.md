# PostgreSQL et TimescaleDB — restauration testée, bascule à revoir

22 octobre 2026. Les essais isolés ont réussi ; **aucune bascule de production
n'a été effectuée** par cet outillage. La production reste PostgreSQL 14.6 /
TimescaleDB 2.9.2 au moment de ces preuves. PostgreSQL 14 arrive en fin de support
le 12 novembre 2026, selon [PostgreSQL](https://www.postgresql.org/support/versioning/).

## Protocole éprouvé

Le skill `supabase-postgres-best-practices` a été lu avant les restaurations.
Le protocole suit les [consignes de restauration logique TimescaleDB](https://www.tigerdata.com/docs/deploy/self-hosted/backup-and-restore/logical-backup)
et le [traitement des incompatibilités de versions](https://www.tigerdata.com/docs/deploy/self-hosted/troubleshooting) :
restaurer avec la même version TimescaleDB, utiliser `timescaledb_pre_restore()`
et `timescaledb_post_restore()`, sans restauration parallèle, puis mettre à jour.
La branche 2.19 est la dernière compatible avec PostgreSQL 14 et sert de pont
vers PostgreSQL 17. Les [images HA](https://docs.tigerdata.com/self-hosted/latest/upgrades/upgrade-docker)
conservent les bibliothèques nécessaires aux versions précédentes.

`test_postgres_upgrade.sh` lit uniquement la production. Un snapshot MVCC exporté
maintient un même instant pour le dump complet et les comptages/empreintes ; les
écritures applicatives peuvent continuer pendant cet essai. Il restaure ensuite :

1. Le dump complet sur l'image exacte de production PG14.6 / TS2.9.2.
2. Le même répertoire de clone avec PG14.19, puis TS2.19.3 et les extensions compatibles.
3. Un nouveau dump sur un clone PG17.11, en créant d'abord TS2.19.3.
4. Les extensions finales : TS2.31.0, Toolkit1.26.0 et PostGIS3.6.4.

Les clones n'ont aucun réseau ni port publié, sont limités à 768 Mo/1 CPU,
sans capacités Linux, avec racine en lecture seule et seuls les répertoires
d'essai en écriture. Aucun socket Docker ni volume de production n'y est monté.
Le readiness attend PID1=`postgres`, puis trois lectures successives stables du
démarrage du serveur, pour éviter le serveur temporaire d'initialisation.

Images figées :

```text
Source : sha256:c6879ffd8d6167c82b2aa8df1d3941f586ce37ad485935cb6a46b68dd46b6b53
Pont : timescale/timescaledb-ha:pg14-ts2.19-all@sha256:127e0220562ee2e5b1abe001deb87670465bc55fca75aeaeaaf861b55734af47
Cible : timescale/timescaledb-ha:pg17.11-ts2.31.0-all@sha256:26e355e055c97d0a51afc415b71fa728dd85664fb24165d595cc8cd0c485b2fc
```

La cible des **nouveaux essais et de la proposition de bascule** est la variante
CA et outils Go reconstruite sur le VPS depuis cette même base :
`sha256:f180ded02bf7e10135f62ae8e6ab8b59425d406dbe1eacaa33a1439046e39c0a`
(`deploy/maintenance/Dockerfile.pg17-ca`). Le paquet CA et les trois binaires Go
ont changé ; les hashes des moteurs postgres/Timescale/PostGIS restent identiques.
Les bundles OS/pip/certifi passent le contrôle sans les racines retirées. Le pin
est l'ID immuable **mesuré sur le VPS Docker29**, sans tag local mutable ni
publication dans un registry.

La première variante CA-only `312856...` comportait encore 103 HIGH et deux
CRITICAL dans `pgbackrest_exporter`, `pgbouncer_exporter` et `yq`. Leurs sources
exactes ont été conservées, reconstruites avec Go1.27.3 et les modules corrigés
`x/crypto v0.57.0`, `x/net v0.60.0`, `x/text v0.42.0`, `x/oauth2 v0.27.0`. Les tests amont
s'exécutent sans réseau lors du build. Le smoke isolé valide les métriques
pgBackRest sur loopback, la CLI PgBouncer et une requête YAML/JSON avec yq.
La connexion réelle de l'exporter à un pooler PgBouncer n'est pas testée : aucun
pooler n'est configuré dans ce Compose.

Le scan **de l'ID exact sur le VPS**, terminé le 22 octobre à 20:17:14 UTC avec
Trivy0.76.1, retourne **0 HIGH et 0 CRITICAL**, sans ignore et avec les avis sans
correctif inclus. Il couvre Ubuntu, Python et les trois binaires Go.
Rapport : `/var/lib/rezo-maintenance/scans/pg17-candidate-20261022.json`, SHA256
`17821a0732cc8a414c3ed487f59495071b413c30c80061de670fe0e878f5967a`.
Copie PC privée et smoke CA/fonctions sous
`/home/phil/geo/.maintenance-backups/taxi/pg17-target-20261022/`.

Aucun conteneur de production n'a été remplacé. La revue du scan, du smoke et de
la répétition exacte a été donnée le 22 octobre ; `TARGET_SECURITY_REVIEWED=True`
enregistre cette revue pour ce pin uniquement. La validation du SHA final, le
backup frais vérifié sur PC et la décision de fenêtre restent obligatoires.
Un ancien `PASS` ne suffit plus :
`rehearsal.json` lie l'image source/cible, les scripts et les empreintes mesurées,
et le contrôleur impose moins de 24 heures.

Entretien de la recette : Dependabot suit aussi les bases Docker sous
`/deploy/maintenance`. Le scan quotidien des images réellement en service
contrôle les bibliothèques Go incorporées. Phil revoit au moins chaque mois les
trois sources amont, leurs avis et les pins de dépendances du Dockerfile :

- [pgbackrest_exporter](https://github.com/woblerr/pgbackrest_exporter), commit de départ `ad666e8c2a4d72dc84b7186774b52c058b668565` ;
- [pgbouncer_exporter](https://github.com/prometheus-community/pgbouncer_exporter), version `0.9.0`, commit de départ `5716a88b8dc8ce887878b2bda832ff46d79f67d2` ;
- [yq](https://github.com/mikefarah/yq), version `4.54.1`, commit `504fc38780cc46be8444ea1b72fb55919fc0bfb0`.

La reconstruction garde ces sources pour préserver les fonctions et remplace
leur toolchain/transitives vulnérables. Les hashes des archives et le digest du
compilateur sont dans le Dockerfile. Les modules principaux reconstruits peuvent
être marqués `(devel)` dans le buildinfo Go : leur revue amont reste nécessaire
en complément de l'analyse des modules embarqués, qui couvre stdlib/transitives.
Une mise à jour de source impose ses tests upstream, les smokes des CLI, un scan
complet et une nouvelle restauration avant remplacement de la cible. Aucun
nouveau workflow Actions n'est activé par ce lot.

## Preuve du 22 octobre

Dernier essai réussi : `/var/backups/rezo-maintenance-test-20261022T201813Z` sur le
VPS REZO, avec l'image cible exacte `f180ded...`. Les quatre états restaurés sont
identiques octet par octet au fichier
d'empreintes source : **45 tables applicatives, dont 44 non vides, 68 indexes,
77 contraintes, 3 hypertables, 26 chunks et 37 définitions de séquences**, plus
contrôle du nombre d'objets volumineux. Aucun contenu de ligne ni hash de mot de
passe/role n'est affiché. Les empreintes de lignes sont des agrégats par table.

```text
Dump complet : 2970921 octets
SHA256 dump : 36b886977bb64e7d85655de844f17189a7429c3ae5059b8e921afb52d30b31fb
SHA256 evidence : 70c0f48db1235e04e444f1c31d76edfe38fdb3a6d6e21f1bdf0380621ddbe5fa
SHA256 rehearsal.json : 7528a6e23d111d2941e89846992867ad7d98257f55f8752b7522fed771fe327c
```

Les tables internes d'extension ne sont pas comparées entre versions, puisqu'une
mise à jour transforme leurs catalogues ; la restauration est complète et leurs
hypertables/chunks sont vérifiés séparément. Les définitions de séquences sont
comparées ; leurs valeurs courantes ne sont pas MVCC et peuvent avancer pendant
un dump avec écritures actives. La bascule finale gèle donc d'abord les writers.

Tous les conteneurs d'essai ont été supprimés. Répertoires conservés en `0700`,
dumps/logs en `0600`, uniquement sur le serveur. Le premier essai à 14:02 UTC
avait rencontré le redémarrage normal du serveur temporaire d'initdb du clone ;
la production n'avait pas redémarré. Les essais initiaux puis la répétition de
20:18 UTC sur la cible CA/Go ont réussi. Le conteneur source et son démarrage
sont restés inchangés pendant cette dernière répétition.

## Préparation obligatoire avant la bascule

- Livrer le code et obtenir la CI complète verte pour le SHA exact sur `rezo/internal-service-base`, ou la validation locale mesurée ci-dessous si GitHub Actions n'a plus de crédits. Une CI absente/rouge ne devient jamais une CI réussie.
- Préparer un commit séparé du Compose de production : image cible figée et nouveau volume logique `taxi-postgres-17-data`, nom effectif `rezo-taxi-core_taxi-postgres-17-data`. Le volume doit encore être absent. Conserver la déclaration de l'ancien volume pour le rollback.
- Ne pas exécuter `docker compose up` avec ce nouveau Compose avant l'outil de migration : une base vide ne doit pas démarrer à la place de l'ancienne.
- Créer un backup chiffré avec `deploy/backup_taxi_core.py --local-only`, copier l'archive et sa preuve sur l'ordinateur de Phil, puis vérifier la copie par relecture SHA-256 et déchiffrement age. Phil a écarté R2 comme destination de sauvegarde. Avant bascule, obtenir un reçu PC mesuré `--operator-receipt` de moins d'une heure, avec digest et inventaire courants (`database_image_id`, `application_image_id`, `source_checkout_sha`). Le JSON du backup serveur garde **`offsite_verified=false`** : aucune édition de ce booléen ne remplace le reçu séparé. Ne pas stocker la clé privée de déchiffrement sur le VPS.
- Garder les credentials applicatifs dans leur configuration protégée ; `roles.sql --no-role-passwords` ne sauvegarde pas les mots de passe. Prévoir leur restauration séparée pour une reprise après perte du serveur.
- Suspendre toute livraison concurrente pendant la fenêtre de bascule. L'outil emploie son propre verrou, que les déploiements existants ne prennent pas nécessairement.
- Utiliser `/opt/rezo-taxi-core/.env`, comme le Compose réellement en service. Le contrôle du 22 octobre a confirmé qu'il correspond aux variables live ; l'ancien `/etc/rezo-taxi-core/production.env` diffère sur le handoff et Sentry. L'outil vérifie la concordance des variables applicatives avant d'arrêter un service, sans afficher leurs valeurs.

## Commande unique proposée pour revue

### Validation locale si les crédits GitHub sont épuisés

L'outil `operator_migration_proof.py validate` s'exécute exclusivement depuis le
checkout PC `/home/phil/geo/rezo-taxi-core`, propre au SHA exact. Il exécute
réellement l'audit verrouillé, les tests de backup/migration et de l'application,
les trois builds et les contrôles des bibliothèques natives, du runtime et des CA.
Le Compose de test utilise un nom de projet isolé, `--no-deps` et aucun `.env`.
Les sorties restent dans des logs privés `0600`, le dossier en `0700`.

```bash
python3 -B deploy/maintenance/operator_migration_proof.py validate \
  --expected-prod-sha SHA_COMPLET_COMMIT_PRODUCTION \
  --output-dir /home/phil/geo/.maintenance-backups/taxi/validation-SHA
```

Le reçu `manual-validation.json` conserve `github_status=not_used_no_credits`,
les exits réels, les SHA-256 des logs, de l'outil et du workflow. Il est valable
24 heures pour ce SHA seulement. Un échec garde `status=failed` et bloque la
bascule. Après revue, copier ce dossier sur le VPS puis installer le reçu et
**tous ses logs** en root `0600` sous un dossier `0700`, par exemple
`/var/backups/rezo-taxi-core/manual-validation/SHA/`. Conserver le SHA-256 réel
du reçu affiché par l'outil pour `--manual-validation-sha256` ; le contrôleur
recalcule aussi chaque log. Cette préparation n'est pas une bascule.

### Preuve PC d'une sauvegarde réelle

Sur le PC, pour une archive précise créée par le wrapper serveur et un digest
revu, exécuter :

```bash
python3 -B deploy/maintenance/operator_migration_proof.py copy-backup \
  --archive apitaxi-full-HORODATAGE.tar.age \
  --sha256 SHA256_CHIFFRE_VERIFIE \
  --expected-prod-sha SHA_COMPLET_COMMIT_PRODUCTION \
  --publish
```

Le helper fait un **vrai pull SSH**, relit les octets locaux, déchiffre avec
`/home/phil/.local/bin/age -d -i /home/phil/.ssh/id_ed25519` (seul age lit la clé),
puis valide en mémoire le TAR limité aux trois entrées, l'identité du manifeste,
la signature `PGDMP`, la taille et le hash du dump. Aucun contenu de base ni clé
n'est affiché et aucun clair n'est écrit sur disque. L'archive est limitée à
512 Mio. `--publish` envoie seulement un reçu JSON vers le même VPS REZO : le
lecteur privilégié recalcule le hash serveur, compare le checkout/les images et
écrit un reçu root `0600` nommé `<archive>.operator-pc.json`. Aucune API de
stockage n'est contactée. Sans `--publish`, le reçu reste uniquement sur le PC.

### Précontrôle de la fenêtre

Lancer d'abord **sans `--execute`** : seule la vérification des préconditions est
faite. Remplacer les trois valeurs majuscules par les preuves de la livraison
exacte, puis faire relire la commande avant d'ajouter `--execute`.

```bash
sudo python3 /opt/rezo-taxi-core/deploy/maintenance/cutover_postgres.py \
  --expected-prod-sha SHA_COMPLET_COMMIT_PRODUCTION \
  --ci-run IDENTIFIANT_RUN_CI_VERT \
  --offsite-proof /CHEMIN_PROTEGE/apitaxi-full-HORODATAGE.tar.age.json \
  --offsite-sha256 SHA256_CHIFFRE_VERIFIE \
  --operator-receipt /var/backups/rezo-taxi-core/full/apitaxi-full-HORODATAGE.tar.age.operator-pc.json \
  --tested-directory /var/backups/rezo-maintenance-test-20261022T201813Z \
  --test-evidence-sha256 70c0f48db1235e04e444f1c31d76edfe38fdb3a6d6e21f1bdf0380621ddbe5fa
```

Cette proposition n'a pas été exécutée en production. Elle exige un checkout
propre au SHA attendu, la CI de ce SHA/branche/workflow réussie ou le reçu local
mesuré avec ses logs, l'image source
exacte, la preuve de restauration complète et un backup hors site de moins d'une
heure dont le chiffrement local correspond au SHA vérifié. Les commandes ne
mettent jamais les mots de passe dans leurs arguments ni dans leur sortie.

Pour utiliser la preuve locale, remplacer `--ci-run ...` par les deux options
`--manual-validation-proof /var/backups/rezo-taxi-core/manual-validation/SHA/manual-validation.json`
et `--manual-validation-sha256 SHA256_RECU_LOCAL_REVU`. Les deux modes sont
mutuellement exclusifs ; la sortie précise la source de validation.

Avec `--execute`, l'outil proposé :

1. Arrête beat/worker/backend web, puis refait un backup local chiffré pendant le gel. Il affiche `FROZEN_BACKUP_READY` avec son basename/digest et attend au maximum dix minutes un **nouveau** reçu PC associé à cette archive. Depuis le PC, relancer `copy-backup` avec les valeurs affichées et **`--frozen --publish`** ; le serveur vérifie réellement que les trois writers sont arrêtés avant d'accepter ce reçu. Absence de PC/réseau, échec age/SHA ou reçu ancien : arrêt de l'opération et reprise de l'ancienne production avant exposition. Ce backup conserve encore les états normaux des jobs Timescale.
2. Sauvegarde leurs états, suspend la planification des jobs Timescale (compression, rétention et tâches système) et attend la fin des jobs actifs avant le dump final. Les restaurations d'essai ont montré que ces jobs existent ; arrêter seulement Celery ne les gèle pas.
3. Rejoue le protocole complet sur un nouveau jeu de clones, sans toucher à l'ancien volume.
4. Remplace l'authentification `trust` du clone par SCRAM, restitue au seul login `apitaxi` son mot de passe depuis l'environnement du conteneur source sans l'afficher, et prouve qu'un mauvais mot de passe TCP est refusé. Ce test d'authentification a aussi réussi sur le clone isolé le 22 octobre. Tous les jobs de la cible restent suspendus.
5. Arrête proprement le clone, préserve les paramètres de connexions/mémoire/workers actuels, puis copie son répertoire dans le nouveau volume.
6. Démarre PG17, recalcule les empreintes complètes et exige leur égalité avec la source gelée. Démarre le backend uniquement sur le réseau privé du Taxi Core, avec une surcharge Compose `!override`. Après sa santé HTTP, vérifie réellement la connexion SQL de l'application, les versions PG17/TS2.31 et Redis avant toute exposition au site. Le health HTTP seul ne teste pas la connexion SQL.
7. Écrit le reçu `COMMITTED`, restitue les états des jobs Timescale, attache l'alias `rezo-taxi-core-web` au réseau externe `rezo-internal`, puis reprend worker et beat.

La nouvelle base ne doit jamais être mise sur le réseau avec le `pg_hba.conf`
`trust` utilisé dans les essais sans réseau. Le clone d'essai a également une
capacité réduite (25 connexions/192 Mo de shared_buffers) : elle n'est pas reprise
comme nouveau réglage de production. L'outil conserve les valeurs du serveur
actuel dans `postgresql.auto.conf` après l'essai d'authentification.

**Rollback automatique seulement avant exposition** : tant que le backend ne peut
pas recevoir d'écritures, l'ancien volume/image peut être remis avec la surcharge
de rollback protégée. Après l'attachement externe, même une erreur ne doit plus
restaurer automatiquement l'ancien volume : cela perdrait les nouvelles écritures.
L'outil garde PG17 et signale l'erreur pour intervention. Tout attachement réseau
externe inattendu ou ambigu est traité comme une exposition possible. La reprise
des jobs internes est également considérée comme une frontière d'écriture : elle
survient après la santé du backend et l'écriture du reçu `COMMITTED`. Avant cette
frontière, le rollback rétablit aussi les états d'origine des jobs sur l'ancien
volume.

Timescale remplace son job interne 2 `policy_job_error_retention` par le job 3
`policy_job_stat_history_retention` pendant la mise à niveau 2.15. Le code conserve
explicitement son état de planification initial, conformément au
[script officiel de migration](https://github.com/timescale/timescaledb/blob/2.31.0/sql/updates/2.14.2--2.15.0.sql).
Tout autre changement inattendu d'identité d'un job interrompt la préparation.

Après succès : vérifier les parcours applicatifs, versions réelles, état des
workers, sauvegarde/restauration PG17 et réception des alertes ; documenter le SHA,
le nouveau volume et le backup dans `doc/DEPLOY.md` REZO. Ne supprimer aucun ancien
volume avant validation séparée de la rétention/rollback.
