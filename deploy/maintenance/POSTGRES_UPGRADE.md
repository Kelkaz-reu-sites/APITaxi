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

## Preuve du 22 octobre

Dernier essai réussi : `/var/backups/rezo-maintenance-test-20261022T140605Z` sur le
VPS REZO. Les quatre états restaurés sont identiques octet par octet au fichier
d'empreintes source : **45 tables applicatives, dont 44 non vides, 68 indexes,
77 contraintes, 3 hypertables, 26 chunks et 37 définitions de séquences**, plus
contrôle du nombre d'objets volumineux. Aucun contenu de ligne ni hash de mot de
passe/role n'est affiché. Les empreintes de lignes sont des agrégats par table.

```text
Dump complet : 2967650 octets
SHA256 dump : 6f4178bf04f8629650a4980719069d35eacc53ffbb0b309c9677cb8b46b32dd0
SHA256 evidence : 4e42e97119597f7ff8c3754e93f7a5efebae9962d435416cff42f130bc98fa32
```

Les tables internes d'extension ne sont pas comparées entre versions, puisqu'une
mise à jour transforme leurs catalogues ; la restauration est complète et leurs
hypertables/chunks sont vérifiés séparément. Les définitions de séquences sont
comparées ; leurs valeurs courantes ne sont pas MVCC et peuvent avancer pendant
un dump avec écritures actives. La bascule finale gèle donc d'abord les writers.

Tous les conteneurs d'essai ont été supprimés. Répertoires conservés en `0700`,
dumps/logs en `0600`, uniquement sur le serveur. Le premier essai à 14:02 UTC
avait rencontré le redémarrage normal du serveur temporaire d'initdb du clone ;
la production n'avait pas redémarré. Deux essais complets corrigés ont réussi.

## Préparation obligatoire avant la bascule

- Livrer le code et obtenir la CI complète verte pour le SHA exact sur `rezo/internal-service-base`.
- Préparer un commit séparé du Compose de production : image cible figée et nouveau volume logique `taxi-postgres-17-data`, nom effectif `rezo-taxi-core_taxi-postgres-17-data`. Le volume doit encore être absent. Conserver la déclaration de l'ancien volume pour le rollback.
- Ne pas exécuter `docker compose up` avec ce nouveau Compose avant l'outil de migration : une base vide ne doit pas démarrer à la place de l'ancienne.
- Créer un backup chiffré avec `deploy/backup_taxi_core.py`, uploader dans R2 privée et vérifier sa relecture SHA256. Le JSON de preuve doit contenir `database_image_id`, `application_image_id` et `source_checkout_sha`. Ne pas stocker la clé privée de déchiffrement sur le VPS.
- Garder les credentials applicatifs dans leur configuration protégée ; `roles.sql --no-role-passwords` ne sauvegarde pas les mots de passe. Prévoir leur restauration séparée pour une reprise après perte du serveur.
- Suspendre toute livraison concurrente pendant la fenêtre de bascule. L'outil emploie son propre verrou, que les déploiements existants ne prennent pas nécessairement.
- Utiliser `/opt/rezo-taxi-core/.env`, comme le Compose réellement en service. Le contrôle du 22 octobre a confirmé qu'il correspond aux variables live ; l'ancien `/etc/rezo-taxi-core/production.env` diffère sur le handoff et Sentry. L'outil vérifie la concordance des variables applicatives avant d'arrêter un service, sans afficher leurs valeurs.

## Commande unique proposée pour revue

Lancer d'abord **sans `--execute`** : seule la vérification des préconditions est
faite. Remplacer les trois valeurs majuscules par les preuves de la livraison
exacte, puis faire relire la commande avant d'ajouter `--execute`.

```bash
sudo python3 /opt/rezo-taxi-core/deploy/maintenance/cutover_postgres.py \
  --expected-prod-sha SHA_COMPLET_COMMIT_PRODUCTION \
  --ci-run IDENTIFIANT_RUN_CI_VERT \
  --offsite-proof /CHEMIN_PROTEGE/apitaxi-full-HORODATAGE.tar.age.json \
  --offsite-sha256 SHA256_CHIFFRE_VERIFIE \
  --tested-directory /var/backups/rezo-maintenance-test-20261022T140605Z \
  --test-evidence-sha256 4e42e97119597f7ff8c3754e93f7a5efebae9962d435416cff42f130bc98fa32
```

Cette proposition n'a pas été exécutée en production. Elle exige un checkout
propre au SHA attendu, la CI de ce SHA/branche/workflow réussie, l'image source
exacte, la preuve de restauration complète et un backup hors site de moins d'une
heure dont le chiffrement local correspond au SHA vérifié. Les commandes ne
mettent jamais les mots de passe dans leurs arguments ni dans leur sortie.

Avec `--execute`, l'outil proposé :

1. Arrête beat/worker/backend web, puis refait un backup chiffré hors site pendant le gel. Ce backup conserve encore les états normaux des jobs Timescale.
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
