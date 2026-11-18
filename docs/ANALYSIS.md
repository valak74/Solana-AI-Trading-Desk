# Analyse de sécurité et score (étape M3)

Toutes les règles sont **déterministes** et écrites en Python : aucune IA n'intervient dans ces décisions. Tous les seuils sont des **hypothèses de départ** (`backend/security/config.py`), à calibrer en M5 avec les résultats mesurés. Chaque analyse enregistre l'empreinte des paramètres utilisés (table `config_versions`).

## Quels tokens sont analysés

Toutes les 20 secondes, le service choisit des tokens pump.fun qui remplissent quatre conditions :

- détectés il y a **3 à 30 minutes** ;
- au moins **15 trades** enregistrés ;
- pas déjà analysés dans les **10 dernières minutes** ;
- toujours sur la courbe (non gradués).

Les plus actifs passent en premier. Un **budget journalier de crédits Helius** (25 000 par défaut, environ 750 000 par mois, sous le quota gratuit d'un million) plafonne le tout. Quand il est atteint, les analyses s'arrêtent jusqu'à minuit UTC, une alerte est envoyée, et l'enregistrement continue.

## Deux étages

| Étage | Contenu | Coût approximatif |
|---|---|---|
| **1. Systématique** | Compte du mint (autorités, programme, extensions Token-2022), compte de la courbe (réserves exactes, état), 20 plus gros holders, historique du créateur et flux de trades déjà enregistrés (aucun crédit) | ~4 crédits |
| **2. Approfondi** | Activité dans le bloc de création (achats groupés), profil des 5 plus gros holders (ancienneté, origine des fonds), avis RugCheck optionnel | ~10–15 crédits |

L'étage 2 ne tourne que si l'étage 1 ne trouve **aucun risque critique** et si le pré-score atteint au moins **60**. Un token sans étage 2 ne peut jamais être classé au-dessus de « surveillance ».

## Risques critiques (un seul suffit à rejeter)

| Indicateur | Signification |
|---|---|
| `mint_authority_active` / `freeze_authority_active` | Le créateur peut encore créer des tokens ou geler les comptes |
| `token2022_permanent_delegate`, `token2022_transfer_hook`, `token2022_non_transferable`, `token2022_default_account_frozen`, `token2022_pausable`, `token2022_transfer_fee_high` | Extensions Token-2022 qui peuvent empêcher ou taxer la revente |
| `token2022_unknown_extension:*` | Extension inconnue : son effet ne peut pas être évalué, donc rejet par prudence |
| `mint_unverifiable` / `curve_unverifiable` | Compte illisible, ou courbe non détenue par le programme pump.fun (donnée possiblement falsifiée) |
| `curve_complete_not_tradable` | Courbe terminée mais pas encore migrée |
| `top_holder_concentration` (≥ 20 %), `top10_concentration` (≥ 50 %), `creator_holds_large_share` (≥ 20 %) | Concentration excessive, hors courbe |
| `creator_sold` | Le créateur a déjà vendu |
| `bundled_launch` (≥ 8 transactions dans le bloc de création) | Lancement groupé |
| `coordinated_wallets` (≥ 3 gros holders financés par le même wallet), `creator_funded_holders` | Wallets coordonnés |
| `serial_creator` (≥ 10 lancements en 24 h) | Créateur en série |

Des **avertissements** existent pour les cas intermédiaires : concentration de 30 % ou plus, 4 à 7 transactions dans le bloc de création, holders majoritairement récents, impact d'entrée supérieur à 5 %, 3 lancements ou plus en 24 h, avis « danger » de RugCheck. Chaque avertissement retire 15 points au `security_score`. Toute **donnée manquante** est listée explicitement et fait passer le risque à « élevé ».

## Score d'opportunité (0–100)

| Composante | Poids | Mesure (rampe linéaire de 0 à 100) |
|---|---|---|
| Liquidité | 20 | Impact de prix à l'entrée pour la taille simulée (0,07 SOL) : ≤ 1 % → 100, ≥ 5 % → 0 |
| Momentum | 20 | Part des SOL achetés (50 % → 0, 80 % → 100) et accélération des achats (2 dernières minutes contre les 3 précédentes) |
| Qualité des wallets | 20 | Part de holders récents, achats groupés, groupes de financement (étage 2 seulement) |
| Répartition | 15 | Part du top 10 (15 % → 100, 40 % → 0) et part du créateur |
| Volume réel | 15 | Acheteurs uniques (5 → 0, 40 → 100) et part de wallets qui achètent et revendent (wash trading) |
| Sentiment | 10 | **Exclu** tant que Grok n'est pas branché : les autres poids sont renormalisés |

Toute autre composante manquante compte **zéro**. Bandes : moins de 60 rejet, 60–74 surveillance, 75–84 candidat, 85 et plus prioritaire.

## Ce que l'analyse ne fait pas

- **Elle ne simule pas la revente avant l'achat** : la RPC ne le permet pas sans détenir le token. La vendabilité est déduite des comptes. Un aller-retour immédiat sur la courbe ne coûte que les frais (environ 2,5 %) : le vrai risque est l'impact à l'entrée et l'évolution du prix ensuite.
- **Elle ne garantit pas qu'un token est sûr**, seulement qu'aucun risque connu n'a été détecté.

## Hypothèses à valider sur données réelles

- **Format de la courbe pump.fun.** On suppose 8 octets d'identifiant, puis cinq entiers de 64 bits (réserves virtuelles et réelles, offre totale), puis l'indicateur de fin de courbe. Des contrôles de cohérence rejettent toute valeur incohérente. Vérification : `python -m backend.scanner.probe --onchain 3`.
- **Coût en crédits** de `getTokenLargestAccounts` et `getMultipleAccounts` : supposé à 1 crédit, à confirmer sur le tableau de bord Helius.
- **Format de réponse de RugCheck** : non documenté. RugCheck est désactivé par défaut (`RUGCHECK_ENABLED=false`).
