# Calendar Alarm — une alarme avant chaque rendez-vous

*[English version](README.en.md)*

Petit programme Windows qui tourne en arrière-plan et sonne **10 minutes avant
chaque rendez-vous** de vos agendas (Infomaniak, Google, Outlook, iCloud…), que
le rendez-vous porte un rappel ou non et quel que soit son créateur. Une
fenêtre passe au premier plan avec le titre du rendez-vous et deux boutons :
**Arrêter** et **Rappel 5 min**.

Les agendas sont relus toutes les minutes : un rendez-vous ajouté à la dernière
minute sonne dès qu'il est découvert.

## Installation

1. Lancez `CalendarAlarm-Setup-<version>.exe` (aucun droit administrateur
   requis). Le fichier n'étant pas signé, Windows affiche un avertissement
   SmartScreen : « Informations complémentaires » → « Exécuter quand même ».
2. À la fin de l'installation, la fenêtre **« Comptes et agendas »** s'ouvre.
   Choisissez votre fournisseur dans la liste déroulante, cliquez sur
   « Ajouter… » et suivez les indications affichées. Recommencez pour chaque
   agenda à surveiller.

Le programme s'inscrit lui-même au démarrage de Windows. Tout le reste passe
par un **clic droit** sur l'icône de la zone de notification : tester l'alarme,
gérer les comptes et les agendas, changer de langue (français ou anglais),
désactiver le lancement au démarrage, quitter.

## Agendas pris en charge

| Fournisseur | Ce qu'il faut saisir |
| --- | --- |
| Infomaniak (kSuite, Kalendar) | Identifiant de synchronisation et mot de passe d'application, affichés sur <https://config.infomaniak.com> (« Mes Contacts & Agendas » → « Synchronisation manuelle ») |
| Google Agenda | L'adresse secrète de l'agenda : Paramètres → votre agenda → « Intégrer l'agenda » → « Adresse secrète au format iCal » |
| Outlook / Microsoft 365 | Le lien ICS de l'agenda publié : Outlook sur le web → Paramètres → Calendrier → Calendriers partagés → « Publier un calendrier » |
| Apple iCloud | Adresse du compte Apple et mot de passe pour application (account.apple.com) |
| Yahoo, Fastmail | Adresse du compte et mot de passe d'application |
| Nextcloud | Adresse du serveur suivie de `/remote.php/dav`, nom d'utilisateur et mot de passe d'application |
| Autre serveur CalDAV | Adresse CalDAV, identifiant et mot de passe |
| Autre adresse iCal (.ics) | Une adresse `https://` renvoyant un agenda au format iCal |

Pour un compte CalDAV (Infomaniak, iCloud, Yahoo, Fastmail, Nextcloud), le
programme demande ensuite quels agendas du compte surveiller : le vôtre est
proposé par défaut, les agendas partagés des collègues et des salles ne le
sont pas. Pour Google et Outlook, il faut une adresse par agenda.

Une adresse secrète donne accès en lecture à tout l'agenda : elle est conservée
comme un mot de passe, ne la partagez pas.

Un rendez-vous présent dans deux agendas (invitation reçue sur les deux) ne
sonne qu'une fois.

## Comportement

- Sonnerie en boucle jusqu'à un clic. Fermer la fenêtre par la croix vaut
  « Rappel », pas « Arrêter ».
- Un rendez-vous découvert à moins de 10 minutes, ou commencé depuis moins de
  5 minutes (sortie de veille), sonne immédiatement.
- Sont ignorés : les événements « toute la journée », les rendez-vous annulés
  et les invitations refusées.
- Icône verte : tous les comptes se synchronisent. Icône rouge et
  notification : un compte n'a plus de synchronisation réussie depuis
  5 minutes, ses alarmes ne sont plus fiables.
- Mots de passe et adresses iCal sont conservés dans le Gestionnaire
  d'identifications Windows. Si un serveur en refuse un, le programme le
  redemande.

Désinstallation : « Applications installées » de Windows. Elle retire aussi
les réglages et les mots de passe enregistrés.

Limites : Windows uniquement ; le PC doit être allumé, session ouverte. Pour
les agendas lus par une adresse iCal (Google, Outlook), le délai d'apparition
d'un nouveau rendez-vous dépend du fournisseur.

## Réglages

`%LOCALAPPDATA%\calendar-alarm\config.json` (relancez le programme après
modification) :

| Clé | Défaut | Rôle |
| --- | --- | --- |
| `lead_minutes` | 10 | Délai avant le rendez-vous |
| `snooze_minutes` | 5 | Durée du rappel |
| `late_grace_minutes` | 5 | Retard toléré pour sonner quand même |
| `poll_seconds` | 60 | Fréquence de relecture des agendas |
| `sound` | `C:\Windows\Media\Alarm01.wav` | Fichier `.wav` de la sonnerie |
| `my_emails` | `[]` | Adresses servant à reconnaître vos invitations refusées |
| `language` | langue de Windows | `"fr"` ou `"en"` |

Le journal est dans le même dossier (`calendar-alarm.log`).

## Depuis les sources

Python 3.12 ou plus récent, installé côté Windows.

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1    # venv, dépendances, lancement
.venv\Scripts\python.exe -m unittest                    # tests
powershell -ExecutionPolicy Bypass -File build.ps1      # produit l'installeur dans dist\ (Inno Setup 6 requis)
powershell -ExecutionPolicy Bypass -File uninstall.ps1  # arrêt et retrait du démarrage
```

Tous les textes affichés sont dans `texts.py`, en français et en anglais.
