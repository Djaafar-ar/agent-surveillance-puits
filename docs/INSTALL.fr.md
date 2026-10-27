# Installer l'agent sur un site

Guide pour l'équipe instrumentation / informatique du site. Durée : une demi-journée, dont la plus grande partie pour trouver les adresses des tags.

## 0. Avant de commencer : les autorisations

L'agent lit le SCADA. Même en lecture seule, il faut l'accord du responsable SCADA et de la cybersécurité du site :

- **Un compte OPC UA en lecture seule** créé sur le serveur OPC UA du SCADA ou de l'historien, réservé à l'agent.
- **Une règle de pare-feu** dans un seul sens : du PC de l'agent vers le serveur OPC UA (port 4840 en général) ou vers les RTU en Modbus (port 502). Aucun accès entrant vers le réseau de contrôle.
- **L'emplacement du PC** : idéalement dans la zone intermédiaire (DMZ, niveau 3.5 du modèle Purdue), pas sur le réseau de contrôle.

Sans ces trois points, ne branchez rien.

## 1. Le matériel

| Élément | Recommandation |
|---|---|
| PC ou serveur | Windows 10/11 ou Windows Server, allumé en permanence, onduleur |
| Python | 3.10 ou plus récent, depuis python.org, case « Add python.exe to PATH » cochée |
| SMS | Modem GSM USB avec une carte SIM dédiée (recommandé), ou un téléphone Android avec l'application « SMS Gateway for Android » |
| E-mail | Un compte SMTP de service (adresse dédiée à l'agent) |

## 2. Installer

1. Copier le dossier `agent-surveillance-puits` sur le PC, par exemple dans `C:\agent-surveillance-puits`.
2. Clic droit sur `install\install-windows.bat` > **Exécuter en tant qu'administrateur**.
3. Le script crée l'environnement Python, installe les bibliothèques, crée `config\config.yaml` et `config\secrets.env`, et enregistre l'agent comme tâche qui démarre avec Windows.

## 3. Configurer

Ouvrir `config\config.yaml` avec le Bloc-notes. Chaque ligne est commentée. À remplir :

- `connector.url` : l'adresse du serveur OPC UA, par exemple `opc.tcp://192.168.10.20:4840`.
- `well_tags` et `manifold_tags` : le modèle d'adresse de chaque mesure. `{id}` est remplacé par le nom du puits.
- `satellites` et `manifolds` : le réseau de conduites (zone → station satellite → manifold). Le `repeater` de chaque manifold ou de chaque puits décrit le réseau radio, qui peut regrouper d'autres puits.
- `wells` : la liste des puits, avec leur type (`gas_lift`, `natural`, `natural_water`), leur manifold et leur MAASP.
- Les mesures que votre SCADA n'a pas : supprimez simplement la ligne. Plus il y a de mesures (pression ligne, pression d'arrivée, consigne gas lift, courant du panneau solaire…), plus le diagnostic est précis.
- `roster` : la liste d'astreinte, dans l'ordre d'escalade, avec les numéros au format international (+213…).
- `sms` et `email` : le modem ou le téléphone, et le serveur SMTP.

Puis ouvrir `config\secrets.env` et mettre les mots de passe (compte OPC UA, SMTP, téléphone passerelle). Ce fichier n'est lisible que par les administrateurs.

### Trouver les adresses des tags

```bat
cd C:\agent-surveillance-puits
.venv\Scripts\python -m agent browse --config config\config.yaml
```

La commande affiche l'arborescence du serveur OPC UA avec les adresses (`ns=2;s=...`) et les valeurs actuelles. Comparez avec les écrans du SCADA pour retrouver chaque mesure. Ajoutez `--node "ns=2;s=PUITS-01"` pour explorer une branche, et `--depth 5` pour descendre plus bas.

### Vérifier chaque tag

```bat
.venv\Scripts\python -m agent check --config config\config.yaml
```

Pour chaque puits et chaque mesure : la valeur, la qualité et l'âge de la donnée. Tout doit être `good` avec un âge de quelques secondes. Une ligne `MISSING` signale une mauvaise adresse. Une valeur très ancienne signale une RTU qui ne communique plus. Comparez quelques valeurs avec l'écran SCADA.

## 4. SMS et e-mail

**Modem GSM** : brancher le modem, regarder son port dans le Gestionnaire de périphériques (par exemple `COM3`) et le mettre dans `sms.port`. Si la carte SIM a un code PIN, mettre `pin_env: SIM_PIN` et le code dans `secrets.env`.

**Téléphone Android** : installer « SMS Gateway for Android », activer « Local Server », noter l'adresse, l'identifiant et le mot de passe affichés. Pour que la réponse « OK » acquitte l'alarme, déclarer dans l'application un webhook `sms:received` vers `http://<adresse du PC>:8080/api/sms-in` (le tableau de bord doit alors écouter sur le réseau, avec un mot de passe).

Tester :

```bat
.venv\Scripts\python -m agent test-sms   --config config\config.yaml --to +213XXXXXXXXX
.venv\Scripts\python -m agent test-email --config config\config.yaml --to vous@site.dz
```

## 5. Démarrer

```bat
schtasks /run /tn AgentSurveillancePuits
```

Ouvrir http://127.0.0.1:8080 sur le PC. Pendant les deux premières heures, les puits sont marqués « apprentissage des valeurs normales » : l'agent observe avant de déclencher des alarmes de pression. Les alarmes de sécurité (vanne, feu, intrusion, RTU perdue) sont actives tout de suite.

Le journal technique est dans `data\agent.log`, l'historique dans `data\agent.db`.

## 6. Mise en service : le pilote

Pendant au moins un mois, l'agent tourne **en parallèle** du système actuel :

1. Chaque matin, comparer le rapport journalier avec le cahier de quart : alarmes manquées, fausses alarmes.
2. Ajuster les seuils dans `config.yaml` avec l'ingénieur production, redémarrer la tâche.
3. Ne passer l'astreinte sur l'agent qu'après accord écrit du chef de service.

Si le rapport journalier n'arrive pas à l'heure prévue, l'agent est arrêté : c'est le signal de vie à surveiller.

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
