# Stratégies et paper trading (étape M4)

> **Paper trading uniquement.** Aucun wallet, aucune transaction, aucune signature. Les résultats sont des simulations et ne doivent jamais être présentés comme des gains réels.

Tous les paramètres sont dans `backend/trading/config.py`. Ce sont des **hypothèses de départ**, versionnées (empreinte dans `config_versions`).

## Les trois stratégies

| | Déclencheur d'entrée | Prix d'exécution |
|---|---|---|
| **A — Momentum confirmé** | Analyse M3 classée « candidat » ou « prioritaire », aucun risque critique, progression de la courbe entre 25 et 70 %, au moins 40 acheteurs uniques | Courbe (réserves exactes) ; cotation Jupiter en plus si une clé est configurée |
| **B — Repli après graduation** | Token gradué depuis moins de 2 h, dernière analyse sans risque critique, chute d'au moins 30 % depuis le plus haut post-migration puis rebond d'au moins 5 %, acheteurs majoritaires sur 2 min | **Cotation Jupiter obligatoire.** Sans clé Jupiter, B ne trade pas et l'enregistre (`no_post_migration_quotes`) |
| **C — Témoin aléatoire** | 10 % tirés au hasard parmi les tokens analysés sans risque critique (tirage reproductible) | Comme A |

Le signal de B s'appuie sur les prix implicites des trades. Ils servent **uniquement à déclencher** : aucun ordre n'est jamais simulé à un prix de dernière transaction.

Chaque stratégie a son **propre portefeuille virtuel** de 1 SOL de capital de trading, avec les mêmes limites, pour pouvoir les comparer équitablement.

## Sorties (identiques pour les trois stratégies)

| Règle | Valeur |
|---|---|
| Stop | -15 % |
| TP1 | +25 % → vente de 40 % de la quantité initiale |
| TP2 | +50 % → vente de 30 % |
| Trailing stop | Une fois TP1 pris, sortie si la valeur retombe de 20 % sous son plus haut |
| Stagnation | Moins de +5 % après 10 min (sans TP1) → sortie |
| Durée maximale | 20 min |
| Sorties de sécurité, prioritaires | Le créateur vend ; un risque critique apparaît dans une nouvelle analyse ; pression vendeuse (au moins 1 SOL vendu et au moins 2 fois plus de SOL vendus qu'achetés sur 60 s) |

Les gains sont calculés sur **ce que la vente rapporterait réellement maintenant** (valeur exécutable, tous frais déduits), comparé à la part correspondante du coût total d'entrée. Comme un vrai stop, un déclenchement ne garantit pas le prix : la vente part après la latence, au prix de ce moment-là.

## Modèle d'exécution prudent

- **Latence :** 1,5 s entre la décision et l'exécution. Le prix utilisé est celui du marché *à ce moment-là* : si le prix bouge pendant la latence, c'est au détriment de la position.
- **Prix :** le **pire** des prix disponibles (calcul exact sur la courbe, cotation Jupiter).
- **Frais :**
  - 1,25 % pour pump.fun et 0,5 % pour l'agrégateur (les ordres réels passeraient par Jupiter) ;
  - 1 % de glissement supplémentaire, par prudence ;
  - frais réseau de 0,000055 SOL par transaction, **payés même en cas d'échec**, comme sur Solana.
- **Rent du compte de token :** 0,00204 SOL payés à l'entrée, remboursés seulement à la clôture complète.
- **Échecs simulés :** 5 % des achats et 3 % des ventes. Un achat raté est enregistré comme une position fermée dont la perte est égale aux frais.
- **Token gradué sans cotation possible :** la position reste ouverte (seule la durée maximale peut la faire sortir), est valorisée à **zéro** dans les instantanés de portefeuille, et chaque tentative de vente ratée est enregistrée.

## Risk Manager (veto absolu, déterministe)

Un nouvel achat est refusé, avec une raison enregistrée, dans les cas suivants :

- kill switch actif, ou flux de données en panne ;
- pause après 3 pertes consécutives (1 h) ;
- token déjà détenu, ou acheté dans les 24 dernières heures ;
- déjà 2 positions ouvertes, ou 10 entrées dans la journée ;
- impact d'entrée supérieur à 3 % (frais compris), ou inconnu.

La **taille** est le plus petit de ces montants : 0,07 SOL ; le budget de perte journalier (3 % du capital total) ou hebdomadaire (7 %) restant ; le cash disponible moins une réserve de frais. Les positions ouvertes sont comptées comme **potentiellement perdues à 100 %**. En dessous de 0,01 SOL, pas d'entrée.

## Kill switch

```powershell
docker compose exec app python -m backend.risk.kill_switch status
docker compose exec app python -m backend.risk.kill_switch on --reason "pause manuelle"
docker compose exec app python -m backend.risk.kill_switch off
```

Il est persistant et pris en compte en moins de 5 s. Il bloque les nouvelles entrées et laisse les sorties suivre leurs règles. S'il est illisible, il est considéré **actif**.

## Redémarrage

Au redémarrage, les positions restées ouvertes sont fermées avec la raison `interrupted_by_restart` :

- leur PnL est laissé **vide**, car l'issue est inconnue, et elles sont exclues des statistiques ;
- leur coût est déduit du capital virtuel, par prudence ;
- les limites (pertes du jour et de la semaine, délai de 24 h par token) sont reconstruites depuis la base.

## Étiquetage des issues

Pour chaque token analysé, `token_outcomes` enregistre le prix de référence au moment de la première analyse, puis le plus haut, le plus bas et le dernier prix à **5, 15, 30 et 60 minutes**. S'y ajoutent la graduation et un indicateur de **coupure de données** (fenêtre à exclure). Ces étiquettes permettront en M5 de tester d'autres règles sur tous les candidats, pas seulement sur les tokens réellement tradés.

## Rejeu et reproductibilité

Le moteur n'avance qu'avec l'heure des événements reçus. Le même flux d'événements produit exactement les mêmes décisions et les mêmes exécutions (testé). C'est la base du rejeu prévu en M5.
