# Installation sur un PC Windows 11 (phase de mesure)

Ce guide installe l'enregistreur (étape M2) sur votre PC. Il n'y a **aucun wallet de trading et aucun argent en jeu** à ce stade.

> **Avant tout passage en réel**, le système devra migrer vers un VPS ou une machine dédiée : la clé d'un wallet de trading ne doit jamais se trouver sur un PC personnel.

## 1. Prérequis

- Windows 11, 8 Go de RAM minimum, 20 Go d'espace disque libre.
- Une connexion stable (filaire de préférence).
- Un compte GitHub ayant accès au dépôt.

## 2. Installer WSL2 et Docker Desktop

1. Ouvrir **PowerShell en administrateur** (clic droit sur Démarrer → « Terminal (administrateur) »), puis :
   ```powershell
   wsl --install
   ```
   Redémarrer le PC quand c'est demandé.
2. Télécharger et installer **Docker Desktop** depuis le site officiel de Docker.
3. Dans Docker Desktop → **Settings** :
   - *General* : cocher **Use the WSL 2 based engine** et **Start Docker Desktop when you sign in to your computer** ;
   - *Resources* : 2 CPU et 4 Go de mémoire suffisent.
4. Vérifier dans un terminal :
   ```powershell
   docker --version
   docker compose version
   ```

## 3. Récupérer le projet

Installer **Git for Windows** (site officiel de Git), puis :

```powershell
cd $HOME
git clone https://github.com/valak74/Solana-AI-Trading-Desk.git
cd Solana-AI-Trading-Desk
git checkout ccr-44ea243f-8w3syv
```

Git ouvrira une fenêtre de connexion GitHub la première fois.

## 4. Obtenir les clés

| Service | Où | Remarques |
|---|---|---|
| **PumpPortal** | pumpportal.fun, section API / clé | La clé est liée à un wallet. Utiliser un **wallet jetable** avec 0,02 SOL, jamais un wallet qui servira au trading. |
| **Helius** | dashboard.helius.dev | Offre gratuite. Copier la clé API. |
| **Jupiter** (recommandé) | Portail développeur Jupiter | Clé gratuite. Cotations seulement, aucune transaction. Nécessaire à la stratégie B. |
| **Telegram** (optionnel) | Dans Telegram : `@BotFather` → `/newbot` | Donne le *token*. Envoyer ensuite un message à votre bot, puis ouvrir `https://api.telegram.org/bot<TOKEN>/getUpdates` dans un navigateur pour lire votre `chat.id`. |
| **healthchecks.io** (optionnel) | healthchecks.io, créer un check | Période 1 min, délai de grâce 5 min. Copier l'URL de ping. Vous serez prévenu si le PC s'éteint ou si le flux s'arrête. |

## 5. Configurer

```powershell
copy .env.example .env
notepad .env
```

Remplir au minimum :

- `POSTGRES_PASSWORD` (lettres et chiffres, au moins 16 caractères) ;
- `PUMPPORTAL_API_KEY` ;
- `HELIUS_API_KEY`.

Ce fichier contient vos secrets : il n'est jamais envoyé sur GitHub (`.gitignore`). **Ne collez jamais son contenu dans un chat.**

## 6. Tester la connexion au flux (1 minute)

```powershell
docker compose build
docker compose run --rm --no-deps app python -m backend.scanner.probe --seconds 60 --onchain 3 --jupiter
```

Le résultat affiche le nombre de messages reçus par type, les noms des champs vus, et le décodage on-chain de 3 nouveaux tokens (environ 6 crédits Helius). Il ne contient aucun secret : **envoyez-le-moi**. Il permet de vérifier que le format réel des messages correspond à ce que le code attend. La documentation PumpPortal n'était pas accessible depuis l'environnement de développement.

## 7. Lancer l'enregistrement

```powershell
docker compose up -d
```

Puis ouvrir `http://127.0.0.1:8000/health/ready` dans un navigateur :

- `"feed": true` : les données arrivent ;
- HTTP 503 : quelque chose ne va pas, voir les logs.

## 8. Laisser tourner

- **Paramètres → Système → Alimentation** : mise en veille sur **Jamais** (sur secteur).
- **Paramètres → Windows Update → Options avancées → Heures d'activité** : les régler pour éviter les redémarrages imprévus.
- Après un redémarrage, Docker Desktop ne repart **qu'une fois votre session ouverte**. Les conteneurs reprennent seuls ensuite (`restart: unless-stopped`).
- Chaque coupure est enregistrée (table `feed_gaps`) et exclue de l'évaluation : une coupure fait perdre du temps de mesure, pas la fiabilité des résultats.

## Commandes utiles

| Action | Commande |
|---|---|
| Voir les logs | `docker compose logs -f app` |
| Arrêter | `docker compose stop` |
| Redémarrer | `docker compose up -d` |
| Mettre à jour le code | `git pull` puis `docker compose up -d --build` |
| Statistiques horaires | `docker compose exec postgres psql -U desk -d desk -c "select created_at, payload from system_events where event_type='ingest_stats' order by id desc limit 3"` |
| Kill switch (bloquer les entrées) | `docker compose exec app python -m backend.risk.kill_switch on --reason "..."` (puis `off`) |
| Positions paper | `docker compose exec postgres psql -U desk -d desk -c "select strategy, exit_reason, realized_pnl_lamports from positions order by id desc limit 20"` |
| Coupures enregistrées | `docker compose exec postgres psql -U desk -d desk -c "select * from feed_gaps order by id desc limit 10"` |

**Ne jamais lancer `docker compose down -v`** : l'option `-v` efface la base de données, donc toutes les données enregistrées.
