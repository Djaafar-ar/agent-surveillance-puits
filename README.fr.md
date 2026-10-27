# Agent de surveillance des puits

*[English version](README.md)*

Un agent en lecture seule qui surveille des puits pétroliers jour et nuit à partir des données du SCADA. Il détecte les anomalies, trouve la cause probable, prévient l'astreinte par SMS, escalade quand personne ne répond et envoie les rapports par e-mail.

Il s'installe sur un PC ou un serveur du site, se connecte au SCADA en **OPC UA** ou en **Modbus TCP** avec un compte en lecture seule, et n'envoie jamais aucune commande aux équipements.

![Tableau de bord de l'agent](docs/img/agent-dashboard.png)

## Ce qu'il fait

- **Lit le SCADA chaque minute**, avec les mesures d'un écran puits typique : pression et température tête, pression ligne après la duse, pression d'arrivée au manifold, annulaires 7" et 9 5/8, débit d'huile calculé, débit, consigne et position de vanne gas lift, eau de dessalage, batterie et panneau solaire de la RTU, température du coffret, cartes E/S, vanne de sécurité, feu, contacts anti-intrusion, commandes à distance, puits en essai ; et pour chaque manifold les pressions des collecteurs production, test et gas lift.
- **Connaît les deux réseaux du champ** : les conduites (zone → satellite → manifold → puits) et la radio (RTU → répéteur → SCADA), qui ne regroupent pas toujours les mêmes puits.
- **Apprend les valeurs normales** de chaque puits, ou utilise celles fixées par l'ingénieur production, ou la consigne gas lift. Les valeurs normales suivent le déclin lent.
- **Détecte 24 types de problèmes**, dont la duse qui se bouche et la restriction de ligne (distinguées par la perte de charge à travers la duse et le long de la ligne), le gas lift sous la consigne vanne ouverte, la pression annulaire au-dessus du seuil API RP 90, les défauts de carte E/S et de transmetteurs, le panneau ou régulateur solaire en défaut des heures avant que la batterie soit vide, les contacts anti-intrusion, les problèmes de répéteur, de réseau gas lift, de manifold et de station satellite, et la perte de la liaison SCADA elle-même.
- **Une cause, une alarme.** Quatre puits muets en même temps donnent un seul SMS « répéteur en panne ». Un défaut de carte E/S et les trois transmetteurs qu'il fait lire négatif donnent une seule alarme. Deux manifolds qui montent ensemble donnent une seule alarme « station satellite » (suppression par conception, ISA-18.2).
- **Sait ce qui est normal** : une vanne de sécurité fermée juste après une commande à distance, un puits en essai ou qui redémarre après une fermeture ne réveillent personne.
- **Escalade par SMS** sur la liste d'astreinte (10 min par défaut) jusqu'à ce que quelqu'un réponde **OK** par SMS ou acquitte sur le tableau de bord. Un feu prévient tout le monde en même temps.
- **E-mails** pour chaque urgence et chaque fin d'alarme, **rapport journalier** (qui prouve aussi que l'agent tourne) et **relève de quart**.
- **Indicateurs d'alarmes** comparés à EEMUA 191 : alarmes par 10 min, pics, inondations, délai d'acquittement.

## Sûreté par conception

1. **Lecture seule.** Uniquement des lectures OPC UA et les codes fonction Modbus de lecture (1 à 4). Le code ne contient aucun appel d'écriture, et un test échoue si quelqu'un en ajoute un.
2. **Ce sont les règles qui décident, pas un modèle de langage.** La détection est déterministe et vérifiable.
3. **Il ne remplace ni les alarmes du SCADA ni les systèmes de sécurité.** C'est un deuxième regard pour la nuit, et chaque message le rappelle.
4. **Les données restent sur le site** : base SQLite locale, SMS par modem GSM, aucun service cloud obligatoire.
5. **Le tableau de bord n'écoute que sur la machine locale**, sauf si un mot de passe est défini.

## Installation sur un site

Le guide pas à pas pour l'équipe du site est ici : **[docs/INSTALL.fr.md](docs/INSTALL.fr.md)**.

En résumé : copier le dossier sur le PC, lancer `install\install-windows.bat` en administrateur, renseigner `config\config.yaml` et `config\secrets.env`, vérifier les tags avec `python -m agent check`, tester SMS et e-mail, puis démarrer le service.

## Essayer sans SCADA

Le simulateur joue le rôle du SCADA : l'agent s'y connecte en OPC UA exactement comme sur un site, sans savoir quel incident est déclenché.

```bash
pip install -r requirements.txt
python -m simulator.sim_server --speed 30 --scenario demo --night   # terminal 1
python -m agent run --config config/config.sim.yaml                 # terminal 2
```

Tableau de bord : http://127.0.0.1:8080. Page de contrôle des incidents : http://127.0.0.1:8090. Sous Windows, `install\demo-windows.bat` fait tout. Le scénario enchaîne en 8 minutes environ une chute de pression, une panne de répéteur, une panne du réseau gas lift, une hausse de pression annulaire, un feu, une restriction de collecte, un défaut de carte E/S et une duse qui se bouche.

Sur ce scénario, l'agent présente **8 alarmes pour 24 détections brutes**, avec un pic de 1 alarme par 10 minutes au lieu de 5. Ces chiffres viennent d'une simulation, pas du terrain.

Une version de la démo qui tourne dans le navigateur, sans installation : **[djaafar-ar.github.io/agent-surveillance-puits](https://djaafar-ar.github.io/agent-surveillance-puits/)**.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests          # moteur et bout en bout : 98 tests, environ 3 minutes
python tests/test_demo_page.py  # démo navigateur
```

## Limites

- Validé uniquement sur le simulateur et sur des données Modbus et CSV de synthèse. **Il n'a pas encore tourné sur un vrai SCADA.** L'étape suivante est un pilote en parallèle du système existant.
- Les seuils sont des valeurs de départ, à fixer avec les ingénieurs production de chaque site.
- L'état des alarmes est gardé en mémoire : après un redémarrage, les alarmes actives sont redétectées et renvoyées.
- Ce n'est pas un système de sécurité et il n'est pas certifié.

## Références

EEMUA 191 et ISA-18.2 (gestion des alarmes), API RP 90 (pression annulaire, MAASP), modèle Purdue et IEC 62443 (cybersécurité industrielle), OPC UA (IEC 62541), Modbus.

## Auteur

**Djaafar Arbane**, master en intelligence artificielle (Université de M'Sila, 2026). Mémoire sur l'IA appliquée à la récupération assistée du pétrole par surfactants.

[GitHub](https://github.com/Djaafar-ar)

## Licence

MIT, voir [LICENSE](LICENSE).
