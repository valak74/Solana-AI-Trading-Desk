# Solana AI Trading Desk

Système de recherche et de trading sur les nouveaux tokens Solana (pump.fun), construit en mode **« mesure d'abord »** : on vérifie qu'un avantage net de frais existe avant de construire l'exécution réelle.

> **État actuel : étapes M2 (enregistrement), M3 (analyse) et M4 (paper trading) terminées et testées localement ; validation sur données réelles à faire. Paper trading uniquement : aucun wallet, aucune transaction réelle.** Seul le mode `paper` est accepté par la configuration. Aucun résultat simulé ne doit être présenté comme un gain réel.

## Documents

| Document | Contenu |
|---|---|
| [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md) | Plan « mesure d'abord » : étapes M1–M5, point de décision GO/STOP, étapes G1–G5 |
| [`docs/TOOLING_AND_STRATEGY.md`](docs/TOOLING_AND_STRATEGY.md) | Outils à moindre coût, stratégies A/B/C, règles de sortie et de risque |
| [`docs/TESTING.md`](docs/TESTING.md) | Lancer les tests et les contrôles qualité |
| [`docs/INSTALL_WINDOWS.md`](docs/INSTALL_WINDOWS.md) | Installation sur un PC Windows 11 pour la phase de mesure |
| [`docs/ANALYSIS.md`](docs/ANALYSIS.md) | Règles de sécurité, score d'opportunité, budget de crédits, limites |
| [`docs/TRADING_STRATEGY.md`](docs/TRADING_STRATEGY.md) | Stratégies A/B/C, sorties, modèle d'exécution prudent, Risk Manager, kill switch |

## Démarrage avec Docker (VPS ou poste local)

```bash
cp .env.example .env        # puis renseigner POSTGRES_PASSWORD (lettres et chiffres)
docker compose up -d --build
curl http://127.0.0.1:8000/health/ready
```

- Les migrations de base de données sont appliquées automatiquement au démarrage.
- L'API n'écoute que sur `127.0.0.1` ; PostgreSQL n'est pas exposé hors du réseau Docker.
- `/health/live` : le processus répond. `/health/ready` : base joignable et schéma à jour (sinon HTTP 503).
- Alertes Telegram et ping healthchecks.io optionnels (voir `.env.example`).

## Développement local

```bash
uv sync                      # installe les dépendances (Python ≥ 3.12)
scripts/check.sh             # lint, format, typage strict, tests
DATABASE_URL=postgresql+asyncpg://desk:motdepasse@localhost:5432/desk uv run alembic upgrade head
DATABASE_URL=... uv run python -m backend
```

## Principes non négociables

- Aucun montant comptable en `float` : lamports et quantités en entiers, prix en `Decimal`.
- Aucun secret dans Git, dans les logs ou dans le frontend ; les logs masquent automatiquement les secrets connus.
- Aucun LLM ne signe ni ne transmet de transaction.
- En cas de doute technique, le système ne trade pas.
