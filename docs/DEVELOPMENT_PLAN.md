# Solana AI Trading Desk — Plan de développement (Phase 0)

Date : 17 novembre 2026 · Statut : **en attente de validation opérateur** · Aucun code applicatif n'a été écrit.

---

## 1. État du repository

| Élément | Constat |
|---|---|
| Commits | 1 (`Initial commit`) sur `main`, branche de travail identique |
| Fichiers | `README.md` (titre seul) |
| Code / dépendances / Docker | Aucun |
| Éléments réutilisables | Aucun — démarrage à partir de zéro |

Outillage disponible dans l'environnement de développement : Python 3.13, Node 22, Docker 29, PostgreSQL 16, Redis 7.

**Incompatibilités techniques avec le dépôt :** aucune (rien à migrer). Les incompatibilités réelles se situent dans le cahier des charges lui-même et dans les offres des fournisseurs (§5 et §7).

---

## 2. Points du cahier des charges à corriger ou préciser

Ces points doivent être tranchés avant ou pendant la Phase 1. Chacun a une proposition par défaut.

1. **Taille de position vs. perte journalière.** Position max 10 € > perte journalière max 3 % × 300 € = 9 €. Un seul rug (perte ≈ 100 %, fréquent sur meme coins, le stop -15 % ne protège pas d'un gap) dépasse la limite journalière.
   *Proposition :* la taille de position est plafonnée par `min(position_max, perte_journalière_restante)` ; risque budgété par trade calculé en supposant une perte possible de 100 % du montant engagé, pas 15 %.
2. **Coûts aller-retour sur une position de 10 €** (ordres de grandeur, à mesurer en paper) :
   - Frais de protocole pump.fun / PumpSwap : ~1 à 1,25 % par côté selon la phase (sources divergentes, à vérifier sur la doc pump.fun).
   - Rent du compte de token (ATA) : ~0,002 SOL, soit plusieurs % d'une position de 10 €. **Récupérable uniquement si le compte est fermé après la vente** → fermeture d'ATA obligatoire dans le moteur.
   - Frais de priorité / landing : faibles en SOL mais non négligeables rapportés à 10 €.
   - Slippage : 1 à 5 %+ par côté sur courbes peu liquides.
   - **Total réaliste : 4 à 10 % aller-retour.** Un TP1 à +25 % brut vaut environ +15 à +20 % net. Le scoring et l'espérance doivent être calculés nets.
3. **Budget infrastructure (30 €) irréaliste** — voir §7. Les coûts récurrents dépasseront probablement 30 €/mois, et le rendement nécessaire pour les couvrir avec 150 € de capital de trading est très élevé. Les KPI doivent inclure une ligne « PnL net d'infrastructure ».
4. **Simulation de vente avant achat.** La RPC Solana standard ne permet pas de simuler la vente d'un token que le wallet ne détient pas (pas de surcharge d'état de compte). Avant achat, la vendabilité est **inférée** (autorités, extensions Token-2022, existence d'une route de vente Jupiter, mathématique de la courbe). Après achat, une simulation de vente réelle de la position détenue est exécutée immédiatement. Conforme à l'exigence « ne pas prétendre qu'un token est sûr parce qu'une simulation réussit ».
5. **Latence Grok vs. sniping.** Une requête Grok avec X Search prend plusieurs secondes à dizaines de secondes, et un token de quelques minutes a rarement des données X exploitables. *Proposition :* sentiment asynchrone, appelé seulement si le pré-score (90 points hors sentiment) peut franchir le seuil ; en l'absence de résultat, composante sentiment neutre avec `confidence = 0` (comportement configurable : attendre / ne pas attendre).
6. **Authentification du dashboard.** Non mentionnée dans le cahier des charges, mais indispensable : les endpoints Settings, mode live et kill switch modifient le comportement financier. *Proposition :* dashboard accessible uniquement en local / VPN + authentification par session, actions sensibles confirmées.
7. **Tables manquantes** (§4.4) : `pools`, `raw_events`, `orders` (clé d'idempotence), `kill_switch_state`, `config_versions`, `api_usage`, `wallet_profiles`.

---

## 3. Architecture proposée

### 3.1 Processus

Un seul paquet Python (`backend/`), plusieurs points d'entrée exécutés dans des conteneurs distincts, communiquant par Redis Streams (groupes de consommateurs) et PostgreSQL (source de vérité).

```
                 ┌──────────────┐
 Helius WS / ──▶ │  ingestor    │──▶ raw_events (PG, append-only)
 gRPC / replay   │  (scanner)   │──▶ stream: token.discovered / trade.observed
                 └──────────────┘
                        │
                        ▼
                 ┌──────────────┐      ┌──────────────┐
                 │  analyzer    │─────▶│  sentiment   │  (Grok, async, budgété)
                 │ on-chain +   │◀─────│  worker      │
                 │ security +   │      └──────────────┘
                 │ scoring      │
                 └──────────────┘
                        │ stream: token.scored
                        ▼
                 ┌──────────────────────────────────┐
                 │  trader (instance unique, verrou) │
                 │  strategist → risk manager →      │
                 │  execution (paper | live) →       │
                 │  position manager                 │
                 └──────────────────────────────────┘
                        │ demande + jeton d'approbation risk
                        ▼  (profil compose "live" uniquement)
                 ┌──────────────┐
                 │   signer     │  clé chiffrée, socket interne,
                 │              │  aucun accès réseau sortant
                 └──────────────┘

 api (FastAPI, lecture + settings + kill switch, jamais de signature) ◀──▶ frontend (Next.js)
```

### 3.2 Modules backend (correspondance avec l'arborescence imposée)

| Dossier | Contenu |
|---|---|
| `config/` | `pydantic-settings`, profils paper/live, validation au démarrage, versionnement des paramètres de stratégie (`config_versions`) |
| `database/` | Modèles SQLAlchemy 2 (async), sessions, repositories. Montants : `BIGINT` en unités de base (lamports, unités brutes du token), prix en `NUMERIC(38,18)` / `Decimal` |
| `blockchain/` | Client JSON-RPC asynchrone mince (httpx + `solders`), décodeurs de comptes (Mint, Token-2022 extensions, courbe pump.fun, pools PumpSwap), parsing d'instructions |
| `scanner/` | Interface `MarketDataProvider` + implémentations : `HeliusWebSocketProvider`, `YellowstoneGrpcProvider` (interface + stub au départ), `ReplayProvider` (rejoue `raw_events` pour tests et backtests). Déduplication par (signature, index d'instruction) |
| `security/` | Analyse autorités, extensions Token-2022, concentration, wallets liés au créateur, vendabilité inférée → `security_score`, `risk_level`, `critical_flags`, `warnings` |
| `sentiment/` | Client xAI (httpx, timeouts, retry, budget), prompt + schéma Pydantic strict, journalisation des sources |
| `scoring/` | Composantes du score (fonctions pures, testables), pondérations et seuils configurables |
| `agents/` | Orchestration et historisation `agent_decisions` (entrées, sorties structurées, durée, version de config) |
| `risk/` | Risk Manager déterministe, émission de jetons d'approbation (montant max, nonce, expiration, HMAC) |
| `trading/` | Strategist, Position Manager (TP, stop, trailing, sortie temporelle, sortie de sécurité) |
| `execution/` | Interface `ExecutionEngine` ; `PaperExecution` (modèle de fill conservateur) ; `JupiterLiveExecution` (`/order` → validation → signature → `/execute` → confirmation → réconciliation) ; emplacement pour Helius Sender |
| `wallet/` | `WalletSigner` : chargement de clé chiffrée, validation de transaction avant signature, aucun export |
| `integrations/` | Adaptateurs HTTP Jupiter, Helius, xAI (rate limiting, circuit breakers, comptage `api_usage`) |
| `monitoring/` | Logs structurés (structlog, filtre de secrets), métriques, health checks, détection de flux obsolète, alertes |
| `api/` | FastAPI : lecture, settings, kill switch, SSE pour le temps réel, authentification |

### 3.3 Règles de conception clés

- **Vérité financière = quantités exécutées.** Le PnL est calculé à partir des variations de solde on-chain (live) ou des fills simulés (paper), jamais d'un prix théorique.
- **Modèle de fill paper conservateur :** cotation Jupiter réelle à T0, délai simulé (distribution configurable), puis prix d'exécution = le pire entre une recotation à T0+délai et le calcul exact sur les réserves de la courbe / du pool au slot correspondant, frais inclus, plus une marge de slippage. Taux d'échec simulé configurable. Toutes les opérations marquées `execution_mode = paper`.
- **Gate « flux sain » :** si aucun événement depuis N secondes ou si le retard de slot dépasse un seuil, le Risk Manager refuse tout nouvel achat.
- **Idempotence :** chaque ordre porte une clé d'idempotence persistée avant tout appel externe ; au redémarrage, réconciliation des ordres `pending` via signature / soldes avant toute nouvelle action.
- **Instance de trading unique :** verrou consultatif PostgreSQL pour empêcher deux traders actifs.
- **Kill switch :** état persistant en PostgreSQL (source de vérité) + cache Redis ; vérifié par le Risk Manager **et** par le signer (défense en profondeur). Il bloque les nouveaux achats ; les sorties suivent la politique configurée (pas de liquidation automatique par défaut).

### 3.4 Sécurité du signer (mode live)

- Conteneur séparé, démarré uniquement avec le profil Compose `live` ; le mode paper n'a besoin d'aucune clé.
- Clé privée chiffrée au repos (passphrase fournie au démarrage ou fichier secret Docker, jamais en variable d'environnement loguée, jamais dans Git).
- Le signer n'accepte qu'une requête accompagnée d'un jeton d'approbation du Risk Manager (HMAC, nonce à usage unique, expiration courte, montant max d'entrée) et vérifie que la transaction :
  - a pour fee payer le wallet du bot ;
  - n'invoque que des programmes en liste blanche (Jupiter, Compute Budget, System, Token, Token-2022, ATA, programmes AMM routés) ;
  - ne sort pas plus de SOL / tokens que le montant approuvé ;
  - passe une simulation (`simulateTransaction`) sans erreur.
- Aucun outil, prompt ou chemin de code ne relie Grok au signer ; les sorties Grok ne sont que des nombres bornés validés par Pydantic, consommés par le scoring.
- **Activation du live** = trois conditions simultanées : variable `LIVE_TRADING_ENABLED=true` côté serveur, conteneur signer présent, et confirmation explicite de l'opérateur dans le dashboard (phrase saisie + rapport des critères de passage affiché). Retour en paper instantané, sans condition.

---

## 4. Plan de développement par phases

Chaque phase se termine par le rapport demandé (fichiers, fonctionnalités, tests et résultats, dépendances, problèmes, reste à faire). Pas de passage à la phase suivante avec une erreur critique ouverte.

### Phase 1 — Fondations
- `pyproject.toml` (gestionnaire `uv`), `ruff`, `mypy --strict`, `pytest`, `pytest-asyncio`.
- Configuration typée, logs structurés avec masquage des secrets.
- Modèles SQLAlchemy + migrations Alembic de toutes les tables (§4.4).
- FastAPI : `/health/live`, `/health/ready` (PG, Redis, fraîcheur du flux).
- Docker Compose : `postgres`, `redis`, `api`, `ingestor`, `analyzer`, `trader` ; profil `live` pour `signer`.
- Kill switch persistant (modèle + API + tests).
- **Critère de sortie :** `docker compose up` démarre, health checks verts, migrations montantes/descendantes testées, CI locale verte.

### Phase 2 — Blockchain et scanner
- Client RPC + reconnexion/backoff, rate limiting.
- `HeliusWebSocketProvider` (logs des programmes pump.fun / PumpSwap), `ReplayProvider`, interface Yellowstone (stub documenté).
- Décodage création de token, création/migration de pool, trades.
- Déduplication, `raw_events` append-only, métadonnées (Metaplex / extension metadata Token-2022).
- **Mesure du volume de données sur 1 h** pour chiffrer la consommation de crédits Helius (§7).
- **Enregistrement continu des événements dès la fin de cette phase** → constitution du jeu de données de backtest pendant le développement des phases suivantes.
- **Critère de sortie :** scanner stable 24 h, taux de doublons nul, reconnexions testées, détection mesurée (latence création → détection).

### Phase 3 — Analyse et sécurité
- Parsing mint/autorités, extensions Token-2022 (critiques : `PermanentDelegate`, `TransferHook` non listé, `NonTransferable`, `DefaultAccountState=Frozen`, `Pausable` avec autorité, `TransferFee` au-dessus du seuil ; autorités mint/freeze actives).
- Concentration des holders, wallets liés au créateur (financement commun, achats dans le même slot / bundles), acheteurs/vendeurs uniques, volume « réel » (exclusion wash trading simple).
- Liquidité et profondeur (impact de prix pour la taille visée, calculé sur les réserves).
- Classification de risque ; toute donnée manquante est un `warning` explicite, jamais une valeur par défaut silencieuse.
- **Critère de sortie :** chaque token découvert reçoit une analyse horodatée ; jeux de tests avec cas de rug / honeypot construits à partir d'événements enregistrés.

### Phase 4 — Grok
- Client xAI (Responses API, outil serveur X Search si disponible sur le modèle choisi), structured outputs en mode strict, validation Pydantic, timeouts, retry, budget journalier d'appels, cache par token.
- Règles de prompt : pas de chiffres non sourcés, `evidence` obligatoire, confiance basse si sources insuffisantes ; validation Python (bornes, cohérence evidence/confiance).
- **Critère de sortie :** tests avec réponses enregistrées (y compris réponses invalides / hallucinées rejetées), test d'intégration réel optionnel, coût par analyse mesuré.

### Phase 5 — Moteur de trading
- Scoring (6 composantes conservées séparément), Strategist (seuils configurables, rejet si risque critique).
- Risk Manager (toutes les limites du §4.6 du cahier des charges + gate flux sain + kill switch).
- Position Manager (TP1/TP2/trailing/stop/temps/sécurité, gestion des tokens invendables, fermeture d'ATA).
- Adaptateur Jupiter Swap API V2 (`/order`, `/execute`), `PaperExecution`, `JupiterLiveExecution` isolé, réconciliation.
- **Critère de sortie :** paper trading de bout en bout sur flux réel ; tests des scénarios de trading (§15 du cahier des charges) via `ReplayProvider`. **Démarrage de l'horloge des 30 jours d'observation.**

### Phase 6 — Dashboard
- Next.js (App Router) + TypeScript + Tailwind + Recharts ; pages Overview, Live Scanner, Positions, Agents, History, Settings.
- Temps réel via SSE ; bandeau permanent PAPER / LIVE avec couleurs distinctes ; authentification.
- **Critère de sortie :** toutes les pages alimentées par l'API réelle, tests de composants et un test e2e Playwright.

### Phase 7 — Validation
- Backtests sur les événements enregistrés, rapports KPI (§16 du cahier des charges, y compris performance par score / type / tranche horaire, et PnL net d'infrastructure).
- Tests de sécurité (accès secrets, signature non autorisée, transaction malformée, double exécution, expiration, coupure réseau).
- Rapport automatique des critères de passage au réel (300 trades, 30 jours, espérance > 0, profit factor > 1,3, drawdown < 10 %, etc.).
- Documentation : README, ARCHITECTURE, API, SECURITY, TRADING_STRATEGY, DEPLOYMENT, TESTING.

**Chemin critique :** les 30 jours de paper trading ne commencent qu'à la fin de la Phase 5. Les phases 6 et 7 se développent pendant cette période d'observation. Le passage au réel est au plus tôt ~30 jours après la Phase 5, et seulement sur décision explicite de l'opérateur.

---

## 5. API, abonnements et dépendances externes

| Service | Usage | Offre minimale | Remarques |
|---|---|---|---|
| **Helius RPC + WebSocket** | Découverte, données on-chain, simulation, confirmation | Free pour démarrer ; **Developer (~49 $/mois)** recommandé | Free : 1 M crédits, 10 req/s, 5 connexions WS. Developer : 10 M crédits, 50 req/s. Le streaming est facturé au volume (≈ 20 crédits/Mo). `transactionSubscribe` à partir de Developer (à confirmer dans le tableau de bord) |
| **Helius LaserStream gRPC (mainnet)** | Flux basse latence | **Business (~499 $/mois)** | Disponible depuis avril 2026 sur Business (Developer = devnet seulement). Incompatible avec le budget : interface préparée, implémentation reportée |
| **Jupiter Swap API V2** | Cotations, transactions, `/execute` | Clé gratuite (portail développeur) | Base `api.jup.ag/swap/v2`, header `x-api-key`. Limites par organisation, `/execute` dans un bucket séparé. Chiffres exacts à vérifier sur la page officielle |
| **xAI API** | Sentiment / narratif | Paiement à l'usage | Structured outputs disponibles. Outils serveur (X Search) facturés à l'appel (≤ ~5 $/1000 appels selon les notes de version) + tokens |
| **VPS** | Exécution continue | 2–4 vCPU, 4–8 Go RAM | Proche des validateurs (Francfort / Amsterdam). Docker Compose |
| **Pump.fun** | Programmes on-chain (pas d'API payante requise) | — | `create_v2` (Token-2022) en place depuis novembre 2025 : les deux programmes de token doivent être gérés |

Bibliothèques Python principales : `fastapi`, `uvicorn`, `pydantic` v2, `pydantic-settings`, `sqlalchemy[asyncio]` 2, `asyncpg`, `alembic`, `redis`, `httpx`, `websockets`, `solders`, `structlog`, `prometheus-client`, `cryptography` ; plus tard `grpcio` (Yellowstone). Dev : `pytest`, `pytest-asyncio`, `respx`, `hypothesis`, `mypy`, `ruff`.

Version Python : cible 3.12 (image Docker `python:3.12-slim`), tests également exécutés sous 3.13.

---

## 6. Données (complément au schéma imposé)

Tables du cahier des charges conservées, avec ajouts :

- `positions` et `trades` : `execution_mode` (paper/live) sur les deux ; montants en unités de base ; `trades` + `requested_amount`, `expected_out`, `actual_out`, `priority_fee_lamports`, `rent_lamports`, `idempotency_key`, `failure_reason`.
- `pools` : adresse, programme (bonding curve / PumpSwap / autre), mint, statut, réserves au dernier snapshot.
- `raw_events` : événement brut, source, slot, signature, horodatages (bloc, réception) → base des backtests.
- `orders` : intention d'ordre persistée avant tout appel externe (idempotence et reprise).
- `kill_switch_state`, `config_versions`, `api_usage`, `wallet_profiles` (créateurs et wallets suspects connus).

Chaque `agent_decisions` et `token_scores` référence la `config_versions` utilisée, pour pouvoir comparer des stratégies.

---

## 7. Estimation des coûts d'infrastructure (mensuels, indicatifs)

| Poste | Minimal (paper) | Recommandé | Basse latence |
|---|---|---|---|
| VPS | 10–20 € | 15–30 € | 30–60 € |
| Helius | 0 $ (Free) | 49 $ (Developer) | 499 $ (Business, gRPC) |
| xAI (sentiment filtré : ~30–100 analyses/jour) | 5–15 $ | 15–50 $ | 15–50 $ |
| Jupiter | 0 $ | 0 $ | plan payant éventuel |
| **Total approx.** | **15–35 €** | **~80–130 €** | **~550–650 €** |

**Inconnue majeure :** le volume du flux pump.fun. Si l'abonnement aux logs des programmes pump.fun génère plusieurs dizaines de Go par jour, la consommation dépasse le quota Developer (10 M crédits ≈ 500 Go streamés/mois au tarif de 20 crédits/Mo, avant les appels RPC). La Phase 2 inclut une mesure d'une heure pour trancher. Alternatives : filtrage plus fin (abonnement aux seules transactions de création, puis suivi ciblé des tokens retenus) ou fournisseur de découverte tiers, derrière la même interface.

**Conséquence économique :** avec 150 € de capital de trading et ~100 €/mois d'infrastructure, la stratégie devrait produire plus de 60 % par mois uniquement pour couvrir ses coûts. La phase de paper trading doit être considérée comme de la R&D ; l'évaluation finale doit inclure le PnL net d'infrastructure, et le capital ou le budget sont peut-être à revoir.

---

## 8. Risques et dépendances bloquantes

### Bloquants (avant implémentation des phases concernées)
1. **Clé API Helius** (Phase 2) — Free suffit pour développer, Developer probablement nécessaire pour le flux continu.
2. **Clé API Jupiter** (Phase 5) — gratuite.
3. **Clé API xAI** et confirmation qu'un modèle disposant de X Search est accessible sur le compte (Phase 4).
4. **Accès réseau sortant** de l'environnement de développement vers `*.helius-rpc.com`, `api.jup.ag`, `api.x.ai` pour les tests d'intégration (à configurer si bloqué).
5. **Décisions opérateur** listées au §9.

### Risques techniques
- **Concurrence et latence :** les snipers professionnels opèrent en intra-slot avec bundles ; un bot WebSocket + Jupiter a des latences de centaines de ms à quelques secondes. La stratégie « confirmations avant entrée » est adaptée, mais l'avantage statistique reste à démontrer.
- **Base rate :** la grande majorité des nouveaux meme coins perd l'essentiel de sa valeur ; les rugs et coordinations de wallets évoluent plus vite que les heuristiques.
- **Évolution des programmes** pump.fun / PumpSwap (frais, instructions, Token-2022) : décodeurs versionnés et alertes sur instructions inconnues.
- **Couverture Jupiter** des tokens en phase de bonding curve : à vérifier en Phase 5 ; un adaptateur d'exécution direct sur la courbe pourrait être nécessaire (derrière l'interface `ExecutionEngine`).
- **Fiabilité de Grok :** latence, coût, hallucinations, données X rares pour les tokens très récents → poids limité (10), validation stricte, jamais de chemin direct vers une transaction.
- **Biais des backtests :** impossibilité de rejouer les cotations historiques de Jupiter ; le modèle de fill sur réserves de courbe atténue ce biais sans le supprimer.
- **Fiscalité / conformité (France) :** conserver un historique exportable de toutes les opérations réelles ; non traité techniquement au-delà de l'export.

---

## 9. Décisions demandées à l'opérateur

1. **Budget infra** : accepter ~80–130 €/mois (Helius Developer) ou commencer en Free + mesure avant d'acheter ?
2. **Périmètre DEX v1** : pump.fun bonding curve + PumpSwap uniquement (recommandé), autres launchpads plus tard ?
3. **Règle de taille de position** : adopter la proposition du §2.1 (plafond par la perte journalière restante, risque = 100 % du montant engagé) ?
4. **Sentiment** : autoriser une entrée sans résultat Grok (composante neutre) ou exiger son résultat ?
5. **Hébergement cible** : VPS (quelle région) ou machine locale ?
6. **Langue** : code et identifiants en anglais, documentation en français ?
7. **Accès au dashboard** : local / VPN uniquement + authentification (recommandé) ?

Après validation, la Phase 1 commence et s'achève par son rapport de livraison.
