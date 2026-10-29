# Données demandées pour tester l'agent sur un vrai champ

Ce document accompagne une demande de données. Il dit exactement quoi exporter, dans quel format, et ce qui sera fait des données.

## Ce qui est fait avec les données

L'agent rejoue l'historique comme s'il le voyait en direct, puis compare ce qu'il aurait dit avec ce qui s'est vraiment passé (le journal des événements). Le résultat est un rapport : pour chaque fermeture, panne ou intervention, l'agent aurait-il prévenu, et combien de jours avant ; ce qu'il aurait manqué ; combien de fausses alarmes.

**Confidentialité** : les données restent sur un seul ordinateur, ne sont publiées nulle part (ni GitHub ni ailleurs) et sont supprimées à la fin de l'essai. Les noms des puits peuvent être remplacés (P01, P02...) avant l'envoi. Un engagement écrit peut être signé.

## 1. L'export de l'historien

**Périmètre** : 5 à 10 puits, de préférence du même manifold, sur 6 à 12 mois.

**Signaux** (ceux qui existent ; aucun n'est obligatoire sauf la pression tête) :

| Signal | Exemple de libellé |
|---|---|
| Pression tête | Pression tête de puits |
| Température tête | Température tête de puits |
| Pression aval duse | Pression ligne, aval duse |
| Pression annulaire (9 5/8, 7") | Pression annulaire |
| Tension batterie RTU | Tension batterie |
| Pression collecteur du manifold | Pression collecteur production |
| Débit gas lift (puits gas lift) | Débit injection gas lift |

**Pas de temps** : une valeur par heure suffit (moyenne ou valeur instantanée). Une valeur par minute est meilleure. Un export « valeurs archivées » (enregistrées quand elles changent) convient aussi.

**Format** : celui que l'historien sait sortir. L'outil d'import accepte :

* le format long, une ligne par valeur : `Tag ; Horodatage ; Valeur ; Qualité` ;
* le format large, une colonne par tag : `Horodatage ; TAG1 ; TAG2 ; ...` ;
* séparateur `;` `,` ou tabulation, virgule décimale, dates `jj/mm/aaaa hh:mm:ss` ou ISO, encodage Windows ou UTF-8, lignes de titre au-dessus du tableau ;
* les valeurs de mauvaise qualité (« Comm Failure », « I/O Timeout »...) : il faut les laisser, elles indiquent les pertes de communication.

**La liste des tags**, si elle existe : `Tag ; Description ; Unité`. Elle permet de reconnaître automatiquement quel tag correspond à quel signal, et l'unité (bar, psi, kPa...).

## 2. Le journal des événements (le plus important)

Sans lui, on ne peut pas savoir si l'agent aurait prévenu à temps. Une copie de la main courante ou d'un tableau Excel suffit, sur la même période :

`Date ; Heure ; Puits ; Événement ; Observations`

Exemples de lignes utiles : fermeture d'un puits (et la raison), réouverture, panne (RTU, répéteur, vanne, fuite), lavage, changement de duse, remplacement de batterie, réétalonnage, purge annulaire, essais de puits.

Le texte peut être écrit librement en français : l'outil reconnaît le type d'événement à partir des mots (fermeture, réouverture, panne, lavage, changement, essai...) et le puits à partir de son numéro. Les lignes qu'il ne comprend pas sont listées pour être vérifiées à la main ; rien n'est deviné en silence.

## 3. Ce qui se passe ensuite

```bat
python -m agent import   --config config\config.yaml --data export.csv --tags liste_tags.csv --guess
python -m agent import   --config config\config.yaml --data export.csv --events journal.csv --mapping data\import\mapping.yaml
python -m agent backtest --config config\config.yaml --csv data\import\export.csv --events data\import\events.csv
```

1. L'outil propose la correspondance tags → signaux (`mapping.yaml`), à vérifier une fois avec l'instrumentation.
2. Il nettoie l'export et produit un rapport de qualité des données (`quality.html`) : couverture de chaque signal, valeurs de mauvaise qualité, valeurs aberrantes retirées, transmetteurs figés, unités converties.
3. Le backtest produit le rapport final (`report.html`), remis à l'équipe qui a fourni les données.

## Essayer sans données réelles

Un export simulé, avec les défauts habituels (encodage Windows, virgules décimales, valeurs archivées irrégulièrement, pertes de communication, valeurs aberrantes, transmetteur figé, annulaires en psi, journal écrit à la main), permet de tout essayer avant :

```bash
python -m simulator.site_export --out data/site
python -m agent import --config config/config.sim.yaml --data data/site/historien_export.csv --tags data/site/liste_tags.csv --guess
python -m agent import --config config/config.sim.yaml --data data/site/historien_export.csv --events data/site/journal_exploitation.csv --mapping data/import/mapping.yaml
python -m agent backtest --config config/config.sim.yaml --csv data/import/export.csv --events data/import/events.csv
```

Sur cet export simulé : 63 tags reconnus sur 63, environ 160 valeurs aberrantes et 130 valeurs de mauvaise qualité retirées, le transmetteur figé trouvé, et le backtest donne le même résultat que sur les données propres (8 problèmes annoncés à l'avance sur 11, 2 vus au moment où ils arrivent, 1 manqué). Ce sont des données simulées : seul un vrai export dira ce que vaut l'agent.
