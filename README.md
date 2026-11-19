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

Dans le dossier 1, les sous-dossiers de premier niveau portent les numéros produit. Dans le dossier 2, un sous-dossier de regroupement précède les sous-dossiers par numéro produit. Le logiciel associe les produits à partir du nom du dossier numéro, sans tenir compte des majuscules/minuscules. Il cherche le motif configuré (par défaut `picture_2`) dans les images du produit.

Pour calculer le taux « dossier 2 uniquement », le logiciel doit aussi analyser les images vertes des produits qui n’ont pas été détectés en gris. Les trois taux affichés utilisent le même dénominateur : le nombre de produits ayant une image analysable dans chacun des deux dossiers.

- **T1 — Dossier 1 uniquement** : claquage détecté en gris, absent en vert.
- **T2 — Présent dans les deux** : claquage détecté en gris et en vert.
- **T3 — Dossier 2 uniquement** : claquage absent en gris, détecté en vert.

Le tableau affiche le numéro produit, son résultat dans chaque dossier et sa catégorie. Les produits sans claquage dans les deux images sont inclus au dénominateur, mais ne font partie d’aucun des trois taux. Les images manquantes ou impossibles à analyser sont marquées « À vérifier » et exclues du dénominateur.

## Fonctions de l’application

- Petite fenêtre avec le taux de claquage, le nombre de dossiers analysés et leur résultat.
- Le bouton `Réouvrir les paramètres` permet de rouvrir les réglages après fermeture de leur fenêtre.
- Surveillance périodique de deux dossiers facultatifs : images en niveaux de gris et images vertes.
- Parcours des sous-dossiers et recherche des fichiers dont le nom contient `picture_2`.
- Détection des points sombres sur le disque lumineux, y compris près de sa périphérie.
- Réglages de date, taille du module, fréquence d’analyse, diamètre minimal du point et contraste.
- Mode essai pour une ou plusieurs images, avec résultat et zone détectée entourée.
- Export de l’historique en CSV.

Les résultats sont enregistrés localement dans `%LOCALAPPDATA%\SuiviClaquage`. Les images de production ne sont pas incluses dans ce projet : l’application analyse les dossiers locaux et ne téléverse pas les images vers GitHub.

## Règle de calcul

Les taux sont calculés sur les produits comparables pour la date et la taille sélectionnées. Les dossiers impossibles à analyser apparaissent « à vérifier » et ne sont pas inclus dans le dénominateur.

Le seuil doit être vérifié à l’aide du mode essai sur des exemples dont le résultat est connu. Cette première version est un outil de tri et ne remplace pas la validation visuelle.
