# Outils à moindre coût et règles de trading — Proposition

Date : 17 novembre 2026 · Statut : **proposition, en attente de validation** · Complète `docs/DEVELOPMENT_PLAN.md`.

Les prix proviennent des pages fournisseurs et de comparatifs consultés le 17/11/2026. Ils changent souvent : chaque chiffre marqué « à vérifier » doit être confirmé au moment de souscrire.

---

## 1. Synthèse

- **Stack minimale viable : ~15 à 35 €/mois**, contre ~80 à 130 € dans le plan initial. Elle tient presque dans le budget de 30 €.
- **Remplacés ou reportés :** LaserStream gRPC (trop cher, inutile pour une stratégie à confirmation), l'abonnement Helius Developer d'emblée (on commence en Free et on mesure), l'API Lightning de PumpPortal (custodiale, 1 % de frais).
- **Grok est conservé mais rétrogradé :** c'est l'accès le moins cher aux données X, mais il devient optionnel, réservé aux meilleurs candidats, et son utilité sera **mesurée** en paper. Il est désactivé s'il n'apporte rien.
- **Ajoutés (gratuits) :** RugCheck en second avis sécurité, GeckoTerminal/DexScreener en contre-vérification de prix, alertes Telegram, flux PumpPortal pour la découverte.
- **Règle n°1 de stratégie :** ne pas acheter à la création. Entre 0,2 % et ~2 % seulement des tokens pump.fun atteignent la graduation selon les périodes (1,8 % en août 2026). Acheter tout à T0 revient à parier contre ces probabilités.
- **Règle n°2 :** tester plusieurs variantes de stratégie **en parallèle en paper**, dont une variante témoin aléatoire, et laisser les données trancher.

---

## 2. Outils : choix retenus et remplacements

### 2.1 Données temps réel et découverte

| Rôle | Choix proposé | Coût | Remplace | Pourquoi |
|---|---|---|---|---|
| Découverte + flux de trades par token | **PumpPortal Data API** (WebSocket `subscribeNewToken`, puis abonnement aux trades des seuls tokens suivis) | ~0 € (clé API + wallet lié contenant 0,02 SOL, à vérifier) | Flux complet des logs pump.fun via Helius | Beaucoup moins de données reçues donc moins de crédits consommés ; flux déjà décodé |
| Vérité on-chain (comptes, holders, simulation, confirmation) | **Helius RPC — offre Free** (1 M crédits, 10 req/s) | 0 $ | Helius Developer d'emblée | On ne paie que si la mesure de consommation le justifie |
| Passage à l'échelle | Helius Developer (49 $/mois, 10 M crédits, 50 req/s) | 49 $ | — | Seulement si Free est saturé |
| Flux basse latence | **Reporté.** Si un jour nécessaire : add-on Yellowstone gRPC Chainstack (~49 $/mois pour 2 flux, plus le plan de base) ou facturation au volume (dRPC, Triton) | — | Helius LaserStream (Business, 499 $/mois) | Une stratégie à confirmation de plusieurs minutes ne gagne rien à quelques ms de moins |
| Contre-vérification prix/liquidité post-graduation | **GeckoTerminal** (30 appels/min gratuits, endpoint `new_pools`) et **DexScreener** (~60/min, à vérifier) | 0 € | — | Second avis indépendant, détection d'incohérences de données |

**Règle de sécurité PumpPortal :** c'est un tiers. Ses données servent uniquement à **déclencher** l'analyse. Toute donnée utilisée pour une décision d'achat (autorités, réserves, holders) est relue on-chain via Helius. Le wallet lié à la clé PumpPortal est un wallet jetable contenant 0,02 SOL, **jamais** le wallet de trading. Comme tous les fournisseurs, PumpPortal est derrière l'interface `MarketDataProvider` et peut être remplacé par Helius WebSocket sans toucher à la stratégie.

### 2.2 Sécurité des tokens

| Rôle | Choix | Coût |
|---|---|---|
| Analyse de référence | Moteur interne (autorités, extensions Token-2022, holders, wallets liés, vendabilité inférée) | 0 € |
| Second avis | **RugCheck API** (lecture publique sans clé, à vérifier) | 0 € |

Le moteur interne reste l'autorité : RugCheck peut ajouter un avertissement ou un flag critique, mais son indisponibilité ne bloque pas l'analyse ; elle est simplement journalisée.

### 2.3 Exécution

| Option | Frais | Rôle proposé |
|---|---|---|
| **Jupiter Swap API V2 `/order`** (sans wallet = cotation seule) | Gratuit en cotation | **Cotations du paper trading** |
| **Jupiter `/order` + `/execute`** | Frais Jupiter inclus dans la cotation (`feeBps`, ~10 pb sur la plupart des paires ; jusqu'à ~50 pb sur tokens de moins de 24 h selon une source secondaire, à vérifier) | **Live v1** : le plus simple, Jupiter gère l'envoi et le landing |
| **Jupiter `/build` + envoi via Helius Sender `swqos_only`** | Pas de frais Jupiter sur `/build` ; pourboire minimal 0,000005 SOL | **Optimisation v2** si le paper montre que les frais pèsent |
| Instructions pump.fun construites en interne + Helius Sender | Seulement les frais pump.fun + réseau | Option v3, seulement si Jupiter route mal les tokens en courbe |
| ~~PumpPortal Lightning API~~ | 1 % par trade, wallet généré par le tiers | **Rejeté** : custodial et cher |

### 2.4 Analyse qualitative (Grok et alternatives)

| Option | Coût | Accès aux données X | Verdict |
|---|---|---|---|
| **Grok 4 Fast + outil `x_search`** | ~0,20 $ / 0,50 $ par M tokens + ≤ 5 $ / 1000 recherches (à vérifier) | **Oui, natif** | **Retenu, en module optionnel** |
| Grok 4.3 | 1,25 $ / 2,50 $ par M tokens | Oui | Inutilement cher pour cet usage |
| API X officielle (paiement à l'usage) | ~0,005 $ par post lu, ~0,01 $ par compte lu ; une recherche de 100 posts ≈ 0,50 $ | Oui (données brutes) | Trop cher pour la recherche. Possible plus tard pour vérifier un seul compte X lié au token (~0,01 $) |
| Claude Haiku 5.5 + recherche web | 0,10 $ / 0,50 $ par M tokens + coût par recherche | Non (web seulement, X peu indexé) | Solution de secours si Grok est indisponible, pas un remplaçant pour X |

**Comment Grok est utilisé :**
1. Il n'est appelé que si le pré-score (90 points sans le sentiment) peut atteindre le seuil d'achat, avec un plafond d'appels par jour (ex. 50).
2. Chaque analyse est mise en cache par token pendant 10 minutes.
3. Si la réponse est absente, invalide ou trop lente : composante neutre, confiance 0.
4. **Test d'utilité** après 150 trades simulés au moins : on compare la performance de la même stratégie avec et sans la composante sentiment. Si l'écart n'est pas significatif, Grok est désactivé (économie de 5 à 15 $/mois). Un fournisseur alternatif peut être branché via l'interface `SentimentProvider`.

### 2.5 Infrastructure et exploitation

| Rôle | Choix | Coût |
|---|---|---|
| Serveur | VPS 2 vCPU / 4 Go à Francfort ou Amsterdam (zones denses en validateurs Solana). Hetzner CX23 ~5,5 €/mois si disponible (disponibilité incertaine), sinon CPX22 ~19,5 €/mois ou équivalent OVH/Scaleway | 6 à 20 € |
| PostgreSQL + Redis | Auto-hébergés sur le même VPS (Docker Compose) | 0 € |
| Alertes | Bot Telegram (kill switch déclenché, flux coupé, perte limite atteinte, transaction échouée) | 0 € |
| Surveillance de disponibilité | healthchecks.io (offre gratuite) | 0 € |
| Secrets | Docker secrets + chiffrement `age` de la clé du wallet | 0 € |
| Sauvegardes | `pg_dump` quotidien chiffré vers un stockage objet bon marché | < 1 € |

### 2.6 Coût mensuel estimé

| Poste | Minimal | Si Helius Free ne suffit pas |
|---|---|---|
| VPS | 6–20 € | 6–20 € |
| Helius | 0 $ | 49 $ |
| PumpPortal data | ~0 € (0,02 SOL immobilisé) | ~0 € |
| xAI (≤ 50 analyses/jour) | 5–15 $ | 5–15 $ |
| Jupiter, RugCheck, GeckoTerminal, Telegram | 0 € | 0 € |
| **Total** | **~15–35 €** | **~60–85 €** |

---

## 3. Règles de trading proposées

Aucune règle n'est « la meilleure » a priori : ce sont des hypothèses de départ raisonnables, qui seront confirmées ou rejetées par le paper trading. Tous les seuils sont configurables.

### 3.1 Ce qu'il faut éviter (constats solides)

- **Acheter à la création (T0)** : ~98 % des tokens n'atteignent pas la graduation, et les bots professionnels sont plus rapides de plusieurs ordres de grandeur.
- **Optimiser le taux de réussite** : une stratégie à 70 % de gains peut perdre de l'argent si les pertes sont des rugs à -100 %.
- **Juger sur le dernier prix** : seule compte la cotation exécutable pour la taille réelle, frais inclus.
- **Moyenner à la baisse, racheter un token déjà perdant, suivre un influenceur sans vérification on-chain.**

### 3.2 Variantes à tester en parallèle en paper

Le paper trading ne coûte rien de plus : on fait tourner plusieurs stratégies simultanément sur les mêmes événements, chacune sous sa propre version de configuration.

**A — Momentum confirmé sur la courbe (stratégie principale)**

Toutes les conditions doivent être réunies :
- âge du token entre 3 et 30 minutes ;
- progression de la courbe entre 25 et 70 % ;
- au moins 40 acheteurs uniques, dont une part limitée de wallets créés récemment ou financés par la même source ;
- flux net acheteur positif et en accélération sur les 2–3 dernières minutes ;
- top 10 des holders (hors courbe) < 30 % de l'offre ;
- le créateur détient < 5 % et n'a pas vendu ;
- pas de bundle à la création (nombre limité de wallets achetant dans le slot de création) ;
- aucune extension Token-2022 dangereuse, aucune autorité mint/freeze active ;
- route de vente Jupiter existante, impact de prix < 2 % pour la taille visée ;
- score ≥ 75 et validation du Risk Manager.

**B — Repli après graduation**
- Token migré vers PumpSwap, liquidité plus profonde, donc slippage plus faible.
- Attendre la chute qui suit généralement la migration, puis entrer seulement quand le prix forme un plus-haut-bas (higher low) avec un flux acheteur qui reprend.
- Le mécanisme BOOST (rachats et burns automatiques après migration, par défaut depuis juillet 2026) pourrait soutenir le prix. Hypothèse à vérifier par les données, pas un fait acquis.

**C — Témoin aléatoire (obligatoire)**
- Entrée au hasard parmi les tokens qui passent **uniquement** les filtres de sécurité, avec les mêmes règles de sortie.
- Si A ou B ne battent pas nettement C, le scoring n'apporte pas de valeur. Sans témoin, on ne peut pas le savoir.

### 3.3 Règles de sortie (adaptées aux frais)

Avec 4 à 10 % de coûts aller-retour, les sorties doivent viser des mouvements suffisamment grands :

| Règle | Valeur initiale | Remarque |
|---|---|---|
| TP1 | +25 % → vendre 40 % | Évalué sur la cotation de vente exécutable, nette de frais |
| TP2 | +50 % → vendre 30 % | |
| Reste (30 %) | Trailing stop à 15–20 % sous le plus-haut | Un trailing plus serré est déclenché par le bruit normal des meme coins |
| Stop | -15 % sur cotation exécutable | Ne garantit pas le prix de sortie |
| Stagnation | Sortie si PnL < +5 % après 10 min | À tester ; évite d'immobiliser le capital |
| Durée max | 20 min | |
| Sortie de sécurité immédiate | Le créateur vend ; un top holder vend > X % ; flux vendeur net sur 60 s au-delà d'un seuil ; flag critique apparu | Prioritaire sur tout le reste |
| Après sortie totale | Fermeture du compte de token | Récupère le rent (~0,002 SOL) |

### 3.4 Taille des positions et limites

- Taille = **min**(position max, budget de perte journalière restant, taille maximale pour un impact de prix < 2 %).
- Le risque d'une position est compté comme **100 % du montant engagé** (le stop ne protège pas d'un rug).
- 2 positions simultanées max, 10 entrées par jour max (configurables).
- Pause d'une heure après 3 pertes consécutives.
- Perte journalière atteinte : plus d'achats jusqu'au lendemain. Perte hebdomadaire atteinte : plus d'achats jusqu'à la semaine suivante.
- Toujours conserver au moins 0,05 SOL pour les frais (bloquant).
- Pas de nouvel achat sur un token déjà tradé dans les dernières 24 h.
- Performance suivie par tranche horaire. Restriction des heures de trading **seulement** si les données le justifient.

### 3.5 Méthode de validation (contre le sur-apprentissage)

1. Chaque variante et chaque jeu de paramètres porte un identifiant de version de configuration.
2. Pas de jugement sur une variante avant **100 trades simulés** au minimum.
3. Les 30 derniers % de la période d'observation sont réservés : on règle les paramètres sur le début et on valide sur la fin, sans retoucher.
4. Le critère est l'**espérance nette par trade** (tous frais inclus) avec un intervalle de confiance (bootstrap). La borne basse doit être positive. Profit factor > 1,3 et drawdown < 10 % en complément.
5. Une ligne « PnL net d'infrastructure » est toujours affichée.
6. **Résultat accepté d'avance :** si aucune variante ne passe ces critères après 300 trades, on ne passe pas en réel. C'est une conclusion valable, pas un échec du logiciel.

---

## 4. Décisions à valider

1. Adopter la stack minimale du §2 (PumpPortal + Helius Free + Jupiter + Grok optionnel + VPS), avec montée en gamme Helius seulement sur mesure ?
2. Garder Grok sous forme de module optionnel testé (§2.4), ou le désactiver dès le départ et l'activer plus tard ?
3. Tester en parallèle les variantes A, B et C du §3.2 ?
4. Adopter les règles de sortie et de taille des §3.3–3.4 comme paramètres initiaux ?
