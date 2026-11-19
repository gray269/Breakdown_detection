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

Dans le dossier 1, les sous-dossiers de premier niveau portent les numéros produit. Dans le dossier 2, les sous-dossiers de premier niveau portent les dates, puis viennent les sous-dossiers par numéro produit. Le dossier 2 peut être choisi à la racine des dates ou directement sur un dossier de date. Les dates de début et de fin filtrent les dossiers produit selon leur date de création Windows dans les deux sources, bornes incluses. Dans chaque dossier produit, l’application retient un seul fichier image dont le nom est exactement `picture_2` (la casse de l’extension et du nom est ignorée). Les numéros produit sont associés sans tenir compte des majuscules/minuscules. Les modules 16 mm et 18 mm sont regroupés.

Le dossier gris est analysé en premier. Le premier passage se fait à résolution réduite ; les images suspectes ou ambiguës sont ensuite vérifiées à la résolution de travail supérieure. Pour calculer le taux « dossier 2 uniquement », les images vertes des produits sans défaut gris passent elles aussi par ce tri rapide. Les détections sont conservées dans la base locale avec le chemin, la date de modification, la taille de fichier et les réglages utilisés. Une nouvelle analyse réutilise les résultats inchangés ; si l’image ou les réglages changent, elle est recalculée. Les trois taux utilisent le même dénominateur : les produits ayant une image `picture_2` exploitable dans chacun des deux dossiers pour l’intervalle choisi.

- **T1 — Dossier 1 uniquement** : claquage détecté en gris, absent en vert.
- **T2 — Présent dans les deux** : claquage détecté en gris et en vert.
- **T3 — Dossier 2 uniquement** : claquage absent en gris, détecté en vert.

Le tableau affiche le numéro produit, son résultat dans chaque dossier et sa catégorie. Les produits sans claquage dans les deux images sont inclus au dénominateur, mais ne font partie d’aucun des trois taux. Les images manquantes ou impossibles à analyser sont marquées « À vérifier » et exclues du dénominateur.

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

Les taux sont calculés sur les produits comparables dont les dossiers produit ont été créés dans l’intervalle sélectionné, toutes tailles de module confondues. Les dossiers impossibles à analyser apparaissent « à vérifier » et ne sont pas inclus dans le dénominateur. Les fichiers nommés `picture_20`, `picture_21`, etc. ne sont pas sélectionnés.

Le seuil doit être vérifié à l’aide du mode essai sur des exemples dont le résultat est connu. Cette première version est un outil de tri et ne remplace pas la validation visuelle.
