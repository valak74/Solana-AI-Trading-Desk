# Solana AI Trading Desk — Plan de développement (v2, « mesure d'abord »)

Date : 17 novembre 2026 · Version 2 — **« mesure d'abord »** (réorganisation validée par l'opérateur le 17/11/2026) · Aucun code applicatif n'a été écrit.

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

Ces points doivent être tranchés avant ou pendant l'étape M1. Chacun a une proposition par défaut.

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

> **Pendant la phase de mesure (§4)**, tout tourne dans un seul processus asyncio avec PostgreSQL, sans signer ni wallet. L'architecture ci-dessous est la cible, atteinte à l'étape G2.

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

## 4. Plan de développement — version « mesure d'abord »

**Principe :** on construit d'abord le minimum qui répond à une seule question : **existe-t-il un avantage net de tous les frais ?** Le reste n'est construit que si la réponse est oui. L'ordre du cahier des charges change (décision de l'opérateur du 17/11/2026). Son contenu reste la cible finale.

### 4.1 Ce qui est reporté, et pourquoi

| Élément du cahier des charges | Pendant la mesure | Arrive à l'étape |
|---|---|---|
| Dashboard Next.js (6 pages) | Rapport en ligne de commande + fichier HTML statique | G3 |
| Grok / sentiment | Absent : le score fonctionne sans sentiment (90 points) | G1, en test A/B |
| Exécution réelle, signer, wallet, `/execute` | Absente : aucun wallet de trading n'existe | G2 |
| Redis + processus séparés | Un seul processus asyncio + PostgreSQL, suffisant pour ce volume | G2 |
| API FastAPI complète | Réduite à `/health` | G3 |
| Yellowstone gRPC | Interface seulement | Si un jour nécessaire |

### 4.2 Étapes de mesure (M1 à M5)

**M1 — Socle minimal**
- `pyproject.toml` (uv), `ruff`, `mypy --strict`, `pytest`.
- Configuration typée, logs structurés avec masquage des secrets.
- PostgreSQL + Alembic avec les tables nécessaires à la mesure : `tokens`, `pools`, `raw_events`, `token_snapshots`, `security_analyses`, `token_scores`, `agent_decisions`, `positions`, `trades`, `portfolio_snapshots`, `system_events`, `config_versions`. Montants en entiers (unités de base) ou `Decimal`.
- Docker Compose (`postgres` + `app`), endpoint `/health`, ping healthchecks.io, alertes Telegram.
- *Sortie :* `docker compose up` démarre ; migrations montantes et descendantes testées ; tests et typage verts.

**M2 — Collecte et enregistrement**
- Interface `MarketDataProvider`.
  - `PumpPortalProvider` : nouveaux tokens, migrations, trades des tokens suivis.
  - `HeliusWebSocketProvider` : secours et vérification.
  - `ReplayProvider` : rejoue les événements enregistrés.
- `raw_events` en ajout seul, déduplication, reconnexion avec backoff, détection de flux interrompu.
- **Périodes de coupure** (PC éteint, réseau perdu, fournisseur en panne) enregistrées avec leur début et leur fin. Elles sont exclues de l'évaluation. Une position paper ouverte pendant une coupure est clôturée au pire prix observé à la reprise : une coupure ne peut jamais améliorer les résultats.
- Snapshots de l'état de la courbe ou du pool pour chaque token suivi.
- Mesures : volume de données, crédits Helius consommés, délai entre création et détection.
- *Réalisé (18/11/2026) :* le flux WebSocket Helius est remplacé à ce stade par une **vérification on-chain par échantillonnage** via la RPC Helius (`getTransaction` sur 2 % des événements). Elle mesure la fiabilité de PumpPortal et le délai de détection, pour un coût en crédits négligeable. Le décodage complet des logs pump.fun via Helius est reporté à M3, où les comptes on-chain seront lus de toute façon.
- *Sortie :* 72 h sur la machine d'exécution sans trou non détecté, zéro doublon, coupure réseau simulée et rattrapée.
- **Ensuite, l'enregistrement tourne en continu.** Ces données servent à toute la suite : plus tôt il démarre, plus tôt on peut conclure.

**M3 — Sécurité et indicateurs**
- Relecture on-chain via Helius de tout ce qui sert à décider :
  - autorités mint/freeze, extensions Token-2022 ;
  - top holders, wallet du créateur ;
  - achats groupés dans le slot de création, wallets récents ou financés par une même source ;
  - flux acheteurs/vendeurs, progression de la courbe ;
  - impact de prix pour la taille visée.
- RugCheck en second avis (optionnel ; son indisponibilité ne bloque rien).
- Score déterministe sans sentiment ; chaque composante est conservée.
- Tests sur des événements réels enregistrés, y compris des cas de rug et de honeypot.
- *Sortie :* chaque token suivi reçoit une analyse horodatée ; toute donnée manquante est explicitement signalée.
- *Réalisé (18/11/2026) :* analyse en deux étages pour économiser les crédits, avec un budget journalier partagé (voir `docs/ANALYSIS.md`). L'analyse porte sur les tokens assez actifs (15 trades ou plus entre 3 et 30 min), pas sur tous les tokens suivis : c'est ce qui permet de rester dans l'offre gratuite de Helius. Défaut corrigé dans M2 : la date de première détection vient désormais du message reçu (indispensable pour les rejeux).

**M4 — Moteur paper et stratégies**
- Stratégies A (momentum confirmé), B (repli après graduation) et C (témoin aléatoire), chacune sous une version de configuration, exécutées en parallèle sur les mêmes événements.
- Risk Manager avec les mêmes limites que celles prévues pour le réel.
- Modèle de fill conservateur :
  - prix = le pire entre le calcul sur les réserves au slot T + délai et la cotation Jupiter (`/order` sans wallet) ;
  - plus les frais du protocole, les frais Jupiter, le rent, le pourboire et le slippage ;
  - échecs d'exécution simulés.
- Position Manager : prises de profit, stop, trailing, stagnation, durée max, sorties de sécurité. Tout est évalué sur le prix de vente exécutable.
- **Étiquetage des issues :** pour chaque token qui passe la sécurité, on enregistre le prix max et min et l'état (gradué ou mort) à 5, 15, 30 et 60 minutes, qu'il ait été « acheté » ou non. On peut ainsi tester d'autres règles hors ligne sur tous les candidats, sans attendre de nouveaux trades.
- *Sortie :* les trois stratégies tournent 7 jours sans erreur ; PnL vérifié à la main sur un échantillon de trades.

**M5 — Rapport de mesure et rejeu**
- Rapport (ligne de commande + HTML statique) :
  - espérance nette par trade avec intervalle de confiance (bootstrap) ;
  - profit factor, drawdown, frais et slippage moyens ;
  - performance par tranche de score, par heure et par stratégie ;
  - écart avec le témoin C ;
  - PnL net d'infrastructure.
- Rejeu des événements enregistrés pour tester une variante de paramètres, uniquement sur la période d'apprentissage.
- Résumé hebdomadaire automatique sur Telegram.
- *Sortie :* rapport reproductible à partir de la seule base de données.

### 4.3 Point de décision

Il a lieu après **au moins 3 à 4 semaines de collecte et au moins 100 trades simulés par stratégie**. Les 30 derniers % de la période sont réservés à la validation : on ne règle aucun paramètre dessus.

| Décision | Condition |
|---|---|
| **GO** | La stratégie A ou B remplit toutes les conditions : espérance nette > 0 avec borne basse de l'intervalle de confiance à 95 % > 0 ; profit factor > 1,3 ; drawdown < 10 % ; nettement meilleure que le témoin C ; confirmée sur la période réservée ; gains projetés supérieurs aux coûts d'infrastructure |
| **PROLONGER** (une seule fois) | Résultats positifs mais pas significatifs : 2 à 4 semaines de plus, sans toucher aux paramètres |
| **STOP** | Tous les autres cas. Le coût est limité à 1–2 mois d'infrastructure ; le code et les données sont conservés |

### 4.4 Si GO : construction du reste

- **G1 — Grok (optionnel).** Module sentiment testé en A/B sur les données ; conservé seulement s'il améliore les résultats.
- **G2 — Exécution réelle isolée.**
  - Signer séparé, Jupiter `/order` + `/execute`, réconciliation on-chain, kill switch persistant.
  - Séparation en processus + Redis.
  - Tests de sécurité : signature non autorisée, double exécution, transaction expirée, coupure réseau.
- **G3 — Dashboard et API.** Next.js, API complète, authentification, distinction visible paper / réel.
- **G4 — Validation finale.** Au moins 300 trades et 30 jours au total (la période de mesure compte), scénarios de panne, documentation complète.
- **G5 — Réel progressif.** Autorisation explicite de l'opérateur, positions réduites au départ (3 à 5 €), retour au paper immédiat à tout moment.

### 4.5 Budget et garde-fous du projet

- **Coût pendant la mesure :** ~10–20 €/mois avec un VPS, ou seulement l'électricité avec un PC local (~2–3 €/mois pour un portable, ~10 € pour un PC fixe). Helius Free + PumpPortal ; pas de xAI, pas de capital engagé.
- **Plafond proposé :** 300 € au total (infrastructure + éventuelles pertes réelles) et décision GO/STOP au plus tard 3 mois après le démarrage de M2. À confirmer par l'opérateur.
- **Effort estimé :** les étapes M1 à M5 représentent environ un tiers du projet complet.
- **Prérequis :**
  - M2 : une machine allumée en continu, **PC local accepté pour la mesure** (aucun argent en jeu) ou VPS ; cet environnement de développement est temporaire. Plus une clé Helius Free, une clé PumpPortal avec un wallet jetable de 0,02 SOL ;
  - M4 : une clé Jupiter gratuite.

---

## 5. API, abonnements et dépendances externes

> **Mise à jour :** une stack à moindre coût (~15–35 €/mois) est proposée dans `docs/TOOLING_AND_STRATEGY.md`. Elle ajoute PumpPortal pour la découverte et RugCheck en second avis, commence en Helius Free et rend Grok optionnel. Le tableau ci-dessous reste la référence des offres disponibles.

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

> **Mise à jour :** avec la stack de `docs/TOOLING_AND_STRATEGY.md` §2.6, le scénario minimal tombe à ~15–35 €/mois (~60–85 € si Helius Developer devient nécessaire). Les règles de trading proposées sont au §3 de ce même document.

| Poste | Minimal (paper) | Recommandé | Basse latence |
|---|---|---|---|
| VPS | 10–20 € | 15–30 € | 30–60 € |
| Helius | 0 $ (Free) | 49 $ (Developer) | 499 $ (Business, gRPC) |
| xAI (sentiment filtré : ~30–100 analyses/jour) | 5–15 $ | 15–50 $ | 15–50 $ |
| Jupiter | 0 $ | 0 $ | plan payant éventuel |
| **Total approx.** | **15–35 €** | **~80–130 €** | **~550–650 €** |

**Inconnue majeure :** le volume du flux pump.fun. Si l'abonnement aux logs des programmes pump.fun génère plusieurs dizaines de Go par jour, la consommation dépasse le quota Developer (10 M crédits ≈ 500 Go streamés/mois au tarif de 20 crédits/Mo, avant les appels RPC). L'étape M2 inclut une mesure d'une heure pour trancher. Alternatives : filtrage plus fin (abonnement aux seules transactions de création, puis suivi ciblé des tokens retenus) ou fournisseur de découverte tiers, derrière la même interface.

**Conséquence économique :** avec 150 € de capital de trading et ~100 €/mois d'infrastructure, la stratégie devrait produire plus de 60 % par mois uniquement pour couvrir ses coûts. La phase de paper trading doit être considérée comme de la R&D ; l'évaluation finale doit inclure le PnL net d'infrastructure, et le capital ou le budget sont peut-être à revoir.

---

## 8. Risques et dépendances bloquantes

### Bloquants (avant les étapes concernées)
1. **Clé API Helius** (étape M2) — Free suffit pour développer, Developer probablement nécessaire pour le flux continu.
2. **Clé API Jupiter** (étape M4, cotations) — gratuite.
3. **Clé API xAI** et confirmation qu'un modèle disposant de X Search est accessible sur le compte (étape G1, seulement si GO).
4. **Accès réseau sortant** de l'environnement de développement vers `*.helius-rpc.com`, `api.jup.ag`, `api.x.ai` pour les tests d'intégration (à configurer si bloqué).
5. **Décisions opérateur** listées au §9.

### Risques techniques
- **Concurrence et latence :** les snipers professionnels opèrent en intra-slot avec bundles ; un bot WebSocket + Jupiter a des latences de centaines de ms à quelques secondes. La stratégie « confirmations avant entrée » est adaptée, mais l'avantage statistique reste à démontrer.
- **Base rate :** la grande majorité des nouveaux meme coins perd l'essentiel de sa valeur ; les rugs et coordinations de wallets évoluent plus vite que les heuristiques.
- **Évolution des programmes** pump.fun / PumpSwap (frais, instructions, Token-2022) : décodeurs versionnés et alertes sur instructions inconnues.
- **Couverture Jupiter** des tokens en phase de bonding curve : à vérifier en étape M4 ; un adaptateur d'exécution direct sur la courbe pourrait être nécessaire (derrière l'interface `ExecutionEngine`).
- **Fiabilité de Grok :** latence, coût, hallucinations, données X rares pour les tokens très récents → poids limité (10), validation stricte, jamais de chemin direct vers une transaction.
- **Biais des backtests :** impossibilité de rejouer les cotations historiques de Jupiter ; le modèle de fill sur réserves de courbe atténue ce biais sans le supprimer.
- **Fiscalité / conformité (France) :** conserver un historique exportable de toutes les opérations réelles ; non traité techniquement au-delà de l'export.

---

## 9. Décisions demandées à l'opérateur

Validé le 17/11/2026 : réorganisation « mesure d'abord » (§4).

Encore à décider avant M1 ou M2 :

1. **Plafond du projet** : 300 € au total et 3 mois pour atteindre la décision GO/STOP (§4.5) ?
2. **Stack à moindre coût** de `docs/TOOLING_AND_STRATEGY.md` (PumpPortal + Helius Free + Jupiter) ? Adoptée par défaut pour la mesure.
3. **Machine d'exécution** : PC local pour la mesure (M2–M5). **Avant tout passage en réel (G2), migration obligatoire** vers un VPS ou une machine dédiée : la clé du wallet ne doit pas vivre sur un PC personnel.
4. **Clés** : Helius Free et PumpPortal pour M2, Jupiter pour M4. Elles ne doivent jamais être collées dans le chat : elles iront dans un fichier `.env` local non versionné ou dans les réglages de l'environnement.
5. **Langue** : code en anglais, documentation en français ?

Après validation, l'étape M1 commence et se termine par son rapport de livraison.
