# SuiviClaquage

Application Windows de surveillance d’images. Le projet contient un workflow GitHub Actions qui construit automatiquement un fichier `SuiviClaquage.exe` autonome sur un runner Windows. Python n’a pas besoin d’être installé sur le PC qui utilisera l’exécutable.

## Importer le projet dans GitHub depuis le navigateur

1. Télécharge et décompresse `SuiviClaquage_GitHub.zip` sur ton PC.
2. Sur GitHub, crée un dépôt nommé `SuiviClaquage`. Choisis **Private** si tu veux garder le code visible uniquement par toi et les personnes invitées. Laisse le dépôt vide lors de sa création.
3. Dans le dépôt, choisis **Add file > Upload files**.
4. Dépose le contenu du dossier décompressé dans la page, y compris le dossier caché `.github`. Si tu ne le vois pas dans l’Explorateur Windows, active **Affichage > Afficher > Éléments masqués**.
5. Valide l’envoi sur la branche `main`.

L’envoi déclenche la compilation. Le workflow est aussi lançable manuellement depuis l’onglet **Actions** avec **Run workflow**.

## Télécharger l’exécutable

Dans **Actions**, ouvre la dernière exécution terminée du workflow **Construire l'executable Windows**, puis télécharge l’artefact `SuiviClaquage-Windows`. Décompresse l’archive téléchargée et lance `SuiviClaquage.exe`.

L’artefact est conservé 30 jours. Après expiration, relance **Run workflow** pour en générer un nouveau.

## Comparaison des deux dossiers

Le **dossier 1 correspond à QIA** : ses sous-dossiers portent les numéros de module. Le **dossier 2 correspond à PHEA** : il contient des dossiers datés (par exemple `20261118`), puis les dossiers par numéro de module ; le dossier PHEA peut aussi être choisi directement sur une date. La période filtre la date de création des dossiers QIA et, pour PHEA, la date inscrite dans le nom du dossier date. Si PHEA ne contient pas de dossier date identifiable, le programme utilise la date de création du dossier module. Les dossiers QIA nommés avec un horodatage après le numéro (par exemple `26270014_20261118114306`) sont rattachés au numéro de base `26270014`. Dans chaque dossier module, un seul fichier image nommé exactement `picture_2` est retenu. Les modules 16 mm et 18 mm sont regroupés.

QIA et PHEA sont analysés séparément : chaque image `picture_2` trouvée est traitée, quel que soit le résultat de l’autre source. Les images sont réduites à une résolution de travail maximale de 640 pixels, identique à celle du mode essai. Les détections sont conservées dans la base locale avec le chemin, la date de modification, la taille de fichier et les réglages utilisés. Une nouvelle analyse réutilise les résultats inchangés ; si l’image ou un réglage change, elle est recalculée. Les taux QIA et PHEA sont calculés séparément sur les images correctement analysées. Le taux de périphérie QIA affiche à la fois la part des images QIA analysées avec un claquage périphérique et la part de ces détections QIA qui sont périphériques. Une image est dite périphérique si au moins un point accepté entre dans la zone extérieure réglée autour du disque. Une image absente ou illisible n’est jamais considérée comme une absence de claquage.

- **T1 — QIA uniquement** : claquage détecté en QIA, absent en PHEA.
- **T2 — Présent dans QIA et PHEA** : claquage détecté dans les deux sources.
- **T3 — PHEA uniquement** : claquage absent en QIA, détecté en PHEA.

Le tableau affiche le numéro produit, son résultat dans chaque dossier et sa catégorie. « À vérifier » signifie qu’au moins une image nécessaire à la comparaison manque ou n’a pas pu être analysée. Ces lignes restent visibles, mais ne sont classées ni T1, ni T2, ni T3. Les paires sans claquage dans les deux images sont incluses au dénominateur des taux T1/T2/T3, mais ne font partie d’aucune des trois catégories. Si un seul côté est exploitable, son résultat reste visible dans le taux brut de sa source ; le produit n’entre pas dans le dénominateur T1/T2/T3. Les erreurs restent visibles dans le CSV et le journal de diagnostic.

## Fonctions de l’application

- Petite fenêtre avec le taux de claquage, le nombre de dossiers analysés et leur résultat.
- Le bouton `Réouvrir les paramètres` permet de rouvrir les réglages après fermeture de leur fenêtre. `Enregistrer` garde la fenêtre ouverte ; `Fermer et enregistrer` et le bouton de fermeture de la fenêtre enregistrent aussi les changements.
- Surveillance périodique de deux dossiers facultatifs : dossier 1 QIA et dossier 2 PHEA.
- Barre de progression pendant la recherche et l’analyse des images.
- Analyse d’un seul fichier `picture_2` par produit dans l’intervalle choisi pour les deux sources.
- Filtrage des dossiers produit par dates de début et de fin, d’après leur date de création.
- Cache persistant des détections pour éviter de recalculer les images inchangées lors des scans suivants ou après redémarrage.
- Détection des points sombres sur le disque lumineux, y compris près de sa périphérie, sans inclure le fond noir extérieur au disque.
- Réglages manuels de la largeur de la zone périphérique, de l’aire minimale et du niveau de noir requis. Des seuils plus bas rendent la détection périphérique plus sensible.
- Mode essai pour une ou plusieurs images, avec résultat, contour du disque et points détectés entourés.
- Export de l’historique en CSV.
- Bouton `Journal` : affiche et exporte le journal de la dernière analyse, avec les erreurs détaillées et leurs traces techniques.
- Choix d’un dossier de destination dans les paramètres, puis bouton `Créer les archives` pour créer les ZIP de la période sélectionnée.

L’export crée un sous-dossier daté contenant quatre archives : `T1_QIA_uniquement.zip`, `T2_Present_dans_QIA_et_PHEA.zip`, `T3_PHEA_uniquement.zip` et `Photos_a_verifier.zip`. Les images des archives sont réanalysées avec les réglages choisis et enregistrées en PNG annoté : cercle jaune pour le bord du disque, rouge pour chaque claquage détecté et magenta autour des points périphériques. Chaque sous-dossier porte le numéro du module et contient les images QIA et/ou PHEA disponibles. `Photos_a_verifier.zip` peut contenir une seule image si l’autre manque. Les modules sans détection ne sont pas archivés. Le fichier `resume_archivage.txt` indique le nombre de modules par catégorie et les images qui n’ont pas pu être copiées.

## Personnaliser les textes de l’interface

Les libellés sont écrits directement dans `claquage_monitor/app.py` :

- Fenêtre principale, titre, en-tête, explications et boutons : méthode `MonitorApp._build_main_window`.
- Paramètres, noms des champs, textes d’aide et boutons : constructeur `SettingsWindow.__init__`.
- Fenêtre d’essai des images : constructeur `ImageTestWindow.__init__`.
- Noms des catégories et des fichiers ZIP : constantes `ARCHIVE_CATEGORIES` et `MonitorApp.category_names`.

Tu peux rechercher le texte exact dans `app.py`, puis le remplacer. Pour changer les noms des catégories, modifie aussi les textes de logique associés à `_classify_states`, car les catégories servent également aux calculs et au classement des résultats. Après une modification, il faut recréer l’exécutable avec GitHub Actions.

Les résultats et les journaux (30 derniers scans) sont enregistrés localement dans `%LOCALAPPDATA%\SuiviClaquage`. Le journal contient les chemins des dossiers et numéros produit ainsi que les détails d’erreur, mais aucune image. Les images de production ne sont pas incluses dans le dépôt GitHub : l’application analyse les dossiers locaux et ne téléverse pas les images vers GitHub. Les archives créées sont écrites uniquement dans le dossier de destination choisi.

## Règle de calcul

Les taux bruts QIA et PHEA ont chacun leur propre dénominateur, correspondant aux images correctement analysées dans la source. Le taux de périphérie est donné sur les images QIA connues et sur les détections QIA connues. T1, T2 et T3 ont pour dénominateur les paires comparables uniquement. Une image absente ou illisible reste « à vérifier » pour la comparaison. Les erreurs et le statut périphérique QIA apparaissent dans le CSV. Les fichiers nommés `picture_20`, `picture_21`, etc. ne sont pas sélectionnés.

Le seuil doit être vérifié à l’aide du mode essai sur des exemples dont le résultat est connu. Cette première version est un outil de tri et ne remplace pas la validation visuelle.
