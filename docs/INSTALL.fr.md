# Installer l'agent sur un site

Guide pour l'équipe instrumentation / informatique du site. Durée : environ une heure, sans modifier aucun fichier à la main.

## 0. Avant de commencer : les autorisations

L'agent lit le SCADA. Même en lecture seule, il faut l'accord du responsable SCADA et de la cybersécurité du site :

- **Un compte OPC UA en lecture seule** créé sur le serveur OPC UA du SCADA ou de l'historien, réservé à l'agent.
- **Une règle de pare-feu** dans un seul sens : du PC de l'agent vers le serveur OPC UA (port 4840 en général) ou vers les RTU en Modbus (port 502). Aucun accès entrant vers le réseau de contrôle.
- **L'emplacement du PC** : idéalement dans la zone intermédiaire (DMZ, niveau 3.5 du modèle Purdue), pas sur le réseau de contrôle.

Sans ces trois points, ne branchez rien.

## 1. Le matériel

| Élément | Recommandation |
|---|---|
| PC ou serveur | Windows 10/11 ou Windows Server, allumé en permanence, onduleur. Pas besoin d'internet |
| Python | 3.10 à 3.13. S'il n'est pas installé, mettre l'installateur `python-3.12.x-amd64.exe` (fourni par l'informatique) dans le dossier `install` : le script l'installe tout seul |
| SMS | Modem GSM USB avec une carte SIM dédiée (recommandé), ou un téléphone Android avec l'application « SMS Gateway for Android » |
| E-mail | Un compte SMTP de service (adresse dédiée à l'agent) |

## 2. Installer (10 minutes)

1. Copier le dossier du **paquet Windows hors ligne** (`agent-surveillance-puits-windows.zip`, décompressé) sur le PC, par exemple dans `C:\agent-surveillance-puits`. Il contient toutes les bibliothèques : aucune connexion internet n'est nécessaire.
2. Clic droit sur `install\install-windows.bat` > **Exécuter en tant qu'administrateur**.
3. Le script installe Python si besoin, l'environnement et les bibliothèques, enregistre l'agent comme tâche qui démarre avec Windows et redémarre s'il s'arrête, puis **ouvre l'assistant de mise en service** dans le navigateur.

## 3. L'assistant de mise en service (http://127.0.0.1:8080/setup)

Aucun fichier à modifier à la main. L'assistant n'est accessible que depuis le PC lui-même.

1. **Site** : nom, langue.
2. **SCADA** : adresse du serveur OPC UA (par exemple `opc.tcp://192.168.10.20:4840`) et le compte en lecture seule. « Tester la connexion », puis **« Explorer le SCADA et reconnaître les puits »** : l'agent lit l'arborescence du serveur et reconnaît les puits, les manifolds et leurs mesures à partir des noms des tags (pression tête, ligne, annulaires, batterie RTU, SDV, feu, intrusion…).
3. **Puits** : vérifier la proposition ligne par ligne : type de puits (éruptif, gas lift, avec eau de dessalage), manifold, répéteur radio, MAASP. Décocher un puits à ne pas surveiller. Corriger un tag si besoin (« tags »).
4. **Personnes** : liste d'astreinte dans l'ordre d'escalade, e-mails des alarmes et des rapports, e-mails des équipes, heures de relève.
5. **SMS et e-mail** : modem GSM (port `COM3`…, code PIN), ou téléphone Android, ou mode test. Bouton « Envoyer un SMS de test » et « Envoyer un e-mail de test ».
6. **Accès** : tableau de bord sur ce PC seulement, ou ouvert au réseau de la salle de contrôle ; compte superviseur.
7. **Vérifier et démarrer** : « Lire chaque tag une fois » montre la valeur, la qualité et l'âge de chaque mesure (tout doit être `good`). « Enregistrer et démarrer » : la configuration est écrite (`config\config.yaml`, les mots de passe dans `config\secrets.env`) et la surveillance démarre.

Pour changer un réglage plus tard : tableau de bord > **Paramètres** (superviseur). L'agent redémarre tout seul après l'enregistrement.

Les réglages avancés (seuils d'alarme…) se font dans le fichier, à l'étape 7 (« Voir le fichier de configuration ») ou dans `config\config.yaml`. Le Modbus TCP se configure aussi dans le fichier : voir `config\config.modbus.example.yaml`.

**Téléphone Android et réponse « OK »** : pour que la réponse « OK » acquitte l'alarme, déclarer dans l'application un webhook `sms:received` vers `http://<adresse du PC>:8080/api/sms-in` (le tableau de bord doit alors être ouvert au réseau).

## 4. Charger l'historique (recommandé)

Tableau de bord > onglet **Données** (superviseur) : envoyer l'export de l'historien des derniers mois (CSV tel qu'il sort), la liste des tags et la main courante. L'agent reconnaît les tags, nettoie les données et montre un rapport de qualité. Ensuite :

* **Ajouter à l'historique de l'agent** : l'analyse long terme (déclins, batteries, annulaires…) fonctionne dès le premier jour au lieu d'attendre des semaines ;
* **Rejouer la période (backtest)** : le rapport qui montre ce que l'agent aurait annoncé, et combien de jours avant.

Formats acceptés et ce qu'il faut demander : [DONNEES.fr.md](DONNEES.fr.md).

## 5. Les premières heures

Ouvrir http://127.0.0.1:8080. Pendant les deux premières heures, les puits sont marqués « apprentissage des valeurs normales » : l'agent observe avant de déclencher des alarmes de pression. Les alarmes de sécurité (vanne, feu, intrusion, RTU perdue) sont actives tout de suite.

Le journal technique est dans `data\agent.log`, l'historique dans `data\agent.db` (sauvegardé chaque jour dans `data\backup`).

En ligne de commande, les outils restent disponibles : `python -m agent browse` (arborescence OPC UA), `check` (lire chaque tag), `test-sms`, `test-email`, `status`.

## 6. Mise en service : le pilote

Pendant au moins un mois, l'agent tourne **en parallèle** du système actuel :

1. Chaque matin, comparer le rapport journalier avec le cahier de quart : alarmes manquées, fausses alarmes.
2. Ajuster les seuils dans `config.yaml` avec l'ingénieur production, redémarrer la tâche.
3. Ne passer l'astreinte sur l'agent qu'après accord écrit du chef de service.

Si le rapport journalier n'arrive pas à l'heure prévue, l'agent est arrêté : c'est le signal de vie à surveiller.

## 7. Rapport hebdomadaire et équipes

L'agent garde chaque jour la moyenne, le minimum et le maximum de chaque signal. Après 21 jours d'historique, il commence l'analyse long terme ; elle devient fiable après plusieurs mois. Le rapport hebdomadaire part au superviseur (`email.report_to`), le jour et l'heure fixés dans le bloc `analysis:` de `config.yaml`.

Pour que chaque équipe reçoive aussi sa propre liste de travail, renseigner ses adresses dans `analysis.teams` et mettre `dispatch_to_teams: true`. Les équipes possibles sont `production`, `instrumentation`, `telecom`, `maintenance` et `hse`.

On peut envoyer le rapport à la main depuis le tableau de bord (bouton « Rapport hebdomadaire »).

## 8. Avant la mise en service : le backtest

Avant le pilote, demander à l'équipe historien un export d'au moins 6 mois (idéalement 12) de quelques puits, et le journal des événements de la même période (fermetures, réouvertures, pannes, interventions, essais). Le détail de la demande est dans [DONNEES.fr.md](DONNEES.fr.md). Puis :

```bat
.venv\Scripts\python -m agent import --config config\config.yaml --data historien.csv --tags liste_tags.csv --guess
:: vérifier data\import\mapping.yaml avec l'instrumentation, puis :
.venv\Scripts\python -m agent import --config config\config.yaml --data historien.csv --events journal.csv --mapping data\import\mapping.yaml
:: lire data\import\quality.html, puis :
.venv\Scripts\python -m agent backtest --config config\config.yaml --csv data\import\export.csv --events data\import\events.csv
```

Le rapport `data\backtest\report.html` montre, pour chaque événement réel, si l'agent aurait prévenu et combien de jours avant. C'est le document à présenter au chef de service. Les colonnes de l'export s'appellent par défaut `PUITS-01.whp` ; sinon, utiliser `--pattern`, par exemple `--pattern "{id}_{signal}"`.

## 9. Comptes utilisateurs

Chaque personne a son compte ; tout ce qu'elle fait sur le tableau de bord est signé de son nom.

```bat
.venv\Scripts\python -m agent add-user --config config\config.yaml --name "K. Benali" --role operator
.venv\Scripts\python -m agent add-user --config config\config.yaml --name "A. Mansouri" --role supervisor
.venv\Scripts\python -m agent add-user --config config\config.yaml --name "Direction" --role viewer
```

* `viewer` : voit tout, ne change rien.
* `operator` : acquitte, écrit la main courante (notes, état des puits, interventions), saisit les essais, prend et termine les ordres de travail.
* `supervisor` : en plus, envoie les rapports, valide ou refuse les essais, crée et annule les ordres de travail.

Les mots de passe sont enregistrés sous forme hachée (jamais en clair) dans `data\users.json`. Avec des comptes, le tableau de bord peut être ouvert au réseau de la salle de contrôle (`web.host: 0.0.0.0`).

## 10. Main courante, ordres de travail, essais, relève

* **Main courante** : quand un puits est fermé, mis en essai ou en intervention sans que le SCADA le signale, l'écrire dans l'onglet « Main courante » avec la raison. Cette raison devient la cause de la production différée. Après un lavage, un changement de duse ou une réparation, écrire une « Intervention réalisée » : l'agent réapprend le puits.
* **Ordres de travail** : renseigner les adresses des équipes dans `analysis.teams`. Quand le travail est fait, cliquer « Fait » : l'agent vérifie pendant 24 h (alarmes) ou 14 jours (analyse long terme) que le problème ne revient pas.
* **Essais** : saisir chaque résultat dans l'onglet « Essais de puits » ; le superviseur le valide. Le dernier essai validé sert de débit de référence pour le rapport de production.
* **Relève** : envoyée automatiquement aux heures de `shifts.changes` (06:00 et 18:00 par défaut). Le chef de quart la complète.

## 11. Assistant

Par défaut, l'assistant répond sans IA, à partir de règles (`assistant.mode: rules`). Pour des réponses rédigées par une IA, installer [Ollama](https://ollama.com) sur le PC de l'agent ou sur un serveur du site, télécharger un modèle (`ollama pull qwen2.5:7b`), puis mettre `assistant.mode: local`. L'agent refuse toute adresse en dehors du réseau du site : les données ne sortent pas. Pour désactiver l'assistant : `assistant.mode: off`.

## 12. Surveiller l'agent lui-même

```bat
.venv\Scripts\python -m agent status --config config\config.yaml
```

Répond `RUNNING` et le code 0 si l'agent tourne et lit le SCADA, sinon un code d'erreur : à brancher sur la supervision informatique du site. L'agent sauvegarde aussi sa base chaque jour dans `data\backup` (7 jours gardés) et prévient par e-mail si le disque se remplit.

## Dépannage

| Symptôme | Cause probable |
|---|---|
| « Liaison SCADA perdue » | Serveur OPC UA arrêté, pare-feu, mot de passe changé, certificat refusé |
| Tous les puits « hors ligne » mais SCADA OK | Mauvaises adresses de tags, ou `SourceTimestamp` non renseigné par le serveur : vérifier avec `check` |
| Un puits reste en « apprentissage » | Le puits a été en alarme pendant l'apprentissage, ou sa pression n'est pas lue |
| SMS « FAILED » dans le tableau de bord | Port COM faux, SIM sans crédit ou sans réseau, PIN |
| Pas d'e-mail | Serveur SMTP, port, STARTTLS, mot de passe, adresse refusée |

## Désinstaller

`install\uninstall-windows.bat` en administrateur. Le dossier `data` (historique) est conservé.
