# Tests

## Contrôles qualité

`scripts/check.sh` exécute, dans l'ordre :

1. `ruff check` : lint (dont règles de sécurité `S` et horodatages naïfs `DTZ`) ;
2. `ruff format --check` : formatage ;
3. `mypy` en mode strict sur `backend`, `tests` et `migrations` ;
4. `pytest`.

La CI GitHub (`.github/workflows/ci.yml`) lance le même script avec un service PostgreSQL 16.

## Tests d'intégration

Ils nécessitent une base PostgreSQL **jetable** (les tables sont vidées entre les tests) :

```bash
TEST_DATABASE_URL=postgresql+asyncpg://desk:desk@localhost:5432/desk_test scripts/check.sh
```

Sans `TEST_DATABASE_URL`, ils sont ignorés (signalés « skipped »).

## Couverture actuelle (étape M1)

| Domaine | Ce qui est vérifié |
|---|---|
| Configuration | Mode `live` refusé (argument ou variable d'environnement), URL de base validée, Telegram tout-ou-rien, secrets absents de `repr`, configuration immuable |
| Logs | Masquage par nom de clé et par valeur, y compris dans les objets imbriqués et les traces d'exception |
| Alertes | Format du message Telegram, échec réseau sans exception ni fuite du jeton, troncature à 4096 caractères |
| Heartbeat | Ping seulement si le service est prêt ; erreurs réseau absorbées |
| Santé | `/health/live` et `/health/ready` (200 / 503), check en erreur = non prêt |
| Types | Montants u64 sans perte, rejet des valeurs hors bornes et non entières |
| Base de données | Migration descendante puis montante ; **modèles strictement identiques aux migrations** ; prix `Decimal` et montants 2⁶⁴−1 exacts ; déduplication des événements bruts ; statuts invalides, quantités négatives et `remaining > quantity` rejetés par la base |
| Service | Cycle démarrage/arrêt enregistré dans `system_events` ; non prêt si la base est injoignable |

## Couverture ajoutée à l'étape M2

| Domaine | Ce qui est vérifié |
|---|---|
| Messages PumpPortal | Nombres lus en `Decimal` (jamais `float`), conversion exacte en lamports et unités brutes, arrondi bancaire, champs optionnels absents, identifiants invalides rejetés, messages de contrôle et inconnus |
| Déduplication | Clé indépendante de l'ordre des champs, deux trades d'une même transaction distincts, mémoire bornée |
| Suivi des tokens | Fenêtre de 60 min, abandon des tokens inactifs, limite de capacité comptée, suivi prolongé après migration |
| Coupures de flux | Début de coupure au dernier message reçu, fin au premier message suivant, déconnexion, arrêt du PC (coupure « service_down »), pas de fausse coupure avant la première donnée |
| Traitement | Lignes produites (événement brut, token, pool, snapshot), doublons, limitation des snapshots, token inconnu, migration, autres launchpads non suivis, échantillonnage de vérification |
| Client PumpPortal | Contre un faux serveur WebSocket local : abonnements, clé dans l'URL, réabonnement après reconnexion, message illisible conservé, **clé jamais écrite dans les logs** |
| Helius / vérification | Requête `getTransaction`, transaction absente, erreurs HTTP/RPC sans fuite de clé, succès/échec de transaction, présence du mint, délai de détection, nouvelles tentatives |
| Base de données (intégration) | Lot complet persisté, réécriture des mêmes événements sans doublon, métadonnées complétées après coup, coupure ouverte puis fermée sur deux lots, montants `Decimal` conservés exactement dans le JSON |
| Service (intégration) | Rejeu complet → base + alertes de coupure ; base indisponible → données gardées et réessayées, une seule alerte ; période de grâce de la santé du flux |
| Bout en bout (intégration) | Application complète + faux serveur PumpPortal : token reçu → abonnement à ses trades → token en base → `/health/ready` avec `feed: true` |

## Couverture ajoutée à l'étape M3

| Domaine | Ce qui est vérifié |
|---|---|
| Comptes on-chain | Mint SPL et Token-2022 (format `jsonParsed`), extensions ; rejet des comptes absents, mal typés ou détenus par un autre programme ; courbe pump.fun décodée, **courbe falsifiée (mauvais propriétaire) rejetée**, valeurs incohérentes rejetées |
| Calcul de la courbe | Impact d'entrée croissant avec la taille ; aller-retour immédiat = frais seulement (vérifié et documenté) |
| Règles de sécurité | Autorités actives, chaque extension Token-2022 (dont l'inconnue → critique), frais de transfert, concentration, créateur vendeur ou en série, lancement groupé, wallets coordonnés, RugCheck, donnée manquante → risque élevé |
| Collecte on-chain | Exclusion de la courbe dans les holders et regroupement par wallet ; origine des fonds (instructions directes et internes) ; wallets récents ou anciens (pas de crédit dépensé pour les anciens) ; activité du bloc de création, pagination et historique trop long → donnée manquante |
| Flux et score | Indicateurs de trades, accélération, wash trading ; rampes ; sentiment exclu et renormalisé ; composante manquante = 0 ; bandes |
| Moteur | Token sain → analyse approfondie et bon score ; risque critique → **pas de crédit dépensé** sur l'étage 2 ; mint illisible → jamais considéré sûr ; wallets coordonnés détectés ; budget épuisé → arrêt sans résultat partiel |
| Budget de crédits | Débit par méthode, remise à zéro quotidienne, **aucune requête envoyée** une fois le budget atteint |
| Service (intégration) | Analyse enregistrée et traçable (version des règles, indicateurs, décisions des agents) ; fenêtre d'âge et activité minimale respectées ; pas de réanalyse trop rapprochée ; **lecture des trades sans jamais regarder le futur** ; budget épuisé signalé une seule fois |

## Couverture ajoutée à l'étape M4

| Domaine | Ce qui est vérifié |
|---|---|
| Marché | Réserves, flux acheteurs/vendeurs, migration (courbe plus exécutable), oubli des tokens inactifs |
| Exécution simulée | Frais et décotes appliqués exactement ; **pire prix retenu** ; échec simulé = frais réseau payés ; pas de prix → aucun frais ; valorisation au prix exécutable ; impact estimé par l'agrégateur |
| Risk Manager | Chaque motif de veto (kill switch, flux, pause, cooldown, positions max, entrées max, impact) ; **taille limitée par le budget de perte, positions ouvertes comptées perdues** ; pause après 3 pertes |
| Sorties | Stop, TP1, TP2, trailing, stagnation, durée max, sorties de sécurité prioritaires, pas de double vente |
| Stratégies | Conditions de A ; témoin C reproductible (~10 %) ; B exige chute, rebond, acheteurs et une analyse sans risque critique |
| Moteur | Cycle complet TP1 → TP2 → trailing avec **PnL vérifié à l'unité** (cash = capital + PnL) ; stop exécuté au prix **après** la latence ; vente du créateur ; kill switch ; achat raté = coût enregistré ; token gradué sans cotation ; reprise après redémarrage ; **rejeu déterministe** ; instantanés de portefeuille |
| Base de données (intégration) | Position, trades (rent payé puis remboursé), décisions et instantané persistés ; PnL = ventes nettes − coût ; redémarrage → position « interrompue » au PnL vide et capital reconstruit ; kill switch persistant, **illisible = actif** ; étiquettes d'issues (fenêtres, rendements, coupures) idempotentes |

## Vérifications manuelles réalisées (17/11/2026, Docker Compose)

- `docker compose up` : migration appliquée, conteneur `healthy`, `/health/ready` = 200.
- Arrêt de PostgreSQL : `/health/ready` passe à 503 ; redémarrage : retour à 200 sans relancer l'application.
- Mot de passe de la base absent des logs du conteneur.
- `EXECUTION_MODE=live` : le service refuse de démarrer.

## Vérifications manuelles réalisées (18/11/2026, Docker Compose, ingestion activée)

PumpPortal n'est pas joignable depuis l'environnement de développement, ce qui a permis de tester le cas d'échec :

- reconnexions avec délais croissants, sans plantage ;
- clé API factice absente des logs ;
- `/health/ready` à 503 avec `feed: false`, ce qui couperait le ping healthchecks.io ;
- aucune coupure fictive enregistrée sans donnée reçue.

**Non vérifié à ce jour : la connexion au vrai flux PumpPortal et le format réel de ses messages.** À faire avec `python -m backend.scanner.probe` sur la machine d'exécution (voir `docs/INSTALL_WINDOWS.md`).

## Vérifications manuelles réalisées (18/11/2026, Docker Compose, analyse activée)

- Base vierge : les trois migrations s'appliquent dans l'ordre sur PostgreSQL 16.
- Service démarré avec ingestion et analyse actives ; version des règles `analysis-v1` enregistrée.
- Clés API factices (PumpPortal, Helius) absentes des logs.
- Limite : la construction complète de l'image a été bloquée par une limite de débit de Docker Hub (HTTP 429). Le nouveau code a été vérifié en surcouche de l'image précédente, qui a les mêmes dépendances.
