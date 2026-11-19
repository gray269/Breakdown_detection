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

Dans le dossier 1, les sous-dossiers portent les numéros produit. Dans le dossier 2, les sous-dossiers portent les dates (par exemple `20261118`), puis les numéros produit ; le dossier 2 peut aussi être choisi directement sur une date. La période filtre la date de création Windows des dossiers gris et, pour le vert, la date inscrite dans le nom du dossier date. Si le vert ne contient pas de dossier date identifiable, le programme utilise la date de création du dossier produit. Les dossiers gris nommés avec un horodatage après le numéro produit (par exemple `26270014_20261118114306`) sont rattachés au numéro de base `26270014`. Dans chaque dossier produit, un seul fichier image nommé exactement `picture_2` est retenu. Les modules 16 mm et 18 mm sont regroupés.

Le dossier gris est analysé en premier. Les images sont réduites à une résolution de travail maximale de 640 pixels, identique à celle du mode essai, afin que le scan ne manque pas un point visible pendant un essai. Pour calculer le taux « dossier 2 uniquement », les images vertes des produits sans défaut gris sont aussi analysées. Les détections sont conservées dans la base locale avec le chemin, la date de modification, la taille de fichier et les réglages utilisés. Une nouvelle analyse réutilise les résultats inchangés ; si l’image ou les réglages changent, elle est recalculée. Les trois taux utilisent le même dénominateur : les numéros produit ayant au moins une image `picture_2` correctement analysée dans l’intervalle choisi. Le nombre de paires complètes est affiché séparément.

- **T1 — Dossier 1 uniquement** : claquage détecté en gris, absent en vert.
- **T2 — Présent dans les deux** : claquage détecté en gris et en vert.
- **T3 — Dossier 2 uniquement** : claquage absent en gris, détecté en vert.

Le tableau affiche le numéro produit, son résultat dans chaque dossier et sa catégorie. Les produits sans claquage dans les images analysées sont inclus au dénominateur, mais ne font partie d’aucun des trois taux. Si un seul côté est exploitable, il compte dans le dénominateur ; une détection de ce côté est attribuée au dossier correspondant. Les erreurs restent visibles dans le CSV.

## Fonctions de l’application

- Petite fenêtre avec le taux de claquage, le nombre de dossiers analysés et leur résultat.
- Le bouton `Réouvrir les paramètres` permet de rouvrir les réglages après fermeture de leur fenêtre.
- Surveillance périodique de deux dossiers facultatifs : images en niveaux de gris et images vertes.
- Barre de progression pendant la recherche et l’analyse des images.
- Analyse d’un seul fichier `picture_2` par produit dans l’intervalle choisi pour les deux sources.
- Filtrage des dossiers produit par dates de début et de fin, d’après leur date de création.
- Cache persistant des détections pour éviter de recalculer les images inchangées lors des scans suivants ou après redémarrage.
- Détection des points sombres sur le disque lumineux, y compris près de sa périphérie.
- Réglages de date, fréquence d’analyse, diamètre minimal du point et contraste.
- Mode essai pour une ou plusieurs images, avec résultat et zone détectée entourée.
- Export de l’historique en CSV.

Les résultats sont enregistrés localement dans `%LOCALAPPDATA%\SuiviClaquage`. Les images de production ne sont pas incluses dans ce projet : l’application analyse les dossiers locaux et ne téléverse pas les images vers GitHub.

## Règle de calcul

Les taux ont pour dénominateur les numéros produit ayant au moins une image correctement analysée dans la période, toutes tailles de module confondues. Une paire complète reste indiquée séparément. Si un claquage est détecté dans une seule source exploitable, il est compté dans le taux de cette source même si l’autre image manque ou ne peut pas être lue. Les erreurs sont signalées et visibles dans le CSV. Les fichiers nommés `picture_20`, `picture_21`, etc. ne sont pas sélectionnés.

Le seuil doit être vérifié à l’aide du mode essai sur des exemples dont le résultat est connu. Cette première version est un outil de tri et ne remplace pas la validation visuelle.
