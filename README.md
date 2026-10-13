# Calendar Alarm — une alarme avant chaque rendez-vous Infomaniak

Petit programme Windows qui tourne en arrière-plan et sonne **10 minutes avant
chaque rendez-vous** d'un agenda Infomaniak (Kalendar), que le rendez-vous porte
un rappel ou non et quel que soit son créateur. Une fenêtre passe au premier
plan avec le titre du rendez-vous et deux boutons : **Arrêter** et
**Rappel 5 min**.

L'agenda est relu toutes les minutes : un rendez-vous ajouté à la dernière
minute sonne dès qu'il est découvert.

## Installation

1. Lancer `CalendarAlarm-Setup-<version>.exe` (aucun droit administrateur
   requis). Le fichier n'étant pas signé, Windows affiche un avertissement
   SmartScreen : « Informations complémentaires » → « Exécuter quand même ».
   Pour vérifier que le fichier est intact, comparer son empreinte à celle du
   fichier `.sha256` fourni avec lui :
   `Get-FileHash CalendarAlarm-Setup-<version>.exe -Algorithm SHA256`
2. Sur <https://config.infomaniak.com> : « Mes Contacts & Agendas » → onglet
   « Synchronisation manuelle ». Relever le **nom d'utilisateur** court (type
   `AB12345`) et cliquer sur **Générer** pour obtenir un mot de passe
   d'application.
3. Les saisir dans la fenêtre du programme, puis cocher les agendas à
   surveiller (le sien est proposé par défaut ; les agendas partagés des
   collègues et des salles ne le sont pas).

Le programme s'inscrit lui-même au démarrage de Windows. Tout le reste passe
par un **clic droit** sur l'icône de la zone de notification : tester l'alarme,
changer d'agendas ou d'identifiants, désactiver le lancement au démarrage,
quitter.

## Comportement

- Sonnerie en boucle jusqu'à un clic. Fermer la fenêtre par la croix vaut
  « Rappel », pas « Arrêter ».
- Un rendez-vous découvert à moins de 10 minutes, ou commencé depuis moins de
  5 minutes (sortie de veille), sonne immédiatement.
- Sont ignorés : les événements « toute la journée », les rendez-vous annulés
  et les invitations refusées.
- Icône verte : synchronisation en cours. Icône rouge et notification : plus de
  synchronisation réussie depuis 5 minutes, les alarmes ne sont plus fiables.
- Le mot de passe est conservé dans le Gestionnaire d'identifications Windows.
  S'il est refusé par Infomaniak, le programme le redemande.

Désinstallation : « Applications installées » de Windows. Elle retire aussi
les réglages et le mot de passe enregistré.

Limites : Windows uniquement ; le PC doit être allumé, session ouverte.

## Réglages

`%LOCALAPPDATA%\calendar-alarm\config.json` (relancer le programme après
modification) :

| Clé | Défaut | Rôle |
| --- | --- | --- |
| `lead_minutes` | 10 | Délai avant le rendez-vous |
| `snooze_minutes` | 5 | Durée du rappel |
| `late_grace_minutes` | 5 | Retard toléré pour sonner quand même |
| `poll_seconds` | 60 | Fréquence de relecture de l'agenda |
| `sound` | `C:\Windows\Media\Alarm01.wav` | Fichier `.wav` de la sonnerie |
| `my_emails` | `[]` | Adresses servant à reconnaître ses invitations refusées |

Le journal est dans le même dossier (`calendar-alarm.log`).

## Depuis les sources

Python 3.12 ou plus récent, installé côté Windows.

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1    # venv, dépendances, lancement
.venv\Scripts\python.exe -m unittest                    # tests
powershell -ExecutionPolicy Bypass -File build.ps1      # produit l'installeur dans dist\ (Inno Setup 6 requis)
powershell -ExecutionPolicy Bypass -File uninstall.ps1  # arrêt et retrait du démarrage
```
