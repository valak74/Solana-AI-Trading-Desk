# Rapport de mesure, verdict et rejeu (étape M5)

> Tous les chiffres viennent du **paper trading** : ce sont des simulations, pas des gains réels. Le verdict n'autorise **jamais** seul le passage en réel : cette décision reste celle de l'opérateur, après les étapes G1 à G5.

## Produire le rapport

Sur la machine d'exécution :

```powershell
docker compose exec app python -m backend.reporting --sol-eur 150 --infra-eur 15 --html /tmp/rapport.html
docker compose cp app:/tmp/rapport.html .
```

- `--sol-eur` : cours SOL/EUR utilisé pour les conversions (à mettre à jour).
- `--infra-eur` : coût d'infrastructure mensuel que la stratégie doit couvrir.
- `--start` / `--end` : bornes ISO 8601 (par défaut, du premier événement enregistré jusqu'à maintenant).
- `--run-id` : `live` par défaut, ou l'identifiant d'un rejeu.

Le rapport est recalculé **uniquement depuis la base** : il est donc reproductible. Un résumé est aussi envoyé chaque semaine sur Telegram (`WEEKLY_SUMMARY_ENABLED`).

## Ce que contient le rapport

Pour chaque stratégie (A, B, et C le témoin) :

| Indicateur | Définition |
|---|---|
| Tentatives | Positions fermées au PnL connu, **achats échoués compris** : leurs frais sont un vrai coût |
| Gain moyen par tentative | PnL net moyen (tous frais, rent et échecs compris) |
| Intervalle de confiance 95 % | Bootstrap du gain moyen (2 000 rééchantillonnages, graine fixe) |
| Profit factor | Gains bruts divisés par pertes brutes |
| Drawdown max | Plus forte baisse du capital virtuel depuis un sommet, sur les résultats réalisés |
| Écart avec le témoin C | Intervalle de confiance de la différence de gain moyen entre la stratégie et C |
| Apprentissage / réservée | Gain moyen sur les 70 premiers pourcents de la période, puis sur les 30 derniers |
| Frais, écart d'exécution, taux d'échec | Coûts réels du modèle d'exécution |
| Répartitions | Par raison de sortie, par score à l'entrée, par heure d'ouverture (UTC) |
| Gain mensuel projeté | PnL de la période ramené à 30 jours, en euros |

Sont **exclues** des statistiques, mais comptées et affichées :

- les positions ouvertes pendant une **coupure de données** ;
- les positions **interrompues par un redémarrage**, dont l'issue est inconnue.

Le rapport montre aussi les **issues à 30 minutes selon le classement M3**, pour tous les tokens analysés, tradés ou non. Si le score sert à quelque chose, les classements élevés doivent montrer de meilleures issues que « reject ».

## Verdict (plan, §4.3)

Une stratégie (A ou B) obtient **GO** seulement si **toutes** ces conditions sont réunies :

1. au moins **100 tentatives** sur au moins **21 jours** ;
2. borne basse de l'intervalle de confiance du gain moyen **> 0** ;
3. profit factor **≥ 1,3** ;
4. drawdown max **< 10 %** ;
5. **nettement meilleure que le témoin C** : borne basse de l'écart **> 0** ;
6. gain moyen **positif aussi sur la période réservée** (les 30 % finaux, jamais utilisés pour régler) ;
7. gain mensuel projeté **supérieur au coût d'infrastructure**.

Sinon, trois autres verdicts sont possibles :

- **DONNÉES INSUFFISANTES** : pas assez de tentatives ou de jours ;
- **PROMETTEUR, NON CONCLUANT** : gain moyen positif, mais au moins une condition échoue ;
- **STOP** : gain moyen négatif ou nul.

Le verdict global vaut GO si une stratégie est GO. Il vaut PROLONGER si une stratégie est prometteuse ou manque de données : une seule prolongation de 2 à 4 semaines, **sans changer les paramètres**. Dans tous les autres cas, il vaut STOP.

## Rejeu : tester une variante de paramètres

```powershell
docker compose cp variante.json app:/tmp/variante.json
docker compose exec app python -m backend.trading.replay --config /tmp/variante.json
```

`variante.json` peut être partiel. Exemple :

```json
{"strategies": [{"name": "A", "min_unique_buyers": 60}, {"name": "C"}],
 "risk": {"max_open_positions": 1}}
```

- Le rejeu relit dans l'ordre les événements de marché enregistrés et les analyses M3, et les fait passer par un moteur neuf. Ses positions sont enregistrées sous un `run_id` distinct (`replay-...`), sans mélange avec le paper trading en direct.
- **Par défaut, il s'arrête au début de la période réservée.** `--include-holdout` ne sert qu'à la **validation finale** : après l'avoir utilisé, il ne faut plus modifier les paramètres, sinon la validation ne veut plus rien dire.

**Limites du rejeu :**

- on peut faire varier les entrées, les sorties, les limites de risque et le modèle d'exécution, mais **pas les analyses M3**, qui dépendent de l'état on-chain du moment ;
- la stratégie B ne peut pas trader en rejeu, car il n'existe pas de cotations Jupiter historiques.
