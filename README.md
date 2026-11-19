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

## Fonctions de l’application

- Petite fenêtre avec le taux de claquage, le nombre de dossiers analysés et leur résultat.
- Surveillance périodique de deux dossiers facultatifs : images en niveaux de gris et images vertes.
- Parcours des sous-dossiers et recherche des fichiers dont le nom contient `picture_2`.
- Détection des points sombres sur le disque lumineux, y compris près de sa périphérie.
- Réglages de date, taille du module, fréquence d’analyse, diamètre minimal du point et contraste.
- Mode essai pour une ou plusieurs images, avec résultat et zone détectée entourée.
- Export de l’historique en CSV.

Les résultats sont enregistrés localement dans `%LOCALAPPDATA%\SuiviClaquage`. Les images de production ne sont pas incluses dans ce projet : l’application analyse les dossiers locaux et ne téléverse pas les images vers GitHub.

## Règle de calcul

Le taux affiché est le nombre de dossiers analysables avec au moins un point détecté, divisé par le nombre total de dossiers analysables pour la date et la taille sélectionnées. Si les deux dossiers surveillés ont la même arborescence relative, les images grises et vertes d’un même sous-dossier sont regroupées. Les dossiers impossibles à analyser apparaissent « à vérifier » et ne sont pas inclus dans le dénominateur.

Le seuil doit être vérifié à l’aide du mode essai sur des exemples dont le résultat est connu. Cette première version est un outil de tri et ne remplace pas la validation visuelle.
