"""Everything the user reads, in French and in English, and the calendar providers on offer."""

import ctypes

LANGUAGES = {"fr": "Français", "en": "English"}

# kind "caldav": server + login + password; kind "ics": one secret address per calendar.
# The name and the instructions of each provider are the strings provider_<key> and help_<key>.
PROVIDERS = [
    {"key": "infomaniak", "kind": "caldav", "url": "https://sync.infomaniak.com"},
    {"key": "google", "kind": "ics"},
    {"key": "outlook", "kind": "ics"},
    {"key": "icloud", "kind": "caldav", "url": "https://caldav.icloud.com"},
    {"key": "yahoo", "kind": "caldav", "url": "https://caldav.calendar.yahoo.com"},
    {"key": "fastmail", "kind": "caldav", "url": "https://caldav.fastmail.com/dav/"},
    {"key": "nextcloud", "kind": "caldav", "url": "https://"},
    {"key": "caldav", "kind": "caldav", "url": "https://"},
    {"key": "ics", "kind": "ics"},
]

STRINGS = {
    "fr": {
        # Providers
        "provider_infomaniak": "Infomaniak (kSuite, Kalendar)",
        "provider_google": "Google Agenda",
        "provider_outlook": "Outlook / Microsoft 365",
        "provider_icloud": "Apple iCloud",
        "provider_yahoo": "Yahoo",
        "provider_fastmail": "Fastmail",
        "provider_nextcloud": "Nextcloud",
        "provider_caldav": "Autre serveur CalDAV",
        "provider_ics": "Autre adresse iCal (.ics)",
        "help_infomaniak": (
            "Saisissez votre identifiant de synchronisation (du type AB12345) et un mot de passe "
            "d'application. Les deux se trouvent sur config.infomaniak.com : « Mes Contacts & Agendas » → "
            "« Synchronisation manuelle »."
        ),
        "help_google": (
            "Collez l'adresse secrète de l'agenda. Dans Google Agenda : Paramètres → votre agenda → "
            "« Intégrer l'agenda » → « Adresse secrète au format iCal ». Cette adresse donne accès en "
            "lecture à l'agenda : ne la partagez pas."
        ),
        "help_outlook": (
            "Collez le lien ICS de l'agenda publié. Dans Outlook sur le web : Paramètres → Calendrier → "
            "Calendriers partagés → « Publier un calendrier », choisissez l'agenda et « Peut afficher tous "
            "les détails », puis copiez le lien ICS. Toute personne possédant ce lien peut lire l'agenda : "
            "ne le partagez pas."
        ),
        "help_icloud": (
            "Identifiant : l'adresse e-mail de votre compte Apple. Mot de passe : un mot de passe pour "
            "application, à créer sur account.apple.com (« Connexion et sécurité » → « Mots de passe pour "
            "applications »). La validation en deux étapes doit être activée."
        ),
        "help_yahoo": (
            "Identifiant : votre adresse Yahoo. Mot de passe : un mot de passe d'application, à créer dans "
            "les réglages de sécurité de votre compte Yahoo (« Générer un mot de passe d'application »)."
        ),
        "help_fastmail": (
            "Identifiant : votre adresse Fastmail. Mot de passe : un mot de passe d'application donnant "
            "accès à « Calendars (CalDAV) », à créer dans Settings → Privacy & Security → Integrations."
        ),
        "help_nextcloud": (
            "Serveur : l'adresse de votre Nextcloud suivie de /remote.php/dav, par exemple "
            "https://cloud.exemple.fr/remote.php/dav. Identifiant : votre nom d'utilisateur. Mot de passe : "
            "un mot de passe d'application, à créer dans Paramètres → Sécurité."
        ),
        "help_caldav": (
            "Saisissez l'adresse CalDAV du serveur, votre identifiant et votre mot de passe (de préférence "
            "un mot de passe d'application)."
        ),
        "help_ics": (
            "Collez l'adresse iCal (.ics) de l'agenda, commençant par https://. Si elle est privée, elle "
            "donne accès en lecture à l'agenda : ne la partagez pas."
        ),
        # Settings windows
        "title_accounts": "comptes et agendas",
        "title_caldav": "compte CalDAV",
        "title_ics": "adresse iCal",
        "title_calendars": "agendas surveillés",
        "accounts_intro": "Ajoutez ici tous vos agendas : chaque rendez-vous sonnera {minutes} minutes avant son début.",
        "no_account": "Aucun agenda pour l'instant.",
        "add_calendar": "Ajouter un agenda :",
        "add": "Ajouter…",
        "close": "Fermer",
        "remove": "Supprimer",
        "edit": "Modifier…",
        "calendars_button": "Agendas…",
        "remove_question": "Supprimer « {name} » ? Ses rendez-vous ne sonneront plus.",
        "server": "Serveur",
        "username": "Identifiant",
        "app_password": "Mot de passe d'application",
        "calendar_name": "Nom de l'agenda",
        "ics_address": "Adresse iCal",
        "test_and_save": "Tester et enregistrer",
        "save": "Enregistrer",
        "cancel": "Annuler",
        "testing": "Test de la connexion…",
        "refused_input": "Refusé par le serveur : vérifiez votre saisie.",
        "connection_failed": "Connexion impossible : {error}",
        "three_required": "Les trois champs sont obligatoires.",
        "no_calendar_found": "Connexion réussie, mais aucun agenda trouvé.",
        "ics_invalid": "Il faut un nom et une adresse commençant par https://.",
        "not_ical": "Cette adresse ne renvoie pas un agenda au format iCal.",
        "choose_calendars": "{name}\nCochez les agendas dont les rendez-vous doivent sonner.",
        "at_least_one": "Cochez au moins un agenda.",
        "refused_caldav": (
            "{name} : le serveur refuse le mot de passe enregistré (révoqué ou expiré). Les alarmes de ce "
            "compte sont suspendues tant qu'il n'est pas remplacé."
        ),
        "refused_ics": (
            "{name} : le serveur refuse l'adresse iCal enregistrée (réinitialisée ou supprimée). Les alarmes "
            "de cet agenda sont suspendues tant qu'elle n'est pas remplacée."
        ),
        # Account states
        "synced_at": "Synchronisé à {time}",
        "failing": "En échec : {error}",
        "refused_short": "refusé par le serveur",
        "none_chosen": "Aucun agenda choisi",
        "waiting": "En attente de synchronisation",
        # Alarm
        "alarm_title": "Rendez-vous",
        "in_minutes": "Rendez-vous dans {minutes} min",
        "now": "Rendez-vous maintenant",
        "started_since": "Rendez-vous commencé depuis {minutes} min",
        "stop": "Arrêter",
        "snooze": "Rappel {minutes} min",
        "untitled": "(sans titre)",
        "test_event": "Test de l'alarme",
        "test_calendar": "Test",
        # Tray
        "not_configured": "Aucun agenda configuré",
        "account_state": "{name} : {state}",
        "last_sync": "Dernière synchro : {time}",
        "no_upcoming": "Aucun rendez-vous dans les 24 h",
        "next_event": "Prochain : {time} {title}",
        "sync_now": "Synchroniser maintenant",
        "test_alarm": "Tester l'alarme",
        "menu_accounts": "Comptes et agendas…",
        "autostart": "Lancer au démarrage de Windows",
        "language": "Langue",
        "open_log": "Ouvrir le journal",
        "quit": "Quitter",
        "stale_notification": "Agenda non synchronisé ({names}) : les alarmes ne sont plus fiables.",
    },
    "en": {
        # Providers
        "provider_infomaniak": "Infomaniak (kSuite, Kalendar)",
        "provider_google": "Google Calendar",
        "provider_outlook": "Outlook / Microsoft 365",
        "provider_icloud": "Apple iCloud",
        "provider_yahoo": "Yahoo",
        "provider_fastmail": "Fastmail",
        "provider_nextcloud": "Nextcloud",
        "provider_caldav": "Other CalDAV server",
        "provider_ics": "Other iCal address (.ics)",
        "help_infomaniak": (
            "Enter your synchronisation username (like AB12345) and an application password. Both are "
            "shown on config.infomaniak.com: “My Contacts & Calendars” → “Manual synchronisation”."
        ),
        "help_google": (
            "Paste the secret address of the calendar. In Google Calendar: Settings → your calendar → "
            "“Integrate calendar” → “Secret address in iCal format”. This address gives read access to "
            "the calendar: do not share it."
        ),
        "help_outlook": (
            "Paste the ICS link of the published calendar. In Outlook on the web: Settings → Calendar → "
            "Shared calendars → “Publish a calendar”, choose the calendar and “Can view all details”, "
            "then copy the ICS link. Anyone holding this link can read the calendar: do not share it."
        ),
        "help_icloud": (
            "Username: the email address of your Apple Account. Password: an app-specific password, "
            "created on account.apple.com (“Sign-In and Security” → “App-Specific Passwords”). "
            "Two-factor authentication must be turned on."
        ),
        "help_yahoo": (
            "Username: your Yahoo address. Password: an app password, created in the security settings "
            "of your Yahoo account (“Generate app password”)."
        ),
        "help_fastmail": (
            "Username: your Fastmail address. Password: an app password with access to “Calendars "
            "(CalDAV)”, created in Settings → Privacy & Security → Integrations."
        ),
        "help_nextcloud": (
            "Server: the address of your Nextcloud followed by /remote.php/dav, for example "
            "https://cloud.example.com/remote.php/dav. Username: your user name. Password: an app "
            "password, created in Settings → Security."
        ),
        "help_caldav": (
            "Enter the CalDAV address of the server, your username and your password (preferably an "
            "application password)."
        ),
        "help_ics": (
            "Paste the iCal (.ics) address of the calendar, starting with https://. If it is private, it "
            "gives read access to the calendar: do not share it."
        ),
        # Settings windows
        "title_accounts": "accounts and calendars",
        "title_caldav": "CalDAV account",
        "title_ics": "iCal address",
        "title_calendars": "watched calendars",
        "accounts_intro": "Add all your calendars here: every event will ring {minutes} minutes before it starts.",
        "no_account": "No calendar yet.",
        "add_calendar": "Add a calendar:",
        "add": "Add…",
        "close": "Close",
        "remove": "Remove",
        "edit": "Edit…",
        "calendars_button": "Calendars…",
        "remove_question": "Remove “{name}”? Its events will no longer ring.",
        "server": "Server",
        "username": "Username",
        "app_password": "Application password",
        "calendar_name": "Calendar name",
        "ics_address": "iCal address",
        "test_and_save": "Test and save",
        "save": "Save",
        "cancel": "Cancel",
        "testing": "Testing the connection…",
        "refused_input": "Refused by the server: check what you entered.",
        "connection_failed": "Connection failed: {error}",
        "three_required": "All three fields are required.",
        "no_calendar_found": "Connection succeeded, but no calendar was found.",
        "ics_invalid": "A name and an address starting with https:// are required.",
        "not_ical": "This address does not return a calendar in iCal format.",
        "choose_calendars": "{name}\nTick the calendars whose events must ring.",
        "at_least_one": "Tick at least one calendar.",
        "refused_caldav": (
            "{name}: the server refuses the saved password (revoked or expired). The alarms of this "
            "account are suspended until it is replaced."
        ),
        "refused_ics": (
            "{name}: the server refuses the saved iCal address (reset or deleted). The alarms of this "
            "calendar are suspended until it is replaced."
        ),
        # Account states
        "synced_at": "Synced at {time}",
        "failing": "Failing: {error}",
        "refused_short": "refused by the server",
        "none_chosen": "No calendar chosen",
        "waiting": "Waiting for the first sync",
        # Alarm
        "alarm_title": "Appointment",
        "in_minutes": "Appointment in {minutes} min",
        "now": "Appointment now",
        "started_since": "Appointment started {minutes} min ago",
        "stop": "Stop",
        "snooze": "Snooze {minutes} min",
        "untitled": "(no title)",
        "test_event": "Alarm test",
        "test_calendar": "Test",
        # Tray
        "not_configured": "No calendar configured",
        "account_state": "{name}: {state}",
        "last_sync": "Last sync: {time}",
        "no_upcoming": "No appointment in the next 24 h",
        "next_event": "Next: {time} {title}",
        "sync_now": "Sync now",
        "test_alarm": "Test the alarm",
        "menu_accounts": "Accounts and calendars…",
        "autostart": "Start with Windows",
        "language": "Language",
        "open_log": "Open the log",
        "quit": "Quit",
        "stale_notification": "Calendar not synced ({names}): the alarms can no longer be trusted.",
    },
}

_language = "en"


def detect_language():
    """French on a French Windows, English everywhere else."""
    try:
        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
    except (AttributeError, OSError):
        return "en"
    return "fr" if primary == 0x0C else "en"


def set_language(code):
    global _language
    _language = code if code in STRINGS else detect_language()


def language():
    return _language


def t(key, **values):
    return STRINGS[_language][key].format(**values)
