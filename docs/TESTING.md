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

## Vérifications manuelles réalisées (17/11/2026, Docker Compose)

- `docker compose up` : migration appliquée, conteneur `healthy`, `/health/ready` = 200.
- Arrêt de PostgreSQL : `/health/ready` passe à 503 ; redémarrage : retour à 200 sans relancer l'application.
- Mot de passe de la base absent des logs du conteneur.
- `EXECUTION_MODE=live` : le service refuse de démarrer.
