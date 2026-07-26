"""i18n string table + resolver (Stage 9).

A deliberately small, framework-free translation layer. The single source of
truth is :data:`STRINGS` — a flat ``{key: {lang: text}}`` table covering the
landing page, the onboarding tour, and the key editor-chrome labels. The
server exposes it at ``/api/i18n`` (auth-exempt) so both the static landing
page and the editor can fetch a resolved dictionary for a language; the
browser persists the chosen language in ``localStorage`` and toggles EN|DE.

Rules:

  * **EN is the default and the fallback.** A key missing a translation for the
    requested language falls back to its English string, never to an empty
    value — so the UI never shows a blank label.
  * **Supported languages are explicit** (``SUPPORTED``); an unknown ``lang``
    resolves to English.
  * Keys are dotted, namespaced by surface (``landing.*``, ``tour.*``,
    ``app.*``) so they're easy to scan and extend.
"""
from __future__ import annotations

DEFAULT_LANG = "en"
SUPPORTED = ("en", "de")

# Flat key → per-language text. EN is mandatory for every key (it's the
# fallback); DE is provided for the landing + onboarding + key UI labels.
STRINGS: dict[str, dict[str, str]] = {
    # ── Landing page ──────────────────────────────────────────────────────
    "landing.nav.signin": {"en": "Sign in", "de": "Anmelden"},
    "landing.hero.eyebrow": {
        "en": "One-page CVs · AI cover letters · job tailoring",
        "de": "Einseitige Lebensläufe · KI-Anschreiben · Job-Anpassung",
    },
    "landing.hero.title": {
        "en": "A CV that always fits on one page.",
        "de": "Ein Lebenslauf, der immer auf eine Seite passt.",
    },
    "landing.hero.subtitle": {
        "en": (
            "CV Copilot turns your experience into a typeset, single-page PDF — "
            "and writes grounded cover letters and job-tailored versions with "
            "your own AI key. No install, no fonts to fight, no second page."
        ),
        "de": (
            "CV Copilot macht aus deiner Erfahrung einen gesetzten, einseitigen "
            "PDF-Lebenslauf — und schreibt fundierte Anschreiben und auf den Job "
            "zugeschnittene Versionen mit deinem eigenen KI-Schlüssel. Keine "
            "Installation, kein Schriftärger, keine zweite Seite."
        ),
    },
    "landing.hero.cta": {"en": "Get started — it's free", "de": "Jetzt starten — kostenlos"},
    "landing.hero.cta_sub": {
        "en": "Sign in with a magic link. 3 free AI generations, then bring your own key.",
        "de": "Mit Magic-Link anmelden. 3 KI-Generierungen gratis, danach dein eigener Schlüssel.",
    },
    "landing.feature.onepage.title": {
        "en": "Guaranteed one page",
        "de": "Garantiert eine Seite",
    },
    "landing.feature.onepage.body": {
        "en": (
            "The renderer refuses to ship a two-page PDF — it tells you exactly "
            "which section overflowed so you can trim it. Print-ready A4, every time."
        ),
        "de": (
            "Der Renderer liefert niemals ein zweiseitiges PDF — er sagt dir genau, "
            "welcher Abschnitt übergelaufen ist. Druckfertiges A4, jedes Mal."
        ),
    },
    "landing.feature.cover.title": {
        "en": "Grounded cover letters",
        "de": "Fundierte Anschreiben",
    },
    "landing.feature.cover.body": {
        "en": (
            "Paste a job description and generate a cover letter grounded in your "
            "real CV — every claim is cross-checked, so the AI can't invent facts."
        ),
        "de": (
            "Füge eine Stellenbeschreibung ein und erzeuge ein Anschreiben, das auf "
            "deinem echten Lebenslauf basiert — jede Aussage wird geprüft, damit die "
            "KI nichts erfindet."
        ),
    },
    "landing.feature.tailor.title": {
        "en": "Tailor to any job",
        "de": "An jeden Job anpassen",
    },
    "landing.feature.tailor.body": {
        "en": (
            "One click reorders and rewrites your genuine bullets for a specific "
            "role and shows an ATS keyword-match score — your original CV is kept untouched."
        ),
        "de": (
            "Ein Klick ordnet deine echten Stichpunkte für eine Rolle neu und zeigt "
            "einen ATS-Keyword-Score — dein Original-Lebenslauf bleibt unverändert."
        ),
    },
    "landing.feature.byok.title": {
        "en": "Bring your own key",
        "de": "Eigener Schlüssel",
    },
    "landing.feature.byok.body": {
        "en": (
            "After 3 free generations, add your own Anthropic key. It's encrypted "
            "at rest, never logged, and the AI runs on your account — full control."
        ),
        "de": (
            "Nach 3 gratis Generierungen fügst du deinen eigenen Anthropic-Schlüssel "
            "hinzu. Verschlüsselt gespeichert, nie geloggt, KI läuft auf deinem Konto."
        ),
    },
    "landing.how.title": {"en": "How it works", "de": "So funktioniert's"},
    "landing.how.step1": {
        "en": "Sign in and start from a clean template — or import your existing resume.",
        "de": "Anmelden und mit einer sauberen Vorlage starten — oder bestehenden Lebenslauf importieren.",
    },
    "landing.how.step2": {
        "en": "Edit in a live form; the preview shows your real one-page PDF as you type.",
        "de": "Im Live-Formular bearbeiten; die Vorschau zeigt dein echtes einseitiges PDF beim Tippen.",
    },
    "landing.how.step3": {
        "en": "Tailor it to a job, generate a cover letter, and export a print-ready PDF.",
        "de": "An einen Job anpassen, ein Anschreiben erzeugen und ein druckfertiges PDF exportieren.",
    },
    "landing.footer.tagline": {
        "en": "CV Copilot — the one-page CV builder.",
        "de": "CV Copilot — der einseitige Lebenslauf-Builder.",
    },
    "landing.footer.privacy": {"en": "Privacy", "de": "Datenschutz"},
    # ── Onboarding tour ───────────────────────────────────────────────────
    "tour.skip": {"en": "Skip tour", "de": "Tour überspringen"},
    "tour.back": {"en": "Back", "de": "Zurück"},
    "tour.next": {"en": "Next", "de": "Weiter"},
    "tour.done": {"en": "Got it", "de": "Verstanden"},
    "tour.step_of": {"en": "Step {n} of {total}", "de": "Schritt {n} von {total}"},
    "tour.form.title": {"en": "Edit your CV in the form", "de": "Bearbeite deinen Lebenslauf im Formular"},
    "tour.form.body": {
        "en": "Every field has a labelled input. Add, remove, and reorder entries — your changes are saved to your account.",
        "de": "Jedes Feld hat eine beschriftete Eingabe. Einträge hinzufügen, entfernen und neu anordnen — Änderungen werden in deinem Konto gespeichert.",
    },
    "tour.import.title": {"en": "Import your existing resume", "de": "Importiere deinen bestehenden Lebenslauf"},
    "tour.import.body": {
        "en": "Have a LinkedIn PDF or an old resume? Drop the PDF (or paste text) into <strong>Import</strong> and Claude maps it into the form in seconds.",
        "de": "Hast du ein LinkedIn-PDF oder einen alten Lebenslauf? Lege das PDF in <strong>Import</strong> ab (oder füge Text ein) und Claude überträgt es in Sekunden ins Formular.",
    },
    "tour.aikey.title": {"en": "Connect your AI key", "de": "Verbinde deinen KI-Schlüssel"},
    "tour.aikey.body": {
        "en": "AI extract, cover letters, and tailoring use Claude. You get <strong>3 free generations</strong>; after that, add your own Anthropic key in <strong>Import → AI extract</strong>. It's encrypted and never logged.",
        "de": "KI-Extraktion, Anschreiben und Anpassung nutzen Claude. Du hast <strong>3 gratis Generierungen</strong>; danach füge deinen eigenen Anthropic-Schlüssel unter <strong>Import → KI-Extraktion</strong> hinzu. Verschlüsselt und nie geloggt.",
    },
    "tour.tailor.title": {"en": "Tailor to a job", "de": "An einen Job anpassen"},
    "tour.tailor.body": {
        "en": "Open <strong>Tailor to a job</strong> to paste a job description, see an ATS match score, and tailor a copy of your CV to it — your original stays untouched.",
        "de": "Öffne <strong>An einen Job anpassen</strong>, füge eine Stellenbeschreibung ein, sieh einen ATS-Score und passe eine Kopie deines Lebenslaufs an — dein Original bleibt unverändert.",
    },
    "tour.build.title": {"en": "Build the PDF", "de": "PDF erstellen"},
    "tour.build.body": {
        "en": "<strong>Build PDF</strong> renders a 1-page A4 and opens it. The build refuses to ship a 2-pager, so you always know it'll fit.",
        "de": "<strong>PDF erstellen</strong> rendert ein einseitiges A4 und öffnet es. Der Build liefert nie zwei Seiten — du weißt also immer, dass es passt.",
    },
    # ── Editor chrome (key UI labels) ─────────────────────────────────────
    "app.save": {"en": "Save", "de": "Speichern"},
    "app.build": {"en": "Build PDF", "de": "PDF erstellen"},
    "app.import": {"en": "Import", "de": "Importieren"},
    "app.tailor": {"en": "ATS score & review", "de": "ATS-Bewertung & Prüfung"},
    "app.cover_letters": {"en": "Cover letters", "de": "Anschreiben"},
    "app.jobs": {"en": "Saved jobs", "de": "Gespeicherte Jobs"},
    "app.themes": {"en": "Themes", "de": "Designs"},
    "app.new_cv": {"en": "New CV", "de": "Neuer Lebenslauf"},
    "app.download_yaml": {"en": "Download YAML", "de": "YAML herunterladen"},
    "app.sign_out": {"en": "Sign out", "de": "Abmelden"},
    "app.language": {"en": "Language", "de": "Sprache"},
    "app.save_changes": {"en": "Save changes", "de": "Änderungen speichern"},
    "app.build_tooltip": {
        "en": "Build the PDF · saves first, opens in a new tab",
        "de": "PDF erstellen · speichert zuerst und öffnet in einem neuen Tab",
    },
    "app.appearance_tooltip": {
        "en": "CV Appearance — presets · font · spacing · accent",
        "de": "Lebenslauf-Design — Voreinstellungen · Schrift · Abstände · Akzent",
    },
    "app.doc_actions_tooltip": {
        "en": "Document actions — import, create, rename, duplicate, download",
        "de": "Dokumentaktionen — importieren, erstellen, umbenennen, duplizieren, herunterladen",
    },
    "app.rerender_preview": {"en": "Re-render preview", "de": "Vorschau neu rendern"},
    "app.toggle_sidebar": {"en": "Toggle sidebar", "de": "Seitenleiste umschalten"},
    "sidebar.workspace": {"en": "Workspace", "de": "Arbeitsbereich"},
    "sidebar.library_aria": {"en": "Document library", "de": "Dokumentbibliothek"},
    "sidebar.outline_aria": {"en": "Section outline", "de": "Abschnittsübersicht"},
    "sidebar.library": {"en": "Library", "de": "Bibliothek"},
    "sidebar.sections": {"en": "Sections", "de": "Abschnitte"},
    "sidebar.job_outputs": {"en": "Job outputs", "de": "Job-Ausgaben"},
    "sidebar.empty": {"en": "No saved documents.", "de": "Keine gespeicherten Dokumente."},
    "sidebar.error": {"en": "Could not load library.", "de": "Bibliothek konnte nicht geladen werden."},
    "workflow.applications": {"en": "Applications", "de": "Bewerbungen"},
    "workflow.applications_tooltip": {
        "en": "Applications — jobs, cover letters, ATS scoring",
        "de": "Bewerbungen — Jobs, Anschreiben, ATS-Bewertung",
    },
    "workflow.hint": {
        "en": "Save a job, generate application documents, and review fit.",
        "de": "Speichere Jobs, erstelle Bewerbungsunterlagen und prüfe die Passung.",
    },
    "workflow.group.start": {"en": "Start with a job", "de": "Mit einem Job starten"},
    "workflow.group.documents": {"en": "Document libraries", "de": "Dokumentbibliotheken"},
    "workflow.group.review": {"en": "Review/ATS", "de": "Prüfung/ATS"},
    "workflow.jobs.title": {"en": "Start with a job", "de": "Mit einem Job starten"},
    "workflow.jobs.sub": {
        "en": "Track roles, then generate a CV and cover letter",
        "de": "Rollen verfolgen, dann Lebenslauf und Anschreiben erstellen",
    },
    "workflow.jobs.aria": {"en": "Start with a job", "de": "Mit einem Job starten"},
    "workflow.jobs.tooltip": {
        "en": "Saved jobs — save a job, then one-click tailor a CV + cover letter for it",
        "de": "Gespeicherte Jobs — speichere einen Job und erstelle mit einem Klick Lebenslauf und Anschreiben",
    },
    "workflow.cover.title": {"en": "Saved cover letters", "de": "Gespeicherte Anschreiben"},
    "workflow.cover.sub": {
        "en": "Open drafts or generate a new letter",
        "de": "Entwürfe öffnen oder ein neues Anschreiben erstellen",
    },
    "workflow.cover.tooltip": {
        "en": "Saved cover letters — open drafts or generate from a CV + job description",
        "de": "Gespeicherte Anschreiben — Entwürfe öffnen oder aus Lebenslauf und Stellenbeschreibung erstellen",
    },
    "workflow.cvs.title": {"en": "Saved CVs", "de": "Gespeicherte Lebensläufe"},
    "workflow.cvs.sub": {
        "en": "Open and manage CV documents",
        "de": "Lebenslauf-Dokumente öffnen und verwalten",
    },
    "workflow.cvs.aria": {"en": "CV documents", "de": "Lebenslauf-Dokumente"},
    "workflow.cvs.tooltip": {
        "en": "CV documents — open saved CVs from the editor library",
        "de": "Lebenslauf-Dokumente — gespeicherte Lebensläufe aus der Editor-Bibliothek öffnen",
    },
    "workflow.ats.title": {"en": "ATS score and review", "de": "ATS-Bewertung und Prüfung"},
    "workflow.ats.sub": {
        "en": "Score first, then create a reviewed copy",
        "de": "Erst bewerten, dann eine geprüfte Kopie erstellen",
    },
    "workflow.ats.aria": {"en": "Score and optimize CV", "de": "Lebenslauf bewerten und optimieren"},
    "workflow.ats.tooltip": {
        "en": "Score and review — ATS breakdown plus a reviewed copy workflow",
        "de": "Bewertung und Prüfung — ATS-Aufschlüsselung plus geprüfte Kopie",
    },
    # ── Form editor (JS-rendered controls in form.js) ─────────────────────
    "form.move_up": {"en": "Move up", "de": "Nach oben verschieben"},
    "form.move_down": {"en": "Move down", "de": "Nach unten verschieben"},
    "form.remove": {"en": "Remove", "de": "Entfernen"},
    "form.section_metrics": {"en": "Section metrics", "de": "Abschnittsmetriken"},
    "form.section_ats_aria": {
        "en": "ATS advisory for this section",
        "de": "ATS-Hinweise für diesen Abschnitt",
    },
    "form.photo_drop_aria": {
        "en": "Profile photo — drag an image here, or use the button below to choose a file",
        "de": "Profilfoto — ziehe ein Bild hierher oder wähle über die Schaltfläche unten eine Datei",
    },
    "form.custom_href_placeholder": {
        "en": "auto-resolved from username",
        "de": "wird automatisch aus dem Nutzernamen abgeleitet",
    },
    "mobile.current": {"en": "Current", "de": "Aktuell"},
    "mobile.utilities": {"en": "Utilities", "de": "Werkzeuge"},
    "mobile.open_utilities": {"en": "Open global utilities", "de": "Globale Werkzeuge öffnen"},
    "mobile.global_utilities": {"en": "Global utilities", "de": "Globale Werkzeuge"},
    "mobile.destinations": {"en": "Global destinations", "de": "Globale Ziele"},
    "mobile.document_actions": {"en": "Document actions", "de": "Dokumentaktionen"},
    "mobile.document_catalog": {"en": "Document catalog", "de": "Dokumentkatalog"},
    "mobile.settings": {"en": "Settings & account", "de": "Einstellungen & Konto"},
    "mobile.applications": {"en": "Applications", "de": "Bewerbungen"},
    "mobile.ats_detail": {"en": "ATS details", "de": "ATS-Details"},
    "mobile.refresh_preview": {"en": "Refresh preview", "de": "Vorschau aktualisieren"},
    "mobile.cv_appearance": {"en": "CV Appearance", "de": "Lebenslauf-Design"},
    "mobile.commands": {"en": "Commands", "de": "Befehle"},
    "mobile.app_theme": {"en": "App theme settings", "de": "App-Theme-Einstellungen"},
    "templates.kicker": {"en": "CV templates", "de": "Lebenslauf-Vorlagen"},
    "templates.title": {"en": "Templates", "de": "Vorlagen"},
    "templates.tooltip": {
        "en": "Templates - choose the active CV layout",
        "de": "Vorlagen - aktives Lebenslauf-Layout wählen",
    },
    "templates.close": {"en": "Close templates", "de": "Vorlagen schließen"},
    "templates.hint": {
        "en": "Choose a built-in layout. The active CV keeps its content, colour, font, and paper settings; some templates may adjust spacing for fit.",
        "de": "Wähle ein integriertes Layout. Der aktive Lebenslauf behält Inhalt, Farbe, Schrift und Papier; einige Vorlagen können den Abstand für die Passung anpassen.",
    },
    "templates.search.placeholder": {"en": "Search templates", "de": "Vorlagen suchen"},
    "templates.search.aria": {"en": "Search templates", "de": "Vorlagen suchen"},
    "templates.empty": {"en": "No templates match this search.", "de": "Keine Vorlagen passen zu dieser Suche."},
    "templates.cancel": {"en": "Cancel", "de": "Abbrechen"},
    "templates.apply": {"en": "Apply template", "de": "Vorlage anwenden"},
    "templates.current": {"en": "Current: {name}", "de": "Aktuell: {name}"},
    "templates.selected": {"en": "Selected: {name}", "de": "Ausgewählt: {name}"},
    "templates.current_badge": {"en": "Current template", "de": "Aktuelle Vorlage"},
    "templates.selected_badge": {"en": "Selected", "de": "Ausgewählt"},
    "templates.select_aria": {"en": "Select {name} template", "de": "Vorlage {name} auswählen"},
    "templates.columns": {"en": "{n} col", "de": "{n} Sp."},
    "templates.ats_chip": {"en": "ATS {value}", "de": "ATS {value}"},
    "templates.preview_error": {"en": "Preview unavailable", "de": "Vorschau nicht verfügbar"},
    "templates.layout.single_column": {"en": "Single column", "de": "Einspaltig"},
    "templates.layout.two_column": {"en": "Two column", "de": "Zweispaltig"},
    "templates.layout.rail": {"en": "Rail", "de": "Seitenspalte"},
    "templates.layout.compact": {"en": "Compact", "de": "Kompakt"},
    "templates.ats_priority.high": {"en": "High", "de": "Hoch"},
    "templates.ats_priority.medium": {"en": "Medium", "de": "Mittel"},
    "templates.ats_priority.low": {"en": "Low", "de": "Niedrig"},
    "templates.risk.low": {"en": "Low risk", "de": "Geringes Risiko"},
    "templates.risk.normal": {"en": "Normal risk", "de": "Normales Risiko"},
    "templates.risk.medium": {"en": "Medium risk", "de": "Mittleres Risiko"},
    "templates.label.classic": {"en": "Classic", "de": "Klassisch"},
    "templates.label.modern": {"en": "Modern", "de": "Modern"},
    "templates.label.sidebar": {"en": "Sidebar", "de": "Seitenspalte"},
    "templates.label.executive": {"en": "Executive", "de": "Executive"},
    "templates.label.technical": {"en": "Technical", "de": "Technisch"},
    "templates.label.ats_compact": {"en": "ATS Compact", "de": "ATS Kompakt"},
    "templates.label.consulting": {"en": "Consulting", "de": "Consulting"},
    "templates.label.minimal": {"en": "Minimal", "de": "Minimal"},
    "templates.use_case.classic": {
        "en": "Academic and conservative applications",
        "de": "Akademische und konservative Bewerbungen",
    },
    "templates.use_case.modern": {
        "en": "Modern product and operations roles",
        "de": "Moderne Produkt- und Operations-Rollen",
    },
    "templates.use_case.sidebar": {
        "en": "Technical CVs with dense skills evidence",
        "de": "Technische Lebensläufe mit dichtem Kompetenznachweis",
    },
    "templates.use_case.executive": {
        "en": "Leadership and advisory roles",
        "de": "Leadership- und Advisory-Rollen",
    },
    "templates.use_case.technical": {
        "en": "Engineering and data roles",
        "de": "Engineering- und Data-Rollen",
    },
    "templates.use_case.ats_compact": {
        "en": "ATS-heavy applications with dense evidence",
        "de": "ATS-lastige Bewerbungen mit dichtem Nachweis",
    },
    "templates.use_case.consulting": {
        "en": "Consulting, strategy, and program roles",
        "de": "Consulting-, Strategie- und Programmrollen",
    },
    "templates.use_case.minimal": {
        "en": "General applications needing low visual noise",
        "de": "Allgemeine Bewerbungen mit ruhigem Erscheinungsbild",
    },
    "app.collapse_all_short": {"en": "Collapse all", "de": "Alle einklappen"},
    "app.expand_all_short": {"en": "Expand all", "de": "Alle ausklappen"},
    # ── Shared sub-page chrome (jobs / cover-letters / account topbars) ────
    "nav.cv_editor": {"en": "CV editor", "de": "Lebenslauf-Editor"},
    "nav.letters": {"en": "Cover letters", "de": "Anschreiben"},
    "nav.tailor": {"en": "ATS", "de": "ATS"},
    "nav.jobs": {"en": "Jobs", "de": "Jobs"},
    "nav.applications": {"en": "Applications", "de": "Bewerbungen"},
    "nav.account": {"en": "Settings", "de": "Einstellungen"},
    "nav.privacy": {"en": "Privacy", "de": "Datenschutz"},
    "nav.theme": {"en": "Light/dark", "de": "Hell/Dunkel"},
    # ── Accessibility: skip links + visually-hidden page headings ─────────
    "a11y.skipToEditor": {"en": "Skip to editor", "de": "Zum Editor springen"},
    "a11y.skipToContent": {"en": "Skip to main content", "de": "Zum Hauptinhalt springen"},
    "a11y.editorHeading": {
        "en": "CV Copilot — one-page CV editor",
        "de": "CV Copilot — einseitiger Lebenslauf-Editor",
    },
    "a11y.accountHeading": {"en": "Account & settings", "de": "Konto & Einstellungen"},
    "a11y.tailorHeading": {"en": "Tailor & ATS score", "de": "Anpassen & ATS-Bewertung"},
    "import.paste_aria": {
        "en": "Paste résumé or CV text to import",
        "de": "Lebenslauf-Text zum Importieren einfügen",
    },
    "tailor.gauge.aria_empty": {
        "en": "ATS match score — not scored yet",
        "de": "ATS-Übereinstimmung — noch nicht bewertet",
    },
    "tailor.gauge.aria_scored": {
        "en": "ATS match score: {n} out of 100",
        "de": "ATS-Übereinstimmung: {n} von 100",
    },
    # ── Compact generation-credit state ──────────────────────────────────
    "credit.loading.short": {"en": "...", "de": "..."},
    "credit.loading.full": {
        "en": "Checking generation credit status",
        "de": "Generierungs-Credits werden geprüft",
    },
    "credit.connected.short": {"en": "Key", "de": "Key"},
    "credit.connected.full": {"en": "AI key connected", "de": "KI-Schlüssel verbunden"},
    "credit.remaining.short": {"en": "{n} left", "de": "{n} übrig"},
    "credit.remaining.full.one": {"en": "1 free generation left", "de": "1 Gratis-Generierung übrig"},
    "credit.remaining.full.many": {
        "en": "{n} free generations left",
        "de": "{n} Gratis-Generierungen übrig",
    },
    "credit.zero.short": {"en": "0 left", "de": "0 übrig"},
    "credit.zero.full": {
        "en": "No free generations left. Add an AI key to continue.",
        "de": "Keine Gratis-Generierungen übrig. Füge einen KI-Schlüssel hinzu.",
    },
    "credit.unavailable.short": {"en": "AI", "de": "KI"},
    "credit.unavailable.full": {
        "en": "Credit status unavailable",
        "de": "Credit-Status nicht verfügbar",
    },
    # ── Command palette (⌘K) — surfaced on every page ─────────────────────
    "palette.open": {"en": "Commands", "de": "Befehle"},
    "palette.open_tip": {
        "en": "Quick actions & navigation",
        "de": "Schnellaktionen & Navigation",
    },
    "palette.placeholder": {
        "en": "Search commands…",
        "de": "Befehle suchen…",
    },
    "palette.empty": {"en": "No matching commands", "de": "Keine passenden Befehle"},
    "palette.title": {"en": "Commands", "de": "Befehle"},
    "palette.group.recent": {"en": "Recent documents", "de": "Zuletzt verwendete Dokumente"},
    "palette.group.navigate": {"en": "Go to", "de": "Gehe zu"},
    "palette.group.actions": {"en": "Actions", "de": "Aktionen"},
    "palette.act.editor": {"en": "CV editor", "de": "Lebenslauf-Editor"},
    "palette.act.jobs": {"en": "Jobs catalog", "de": "Job-Katalog"},
    "palette.act.letters": {"en": "Cover letters", "de": "Anschreiben"},
    "palette.act.tailor": {"en": "ATS score & review", "de": "ATS-Bewertung & Prüfung"},
    "palette.act.account": {"en": "Settings & account", "de": "Einstellungen & Konto"},
    "palette.act.import": {"en": "Import a resume", "de": "Lebenslauf importieren"},
    "palette.act.new_cv": {"en": "New CV", "de": "Neuer Lebenslauf"},
    "palette.act.template": {"en": "Choose template", "de": "Vorlage wählen"},
    "palette.act.appearance": {"en": "CV Appearance", "de": "Lebenslauf-Design"},
    "palette.act.collapse_all": {"en": "Collapse all sections", "de": "Alle Abschnitte einklappen"},
    "palette.act.expand_all": {"en": "Expand all sections", "de": "Alle Abschnitte ausklappen"},
    "palette.act.theme": {"en": "App theme settings", "de": "App-Theme-Einstellungen"},
    "palette.hint.move": {"en": "move", "de": "wählen"},
    "palette.hint.run": {"en": "select", "de": "öffnen"},
    "palette.hint.close": {"en": "close", "de": "schließen"},
    # ── Account & data page ───────────────────────────────────────────────
    "account.title": {"en": "Settings & account", "de": "Einstellungen & Konto"},
    "account.profile.heading": {"en": "Profile", "de": "Profil"},
    "account.profile.intro": {
        "en": "Your display name and the language the editor starts in.",
        "de": "Dein Anzeigename und die Sprache, in der der Editor startet.",
    },
    "account.profile.email": {"en": "Email", "de": "E-Mail"},
    "account.profile.email_tip": {
        "en": "Your sign-in email — managed by your login provider",
        "de": "Deine Anmelde-E-Mail — verwaltet von deinem Login-Anbieter",
    },
    "account.profile.name": {"en": "Display name", "de": "Anzeigename"},
    "account.profile.language": {"en": "Default language", "de": "Standardsprache"},
    "account.profile.save": {"en": "Save profile", "de": "Profil speichern"},
    "account.ai.heading": {"en": "AI provider & key", "de": "KI-Anbieter & Schlüssel"},
    "account.ai.intro": {
        "en": (
            "Pick which AI you generate with, then add that provider's API key — "
            "you pay the provider directly. We store your key encrypted "
            "(AES-256-GCM) and never show it again, only the last 4 characters. "
            "Managed free credits apply to Claude only; Gemini needs your own key."
        ),
        "de": (
            "Wähle die KI, mit der du generierst, und füge den API-Schlüssel "
            "dieses Anbieters hinzu — du zahlst direkt beim Anbieter. Wir "
            "speichern deinen Schlüssel verschlüsselt (AES-256-GCM) und zeigen "
            "ihn nie wieder, nur die letzten 4 Zeichen. Verwaltete Gratis-"
            "Credits gelten nur für Claude; Gemini braucht deinen eigenen Schlüssel."
        ),
    },
    "account.ai.provider": {"en": "Provider", "de": "Anbieter"},
    "account.ai.model": {"en": "Model", "de": "Modell"},
    "account.ai.key": {"en": "API key", "de": "API-Schlüssel"},
    "account.ai.fineprint": {
        "en": (
            "The key is validated with a tiny test call, encrypted, and stored. "
            "It is never logged or returned."
        ),
        "de": (
            "Der Schlüssel wird mit einem winzigen Testaufruf geprüft, "
            "verschlüsselt und gespeichert. Er wird nie geloggt oder zurückgegeben."
        ),
    },
    "account.ai.connect": {"en": "Connect key", "de": "Schlüssel verbinden"},
    "account.ai.rotate": {"en": "Rotate key", "de": "Schlüssel ersetzen"},
    "account.ai.remove": {"en": "Remove key", "de": "Schlüssel entfernen"},
    "account.ai.checking": {"en": "Checking…", "de": "Prüfe…"},
    "account.ai.no_key": {"en": "No key connected", "de": "Kein Schlüssel verbunden"},
    "account.ai.key_connected": {"en": "Key connected", "de": "Schlüssel verbunden"},
    "account.ai.byo_required": {"en": "BYO key required", "de": "Eigener Schlüssel nötig"},
    "account.ai.credits_left": {"en": "managed credits left", "de": "verwaltete Credits übrig"},
    "account.ai.key_label_anthropic": {"en": "Anthropic API key", "de": "Anthropic-API-Schlüssel"},
    "account.ai.key_label_gemini": {"en": "Google (Gemini) API key", "de": "Google-(Gemini-)API-Schlüssel"},
    "account.job_sources.heading": {"en": "Job sources", "de": "Jobquellen"},
    "account.job_sources.intro": {
        "en": "Configure browser defaults for Jobs search and review server-reported source availability. Source capability is operator-controlled.",
        "de": "Konfiguriere Browser-Standards für die Jobsuche und prüfe die serverseitig gemeldete Quellenverfügbarkeit. Quellenfunktionen werden operatorseitig gesteuert.",
    },
    "account.job_sources.policy.no_blind_fetch": {"en": "No blind LinkedIn fetch.", "de": "Kein blindes LinkedIn-Abrufen."},
    "account.job_sources.policy.no_sessions": {"en": "No stored LinkedIn sessions.", "de": "Keine gespeicherten LinkedIn-Sitzungen."},
    "account.job_sources.policy.no_cookies": {"en": "No stored LinkedIn cookies.", "de": "Keine gespeicherten LinkedIn-Cookies."},
    "account.job_sources.policy.no_stealth": {"en": "No stealth automation.", "de": "Keine verdeckte Automatisierung."},
    "account.job_sources.policy.read_only": {
        "en": "Local databases are read-only preview sources.",
        "de": "Lokale Datenbanken sind schreibgeschützte Vorschauquellen.",
    },
    "account.job_sources.defaults_heading": {"en": "Jobs search defaults", "de": "Standards für die Jobsuche"},
    "account.job_sources.defaults_intro": {
        "en": "Stored only in this browser and applied when you open Jobs import preview.",
        "de": "Nur in diesem Browser gespeichert und beim Öffnen der Job-Importvorschau angewendet.",
    },
    "account.job_sources.source": {"en": "Source", "de": "Quelle"},
    "account.job_sources.source_aria": {"en": "Default job source", "de": "Standard-Jobquelle"},
    "account.job_sources.source.none": {"en": "No source default", "de": "Keine Standardquelle"},
    "account.job_sources.source.linkedin": {"en": "LinkedIn local DB", "de": "Lokale LinkedIn-Datenbank"},
    "account.job_sources.source.manual": {"en": "Manual paste/export", "de": "Manuelles Einfügen/Export"},
    "account.job_sources.source.all": {"en": "All local sources", "de": "Alle lokalen Quellen"},
    "account.job_sources.title": {"en": "Title", "de": "Titel"},
    "account.job_sources.family": {"en": "Job family", "de": "Jobfamilie"},
    "account.job_sources.titles": {"en": "Preferred titles", "de": "Bevorzugte Titel"},
    "account.job_sources.search_mode": {"en": "Title match", "de": "Titelabgleich"},
    "account.job_sources.search_mode_aria": {"en": "Default title match mode", "de": "Standard-Titelabgleich"},
    "account.job_sources.country": {"en": "Country", "de": "Land"},
    "account.job_sources.region": {"en": "Region", "de": "Region"},
    "account.job_sources.cities": {"en": "Cities", "de": "Städte"},
    "account.job_sources.remote.label": {"en": "Remote mode", "de": "Remote-Modus"},
    "account.job_sources.remote.aria": {"en": "Default remote mode", "de": "Standard-Remote-Modus"},
    "account.job_sources.remote.any": {"en": "Any", "de": "Alle"},
    "account.job_sources.remote.onsite": {"en": "Onsite", "de": "Vor Ort"},
    "account.job_sources.remote.hybrid": {"en": "Hybrid", "de": "Hybrid"},
    "account.job_sources.remote.remote": {"en": "Remote", "de": "Remote"},
    "account.job_sources.seniority": {"en": "Seniority", "de": "Seniorität"},
    "account.job_sources.min_years": {"en": "Minimum years", "de": "Mindestjahre"},
    "account.job_sources.freshness.label": {"en": "Freshness", "de": "Aktualität"},
    "account.job_sources.freshness.aria": {"en": "Default freshness", "de": "Standard-Aktualität"},
    "account.job_sources.freshness.any": {"en": "Any", "de": "Alle"},
    "account.job_sources.freshness.fresh_only": {"en": "Fresh only", "de": "Nur aktuell"},
    "account.job_sources.freshness.stale_only": {"en": "Stale only", "de": "Nur veraltete"},
    "account.job_sources.freshness.include_stale": {"en": "Stale only", "de": "Nur veraltete"},
    "account.job_sources.ranking": {"en": "Ranking weights", "de": "Ranking-Gewichte"},
    "account.job_sources.save": {"en": "Save job defaults", "de": "Job-Standards speichern"},
    "account.job_sources.reset": {"en": "Reset job defaults", "de": "Job-Standards zurücksetzen"},
    "account.job_sources.export": {"en": "Export profile", "de": "Profil exportieren"},
    "account.job_sources.import": {"en": "Import profile", "de": "Profil importieren"},
    "account.job_sources.import_prompt": {"en": "Paste the exported job profile JSON.", "de": "Exportiertes Jobprofil-JSON einfügen."},
    "account.job_sources.open_jobs": {"en": "Open Jobs", "de": "Jobs öffnen"},
    "account.job_sources.feedback.saved": {
        "en": "Job source defaults saved for this browser.",
        "de": "Jobquellen-Standards für diesen Browser gespeichert.",
    },
    "account.job_sources.feedback.reset": {
        "en": "Job source defaults reset.",
        "de": "Jobquellen-Standards zurückgesetzt.",
    },
    "account.job_sources.feedback.exported": {"en": "Job profile exported.", "de": "Jobprofil exportiert."},
    "account.job_sources.feedback.imported": {"en": "Job profile imported.", "de": "Jobprofil importiert."},
    "account.job_sources.feedback.import_invalid": {
        "en": "Import file is not valid JSON.",
        "de": "Importdatei ist kein gültiges JSON.",
    },
    "account.job_sources.feedback.import_schema": {
        "en": "Import file is not a CV Copilot job profile.",
        "de": "Importdatei ist kein CV-Copilot-Jobprofil.",
    },
    "account.job_sources.feedback.import_read_failed": {
        "en": "Could not read the import file.",
        "de": "Importdatei konnte nicht gelesen werden.",
    },
    "account.job_sources.status.checking": {"en": "Checking job source status...", "de": "Jobquellen-Status wird geprüft..."},
    "account.job_sources.status.local_available": {"en": "Local jobs DB available", "de": "Lokale Jobdatenbank verfügbar"},
    "account.job_sources.status.local_unavailable": {"en": "Local jobs DB unavailable", "de": "Lokale Jobdatenbank nicht verfügbar"},
    "account.job_sources.status.rows": {"en": "{n} rows", "de": "{n} Zeilen"},
    "account.job_sources.status.latest_seen": {"en": "latest seen {date}", "de": "zuletzt gesehen {date}"},
    "account.job_sources.status.days_old": {"en": "{n} days old", "de": "{n} Tage alt"},
    "account.job_sources.status.adapter_configured": {"en": "approved adapter configured", "de": "freigegebener Adapter konfiguriert"},
    "account.job_sources.status.adapter_disabled": {"en": "approved adapter disabled", "de": "freigegebener Adapter deaktiviert"},
    "account.job_sources.status.no_blind_fetch": {"en": "no blind LinkedIn fetch", "de": "kein blindes LinkedIn-Abrufen"},
    "account.job_sources.status.stale": {"en": "stale", "de": "veraltet"},
    "account.job_sources.status.unavailable": {"en": "Job source status unavailable.", "de": "Jobquellen-Status nicht verfügbar."},
    # Provider-card sub-lines (Stage 8 redesign).
    "account.ai.provider_anthropic": {
        "en": "Anthropic · managed credits or BYO key",
        "de": "Anthropic · verwaltete Credits oder eigener Schlüssel",
    },
    "account.ai.provider_gemini": {
        "en": "Google · your own API key",
        "de": "Google · dein eigener API-Schlüssel",
    },
    # Key field: show/hide toggle + validate/save state copy (Stage 8 redesign).
    "account.ai.reveal": {"en": "Show", "de": "Anzeigen"},
    "account.ai.hide": {"en": "Hide", "de": "Verbergen"},
    "account.ai.validating": {"en": "Validating…", "de": "Prüfe…"},
    "account.ai.validating_long": {
        "en": "Validating and encrypting your key…",
        "de": "Schlüssel wird geprüft und verschlüsselt…",
    },
    "account.ai.paste_first": {"en": "Paste your", "de": "Füge zuerst diesen ein —"},
    "account.ai.saved_ok": {
        "en": "Key connected. Stored encrypted, shown only as the last 4 characters.",
        "de": "Schlüssel verbunden. Verschlüsselt gespeichert, nur die letzten 4 Zeichen sichtbar.",
    },
    "account.ai.save_fail": {"en": "Could not save the key.", "de": "Schlüssel konnte nicht gespeichert werden."},
    "account.ai.status_error": {
        "en": "Couldn't load key status.",
        "de": "Schlüsselstatus konnte nicht geladen werden.",
    },
    "account.ai.remove_confirm": {"en": "Remove your stored", "de": "Diesen gespeicherten Schlüssel entfernen —"},
    "account.ai.removing": {"en": "Removing…", "de": "Entferne…"},
    "account.ai.removed_ok": {"en": "Key removed.", "de": "Schlüssel entfernt."},
    "account.ai.remove_fail": {"en": "Could not remove the key.", "de": "Schlüssel konnte nicht entfernt werden."},
    "account.data.heading": {"en": "Your data", "de": "Deine Daten"},
    "account.data.export": {"en": "Download my data (JSON)", "de": "Meine Daten herunterladen (JSON)"},
    "account.delete.heading": {"en": "Delete account", "de": "Konto löschen"},
    "account.delete.confirm_hint": {
        "en": "Type <code>DELETE</code> to confirm",
        "de": "Tippe <code>DELETE</code> zur Bestätigung",
    },
    "account.delete.button": {"en": "Delete my account", "de": "Mein Konto löschen"},
    "account.delete.cancel": {"en": "Cancel deletion", "de": "Löschung abbrechen"},
    # ── Jobs catalog ──────────────────────────────────────────────────────
    "jobs.title": {"en": "Jobs catalog", "de": "Job-Katalog"},
    "jobs.page.sub": {
        "en": "Track saved roles, linked tailored CVs, and grounded cover letters in one workspace.",
        "de": "Verwalte gespeicherte Rollen, verknüpfte CVs und fundierte Anschreiben an einem Ort.",
    },
    "jobs.add.summary": {"en": "Save a job", "de": "Job speichern"},
    "jobs.add.button": {"en": "Save job", "de": "Job speichern"},
    "jobs.field.url": {"en": "Job URL (optional)", "de": "Job-URL (optional)"},
    "jobs.field.jd": {"en": "Job description", "de": "Stellenbeschreibung"},
    "jobs.field.title": {"en": "Title (optional)", "de": "Titel (optional)"},
    "jobs.field.company": {"en": "Company (optional)", "de": "Unternehmen (optional)"},
    "jobs.field.field": {"en": "Field (optional)", "de": "Bereich (optional)"},
    "jobs.field.deadline": {"en": "Deadline (optional)", "de": "Frist (optional)"},
    "jobs.field.confidence": {"en": "Fit confidence (optional)", "de": "Passungs-Konfidenz (optional)"},
    "jobs.field.whyfit": {"en": "Your note (optional)", "de": "Deine Notiz (optional)"},
    "jobs.field.deadline_hint": {
        "en": "advisory — verify on the posting",
        "de": "Richtwert — auf der Stellenanzeige prüfen",
    },
    "jobs.filter.all": {"en": "All", "de": "Alle"},
    "jobs.filter.has_cover": {"en": "Has cover", "de": "Mit Anschreiben"},
    "jobs.filter.search_mode": {"en": "Search mode", "de": "Suchmodus"},
    "jobs.filter.mode.flexible": {"en": "Flexible", "de": "Flexibel"},
    "jobs.filter.mode.strict": {"en": "Strict title", "de": "Strenger Titel"},
    "jobs.filter.family": {"en": "Job family", "de": "Jobfamilie"},
    "jobs.filter.city": {"en": "City", "de": "Stadt"},
    "jobs.filter.min_years": {"en": "Years fit", "de": "Jahre-Fit"},
    "jobs.filter.rank_bucket": {"en": "Rank bucket", "de": "Ranggruppe"},
    "jobs.filter.any_rank": {"en": "Any rank", "de": "Alle Ränge"},
    "jobs.filter.rank.apply_first": {"en": "Apply first", "de": "Zuerst bewerben"},
    "jobs.filter.rank.strong_volume": {"en": "Strong volume", "de": "Starke Menge"},
    "jobs.filter.rank.frontier_stretch": {"en": "Frontier stretch", "de": "Ambitionierte Option"},
    "jobs.filter.rank.berlin_backups": {"en": "Berlin backups", "de": "Berlin-Reserve"},
    "jobs.filter.country": {"en": "Country", "de": "Land"},
    "jobs.filter.region": {"en": "Region", "de": "Region"},
    "jobs.filter.provider": {"en": "Source", "de": "Quelle"},
    "jobs.filter.any_source": {"en": "Any source", "de": "Alle Quellen"},
    "jobs.filter.source.manual": {"en": "Manual", "de": "Manuell"},
    "jobs.filter.source.linkedin": {"en": "LinkedIn", "de": "LinkedIn"},
    "jobs.filter.source.other": {"en": "Other", "de": "Andere"},
    "jobs.filter.verification": {"en": "Verification", "de": "Prüfung"},
    "jobs.filter.any_verification": {"en": "Any verification", "de": "Alle Prüfstände"},
    "jobs.filter.remote": {"en": "Remote", "de": "Remote"},
    "jobs.filter.seniority": {"en": "Seniority", "de": "Seniorität"},
    "jobs.filter.freshness": {"en": "Freshness", "de": "Aktualität"},
    "jobs.filter.group_aria": {"en": "Filter jobs", "de": "Jobs filtern"},
    "jobs.page_status_aria": {"en": "Page status", "de": "Seitenstatus"},
    "jobs.catalog_aria": {"en": "Jobs search and catalog filters", "de": "Jobsuche und Katalogfilter"},
    "jobs.detail_aria": {"en": "Job detail", "de": "Jobdetails"},
    "jobs.status_aria": {"en": "Job status", "de": "Jobstatus"},
    "jobs.delete_aria": {"en": "Delete job", "de": "Job löschen"},
    "jobs.delete_tooltip": {
        "en": "Delete this job (your tailored docs are kept)",
        "de": "Diesen Job löschen (angepasste Dokumente bleiben erhalten)",
    },
    "jobs.downstream.tailor": {"en": "ATS check", "de": "ATS-Prüfung"},
    "jobs.downstream.tailor_aria": {"en": "Run ATS check for this saved job", "de": "ATS-Prüfung für diesen gespeicherten Job ausführen"},
    "jobs.downstream.cover": {"en": "Cover letter", "de": "Anschreiben"},
    "jobs.downstream.cover_aria": {"en": "Create a cover letter for this saved job", "de": "Anschreiben für diesen gespeicherten Job erstellen"},
    "jobs.star.star": {"en": "Star job", "de": "Job markieren"},
    "jobs.star.unstar": {"en": "Unstar job", "de": "Markierung entfernen"},
    "jobs.star.failed": {"en": "Couldn't update the star.", "de": "Markierung konnte nicht aktualisiert werden."},
    "jobs.import.summary": {"en": "Import preview", "de": "Importvorschau"},
    "jobs.import.intro": {
        "en": "Search configured local sources or paste exported postings. Results stay in review until you explicitly save selected rows.",
        "de": "Durchsuche konfigurierte lokale Quellen oder füge exportierte Anzeigen ein. Ergebnisse bleiben in Prüfung, bis du ausgewählte Zeilen speicherst.",
    },
    "jobs.import.source.label": {"en": "Source", "de": "Quelle"},
    "jobs.import.source.default": {"en": "Profile default", "de": "Profilstandard"},
    "jobs.import.source.manual": {"en": "Manual paste/export", "de": "Manuelles Einfügen/Export"},
    "jobs.import.source.linkedin": {"en": "LinkedIn local DB", "de": "Lokale LinkedIn-Datenbank"},
    "jobs.import.source.all": {"en": "All local sources", "de": "Alle lokalen Quellen"},
    "jobs.import.title_filter": {"en": "Title/query", "de": "Titel/Suche"},
    "jobs.import.country": {"en": "Country", "de": "Land"},
    "jobs.import.region": {"en": "Region", "de": "Region"},
    "jobs.import.remote_mode": {"en": "Remote mode", "de": "Remote-Modus"},
    "jobs.import.seniority": {"en": "Seniority", "de": "Seniorität"},
    "jobs.import.posting_text": {"en": "Pasted posting", "de": "Eingefügte Anzeige"},
    "jobs.import.preview": {"en": "Preview import", "de": "Import prüfen"},
    "jobs.import.local_search": {"en": "Search local source", "de": "Lokale Quelle suchen"},
    "jobs.import.save_selected": {"en": "Save selected", "de": "Ausgewählte speichern"},
    "jobs.import.mark_stale": {"en": "Mark stale", "de": "Als veraltet markieren"},
    "jobs.import.feedback.searching": {"en": "Searching local source…", "de": "Lokale Quelle wird durchsucht…"},
    "jobs.import.feedback.previewing": {"en": "Preparing import preview…", "de": "Importvorschau wird vorbereitet…"},
    "jobs.import.feedback.paste_first": {"en": "Paste posting text before previewing.", "de": "Füge vor der Vorschau Anzeigentext ein."},
    "jobs.import.feedback.preview_failed": {"en": "Import preview failed.", "de": "Importvorschau fehlgeschlagen."},
    "jobs.import.feedback.no_results": {"en": "No import rows returned.", "de": "Keine Importzeilen zurückgegeben."},
    "jobs.import.feedback.preview_ready": {
        "en": "Preview ready. Rows are unverified; review before saving.",
        "de": "Vorschau bereit. Zeilen sind ungeprüft; vor dem Speichern prüfen.",
    },
    "jobs.import.feedback.network_error": {"en": "Network error - please try again.", "de": "Netzwerkfehler - bitte erneut versuchen."},
    "jobs.import.feedback.select_one": {
        "en": "Select at least one preview row.",
        "de": "Wähle mindestens eine Vorschauzeile aus.",
    },
    "jobs.import.feedback.saved_count": {"en": "Saved {n} posting(s).", "de": "{n} Anzeige(n) gespeichert."},
    "jobs.import.feedback.saved": {"en": "Selected jobs saved for review.", "de": "Ausgewählte Jobs zur Prüfung gespeichert."},
    "jobs.import.feedback.save_failed": {"en": "Save selected failed.", "de": "Ausgewählte konnten nicht gespeichert werden."},
    "jobs.import.feedback.local_search_failed": {"en": "Local search failed.", "de": "Lokale Suche fehlgeschlagen."},
    "jobs.import.feedback.local_preview_failed": {"en": "Local preview run failed.", "de": "Lokale Vorschau fehlgeschlagen."},
    "jobs.import.feedback.stale": {"en": "Selected rows marked stale.", "de": "Ausgewählte Zeilen als veraltet markiert."},
    "jobs.provenance.source": {"en": "Source", "de": "Quelle"},
    "jobs.provenance.external_id": {"en": "External ID", "de": "Externe ID"},
    "jobs.provenance.verification": {"en": "Verification", "de": "Prüfung"},
    "jobs.provenance.location": {"en": "Location", "de": "Ort"},
    "jobs.provenance.remote": {"en": "Remote", "de": "Remote"},
    "jobs.provenance.seniority": {"en": "Seniority", "de": "Seniorität"},
    "jobs.provenance.salary": {"en": "Salary", "de": "Gehalt"},
    "jobs.provenance.last_seen": {"en": "Last seen", "de": "Zuletzt gesehen"},
    "jobs.provenance.expires": {"en": "Expires", "de": "Läuft ab"},
    "jobs.provenance.origin": {"en": "Origin", "de": "Herkunft"},
    "jobs.freshness.fresh": {"en": "fresh", "de": "aktuell"},
    "jobs.freshness.stale": {"en": "stale", "de": "veraltet"},
    "jobs.years": {"en": "years", "de": "Jahre"},
    "jobs.match.criteria": {"en": "Criteria match", "de": "Kriterien-Treffer"},
    "jobs.match.broader": {"en": "Broader match", "de": "Breiterer Treffer"},
    "jobs.verification.user_provided": {"en": "User provided", "de": "Nutzerangabe"},
    "jobs.verification.needs_review": {"en": "Needs review", "de": "Prüfung nötig"},
    "jobs.verification.verified": {"en": "Verified", "de": "Geprüft"},
    "jobs.verification.stale": {"en": "Stale", "de": "Veraltet"},
    "jobs.fit": {"en": "Fit", "de": "Passung"},
    "jobs.sort.label": {"en": "Sort", "de": "Sortieren"},
    "jobs.sort.smart": {"en": "Smart (cover · deadline)", "de": "Smart (Anschreiben · Frist)"},
    "jobs.sort.deadline": {"en": "Deadline soonest", "de": "Frist am nächsten"},
    "jobs.sort.confidence": {"en": "Confidence high", "de": "Konfidenz hoch"},
    "jobs.sort.shortlist_rank": {"en": "Shortlist rank", "de": "Shortlist-Rang"},
    "jobs.sort.created": {"en": "Newest added", "de": "Zuletzt hinzugefügt"},
    "jobs.sort.aria": {"en": "Sort jobs", "de": "Jobs sortieren"},
    "jobs.saved_filters_aria": {"en": "Saved-job filters", "de": "Gespeicherte Jobfilter"},
    "jobs.source.revalidate": {"en": "Revalidate", "de": "Neu prüfen"},
    "jobs.source.panel_aria": {"en": "Job source state", "de": "Jobquellenstatus"},
    "jobs.source.state": {"en": "Source state", "de": "Quellenstatus"},
    "jobs.source.state.stale": {"en": "Stale source", "de": "Veraltete Quelle"},
    "jobs.source.state.fresh": {"en": "Fresh source", "de": "Aktuelle Quelle"},
    "jobs.source.state.review": {"en": "Source review", "de": "Quellenprüfung"},
    "jobs.source.state.full_jd_required": {"en": "Full JD review required", "de": "Vollständige Stellenbeschreibung prüfen"},
    "jobs.source.last_seen": {"en": "Last seen", "de": "Zuletzt gesehen"},
    "jobs.source.verified": {"en": "Verified", "de": "Geprüft"},
    "jobs.source.unknown": {"en": "unknown", "de": "unbekannt"},
    "jobs.source.not_recorded": {"en": "not recorded", "de": "nicht erfasst"},
    "jobs.source.full_jd_reviewed": {"en": "Full JD reviewed", "de": "Vollständige Stellenbeschreibung geprüft"},
    "jobs.source.full_jd_missing": {"en": "Full JD missing", "de": "Vollständige Stellenbeschreibung fehlt"},
    "jobs.source.full_jd_label": {
        "en": "Reviewed full job description",
        "de": "Geprüfte vollständige Stellenbeschreibung",
    },
    "jobs.source.full_jd_placeholder": {
        "en": "Paste reviewed full JD before tailoring if the source is incomplete.",
        "de": "Füge vor dem Anpassen die geprüfte vollständige Stellenbeschreibung ein, falls die Quelle unvollständig ist.",
    },
    "jobs.source.full_jd_placeholder_short": {
        "en": "Paste the reviewed full JD here before tailoring.",
        "de": "Füge hier vor dem Anpassen die geprüfte vollständige Stellenbeschreibung ein.",
    },
    "jobs.source.full_jd_blocked": {
        "en": "Paste and revalidate the reviewed full job description before tailoring this shortlist lead.",
        "de": "Füge die geprüfte vollständige Stellenbeschreibung ein und prüfe sie erneut, bevor du diesen Shortlist-Job anpasst.",
    },
    "jobs.selector.empty": {
        "en": "No saved jobs yet —",
        "de": "Noch keine gespeicherten Jobs —",
    },
    "jobs.selector.empty_link": {
        "en": "open the Jobs repository",
        "de": "Jobs-Übersicht öffnen",
    },
    "jobs.selector.needs_full_jd": {
        "en": "(needs full JD)",
        "de": "(JD unvollständig)",
    },
    "jobs.selector.full_jd_warning": {
        "en": "This imported job still needs its full job description reviewed before scoring or generation.",
        "de": "Für diesen importierten Job muss die vollständige Stellenbeschreibung geprüft werden, bevor Scoring oder Generierung möglich ist.",
    },
    "jobs.selector.open_in_jobs": {
        "en": "Open in Jobs",
        "de": "In Jobs öffnen",
    },
    "jobs.source.feedback.updating": {"en": "Updating source review state...", "de": "Quellenprüfung wird aktualisiert..."},
    "jobs.source.feedback.failed": {"en": "Source revalidation failed.", "de": "Quellenprüfung fehlgeschlagen."},
    "jobs.source.feedback.updated": {"en": "Source review state updated.", "de": "Quellenprüfung aktualisiert."},
    "jobs.source.feedback.network": {"en": "Network error updating source state.", "de": "Netzwerkfehler beim Aktualisieren der Quelle."},
    "jobs.review.cover_confirm": {"en": "Cover letter grounding reviewed against the tailored CV", "de": "Anschreiben-Grundlage gegen den zugeschnittenen CV geprüft"},
    "jobs.review.first": {"en": "Review first", "de": "Zuerst prüfen"},
    "jobs.review.heading": {"en": "Reviewed CV changes", "de": "Geprüfte Lebenslaufänderungen"},
    "jobs.review.busy": {"en": "Reviewing…", "de": "Prüfe…"},
    "jobs.review.preparing": {"en": "Preparing a reviewed CV copy. Nothing is saved until you apply it.", "de": "Geprüfte Lebenslaufkopie wird vorbereitet. Bis zur Anwendung wird nichts gespeichert."},
    "jobs.review.duplicate": {"en": "Current packet already exists for this source CV and job.", "de": "Das aktuelle Paket existiert bereits für diesen Quell-Lebenslauf und Job."},
    "jobs.review.ready": {"en": "Review ready. Inspect the changes, then apply to create the packet.", "de": "Prüfung bereit. Prüfe die Änderungen und erstelle dann das Paket."},
    "jobs.review.different_job": {"en": "Review belongs to a different job. Prepare a fresh review.", "de": "Die Prüfung gehört zu einem anderen Job. Erstelle eine neue Prüfung."},
    "jobs.review.confirm_grounding": {"en": "Review and confirm the cover-letter grounding before packet completion.", "de": "Prüfe und bestätige die Anschreiben-Grundlage vor Paketabschluss."},
    "jobs.review.additions": {"en": "additions", "de": "Ergänzungen"},
    "jobs.review.removals": {"en": "removals", "de": "Entfernungen"},
    "jobs.review.no_yaml_changes": {"en": "No YAML changes", "de": "Keine YAML-Änderungen"},
    "jobs.review.apply_summary": {"en": "Apply creates the tailored CV and writes the linked cover letter.", "de": "Anwenden erstellt den angepassten Lebenslauf und schreibt das verknüpfte Anschreiben."},
    "jobs.review.no_diff": {"en": "No diff returned.", "de": "Kein Diff zurückgegeben."},
    "jobs.review.review": {"en": "Review", "de": "Prüfung"},
    "jobs.review.source_cv": {"en": "Source CV", "de": "Quell-Lebenslauf"},
    "jobs.review.source_hash": {"en": "source", "de": "Quelle"},
    "jobs.review.jd_hash": {"en": "JD", "de": "JD"},  # hash-chip abbreviation; the full word overflows compact chips (plan 107, F8)
    "jobs.review.job": {"en": "Job", "de": "Job"},
    "jobs.review.before": {"en": "Before", "de": "Vorher"},
    "jobs.review.after": {"en": "After", "de": "Nachher"},
    "jobs.review.source_revision": {"en": "source revision", "de": "Quellrevision"},
    "jobs.review.tailored_artifact": {"en": "reviewed tailored artifact", "de": "geprüftes angepasstes Artefakt"},
    "jobs.review.current": {"en": "current", "de": "aktuell"},
    "jobs.review.post_tailor_ats": {"en": "Post-tailor ATS", "de": "ATS nach Anpassung"},
    "jobs.review.not_returned": {"en": "not returned", "de": "nicht zurückgegeben"},
    "jobs.review.years_fit": {"en": "Years fit", "de": "Jahre-Fit"},
    "jobs.review.unknown": {"en": "unknown", "de": "unbekannt"},
    "jobs.review.grounding_evidence": {
        "en": "Cover letter will be generated from the reviewed tailored CV and current job JD.",
        "de": "Das Anschreiben wird aus dem geprüften angepassten Lebenslauf und der aktuellen Stellenbeschreibung erzeugt.",
    },
    "jobs.search": {"en": "Search title, company, field…", "de": "Titel, Unternehmen, Bereich suchen…"},
    "jobs.search_aria": {"en": "Search jobs", "de": "Jobs suchen"},
    "jobs.source_cv_aria": {"en": "Source CV", "de": "Quell-Lebenslauf"},
    "jobs.experience.aria": {"en": "Experience fit evidence", "de": "Erfahrungs-Fit-Nachweis"},
    "jobs.experience.required": {"en": "Required", "de": "Erforderlich"},
    "jobs.experience.candidate": {"en": "Candidate", "de": "Kandidat"},
    "jobs.experience.missing": {"en": "Missing", "de": "Fehlend"},
    "jobs.experience.rank_contribution": {"en": "Rank contribution", "de": "Rangbeitrag"},
    "jobs.detail.empty": {
        "en": (
            "Select a job from the catalog to see its details, tailor a CV to "
            "it, and generate a matching cover letter."
        ),
        "de": (
            "Wähle einen Job aus dem Katalog, um Details zu sehen, einen "
            "Lebenslauf anzupassen und ein passendes Anschreiben zu erzeugen."
        ),
    },
    "jobs.detail.source_cv": {
        "en": "Source CV for one-click generate",
        "de": "Quell-Lebenslauf für Ein-Klick-Erzeugung",
    },
    "jobs.detail.note": {
        "en": (
            "Tailors a copy of that CV to this job and writes a cover letter "
            "grounded only in your CV. Invented facts and 2-page CVs are blocked."
        ),
        "de": (
            "Passt eine Kopie dieses Lebenslaufs an den Job an und schreibt ein "
            "Anschreiben, das nur auf deinem Lebenslauf basiert. Erfundene "
            "Angaben und 2-seitige Lebensläufe werden blockiert."
        ),
    },
    # Status labels (also used by the status <select> + card badges).
    "jobs.status.saved": {"en": "Saved", "de": "Gespeichert"},
    "jobs.status.open": {"en": "Open", "de": "Offen"},
    "jobs.status.speculative": {"en": "Speculative", "de": "Initiativ"},
    "jobs.status.applied": {"en": "Applied", "de": "Beworben"},
    "jobs.status.closed": {"en": "Closed", "de": "Geschlossen"},
    # Card / detail chrome.
    "jobs.note_label": {"en": "Your note", "de": "Deine Notiz"},
    "jobs.badge.cover": {"en": "Cover", "de": "Anschreiben"},
    "jobs.deadline.rolling": {"en": "Rolling", "de": "Laufend"},
    "jobs.deadline.closed": {"en": "Closed", "de": "Abgelaufen"},
    "jobs.deadline.due": {"en": "Due", "de": "Fällig"},
    "jobs.card.details": {"en": "Details", "de": "Details"},
    "jobs.card.open_detail": {"en": "Open job details", "de": "Jobdetails öffnen"},
    "jobs.card.open": {"en": "Open", "de": "Öffnen"},
    "jobs.card.generate": {"en": "Generate", "de": "Erzeugen"},
    "jobs.untitled": {"en": "Untitled job", "de": "Unbenannter Job"},
    "jobs.company_missing": {"en": "Company not detected", "de": "Unternehmen nicht erkannt"},
    "jobs.error.load": {"en": "Could not load saved jobs.", "de": "Gespeicherte Jobs konnten nicht geladen werden."},
    "jobs.empty.none": {
        "en": "No saved jobs yet. Use “Save a job” above to add one.",
        "de": "Noch keine gespeicherten Jobs. Nutze oben „Job speichern“, um einen anzulegen.",
    },
    "jobs.empty.filter": {"en": "No jobs match this filter.", "de": "Keine Jobs passen zu diesem Filter."},
    "jobs.empty.cvs": {"en": "No tailored CVs yet.", "de": "Noch keine angepassten Lebensläufe."},
    "jobs.empty.letters": {"en": "No cover letters yet.", "de": "Noch keine Anschreiben."},
    "jobs.add.saving": {"en": "Saving…", "de": "Speichere…"},
    "jobs.add.parsing": {"en": "Parsing and saving the job…", "de": "Job wird gelesen und gespeichert…"},
    "jobs.add.saved": {"en": "Saved.", "de": "Gespeichert."},
    "jobs.add.error.no_input": {
        "en": "Paste a job description or give a public non-LinkedIn URL.",
        "de": "Füge eine Stellenbeschreibung ein oder gib eine öffentliche Nicht-LinkedIn-URL an.",
    },
    "jobs.add.error.url_blocked_title": {"en": "That URL isn't allowed.", "de": "Diese URL ist nicht erlaubt."},
    "jobs.add.error.url_blocked_body": {
        "en": "For safety we don't fetch internal or private addresses — paste the text instead.",
        "de": "Aus Sicherheitsgründen werden interne oder private Adressen nicht abgerufen — füge stattdessen den Text ein.",
    },
    "jobs.add.error.linkedin_title": {"en": "Paste the LinkedIn job text.", "de": "Füge den LinkedIn-Stellentext ein."},
    "jobs.add.error.linkedin_body": {
        "en": "This app does not blind-fetch LinkedIn pages; approved adapters are disabled unless configured.",
        "de": "Diese App ruft LinkedIn-Seiten nicht blind ab; genehmigte Adapter bleiben deaktiviert, solange sie nicht konfiguriert sind.",
    },
    "jobs.add.error.network": {"en": "Network error — please try again.", "de": "Netzwerkfehler — bitte erneut versuchen."},
    "jobs.generate.full": {
        "en": "Generate CV + cover letter",
        "de": "Lebenslauf + Anschreiben erzeugen",
    },
    "jobs.generate.busy": {"en": "Generating…", "de": "Erzeuge…"},
    "jobs.generate.busy_note": {
        "en": (
            "Tailoring a copy of your CV and writing a grounded letter — "
            "checking every fact and keeping the CV to one page…"
        ),
        "de": (
            "Eine Kopie deines Lebenslaufs wird angepasst und ein fundiertes "
            "Anschreiben geschrieben — jede Angabe wird geprüft und der "
            "Lebenslauf bleibt einseitig…"
        ),
    },
    "jobs.generate.no_cv": {
        "en": "Add a CV first, then pick it as the source.",
        "de": "Lege zuerst einen Lebenslauf an und wähle ihn dann als Quelle.",
    },
    "jobs.generate.ats_match": {"en": "ATS match", "de": "ATS-Passung"},
    "jobs.generate.done": {
        "en": "Done — a tailored CV and a grounded cover letter were created and linked to this job",
        "de": "Fertig — ein angepasster Lebenslauf und ein fundiertes Anschreiben wurden erstellt und mit diesem Job verknüpft",
    },
    "jobs.linked.cvs": {"en": "Tailored CVs", "de": "Angepasste Lebensläufe"},
    "jobs.linked.letters": {"en": "Cover letters", "de": "Anschreiben"},
    "jobs.packet.title": {"en": "Application packet", "de": "Bewerbungspaket"},
    "jobs.packet.aria": {"en": "Application packet", "de": "Bewerbungspaket"},
    "jobs.packet.create": {"en": "Create packet", "de": "Paket erstellen"},
    "jobs.packet.creating": {"en": "Creating...", "de": "Erstelle..."},
    "jobs.packet.applying": {"en": "Applying the reviewed CV and writing the cover letter...", "de": "Der geprüfte Lebenslauf wird angewendet und das Anschreiben geschrieben..."},
    "jobs.packet.created": {"en": "Packet created from the reviewed CV changes.", "de": "Paket aus den geprüften Lebenslaufänderungen erstellt."},
    "jobs.packet.none": {"en": "No packet", "de": "Kein Paket"},
    "jobs.packet.packet": {"en": "Packet", "de": "Paket"},
    "jobs.packet.archived": {"en": "Archived packet", "de": "Archiviertes Paket"},
    "jobs.packet.stale": {"en": "Stale packet", "de": "Veraltetes Paket"},
    "jobs.packet.applied_external": {"en": "Applied externally", "de": "Extern beworben"},
    "jobs.packet.incomplete": {"en": "Incomplete packet", "de": "Unvollständiges Paket"},
    "jobs.packet.archive": {"en": "Archive", "de": "Archivieren"},
    "jobs.packet.regenerate": {"en": "Regenerate", "de": "Neu erzeugen"},
    "jobs.packet.feedback.regeneration_ready": {
        "en": "Regeneration review ready. Inspect the changes, then create the replacement packet.",
        "de": "Prüfung für Neuerzeugung bereit. Prüfe die Änderungen und erstelle dann das Ersatzpaket.",
    },
    "jobs.packet.feedback.archived": {"en": "Packet archived.", "de": "Paket archiviert."},
    "jobs.packet.feedback.regenerated": {"en": "Packet regenerated.", "de": "Paket neu erzeugt."},
    "jobs.packet.feedback.review_required": {
        "en": "Regeneration requires a fresh review. Review the current CV changes before creating the packet.",
        "de": "Die Neuerzeugung erfordert eine neue Prüfung. Prüfe die aktuellen Lebenslaufänderungen vor der Paketerstellung.",
    },
    "jobs.packet.feedback.action_failed": {"en": "Packet action failed", "de": "Paketaktion fehlgeschlagen"},
    "jobs.packet.cover_review_confirmed": {"en": "cover review confirmed", "de": "Anschreibenprüfung bestätigt"},
    "jobs.packet.unverified_claims": {
        "en": "Blocked - couldn't verify these claims against your CV:",
        "de": "Blockiert - diese Aussagen konnten nicht gegen deinen Lebenslauf geprüft werden:",
    },
    "jobs.packet.nothing_saved": {"en": "Nothing was saved.", "de": "Nichts wurde gespeichert."},
    "jobs.packet.overflow": {
        "en": "The reviewed CV overflowed onto a second page",
        "de": "Der geprüfte Lebenslauf läuft auf eine zweite Seite über",
    },
    "jobs.packet.a_section": {"en": "a section", "de": "ein Abschnitt"},
    "jobs.packet.needs_key": {
        "en": "You're out of free generations. Add your API key in the account settings to continue.",
        "de": "Deine freien Generierungen sind aufgebraucht. Hinterlege deinen API-Schlüssel in den Kontoeinstellungen.",
    },
    "jobs.linked.pdf": {"en": "PDF", "de": "PDF"},
    "jobs.linked.building": {"en": "Building", "de": "Erstelle"},
    "jobs.provenance.aria": {"en": "Job source provenance", "de": "Jobquellen-Provenienz"},
    "jobs.pdf.building": {"en": "Building PDF", "de": "PDF wird erstellt"},
    "jobs.pdf.building_dots": {"en": "Building PDF...", "de": "PDF wird erstellt..."},
    "jobs.network_error": {"en": "Network error - please try again.", "de": "Netzwerkfehler - bitte erneut versuchen."},
    "jobs.ai_unavailable": {"en": "AI is temporarily unavailable. Try again shortly.", "de": "KI ist vorübergehend nicht verfügbar. Versuche es in Kürze erneut."},
    "jobs.request_failed": {"en": "Request failed", "de": "Anfrage fehlgeschlagen"},
    "jobs.delete_confirm": {
        "en": "Delete this job? Your tailored CVs and letters are kept, just unlinked.",
        "de": "Diesen Job löschen? Angepasste Lebensläufe und Anschreiben bleiben erhalten und werden nur entknüpft.",
    },
    "jobs.delete_failed": {"en": "Couldn't delete that job.", "de": "Dieser Job konnte nicht gelöscht werden."},
    "jobs.delete_network": {"en": "Network error deleting job.", "de": "Netzwerkfehler beim Löschen des Jobs."},
    "jobs.generate.review_required": {
        "en": "Preparing the reviewed CV changes. Packet creation is blocked until cover-letter grounding is confirmed.",
        "de": "Geprüfte Lebenslaufänderungen werden vorbereitet. Paketerstellung bleibt blockiert, bis die Anschreiben-Grundlage bestätigt ist.",
    },
    # ── Cover-letter library ──────────────────────────────────────────────
    "cl.title": {"en": "Cover letters", "de": "Anschreiben"},
    "cl.page.sub": {
        "en": "Saved drafts are listed here. Generate a grounded letter from a source CV and keep editing it later.",
        "de": "Gespeicherte Entwürfe stehen hier. Erzeuge ein fundiertes Anschreiben aus einem Quell-Lebenslauf und bearbeite es später weiter.",
    },
    "cl.status.editable": {"en": "Editable drafts", "de": "Editierbare Entwürfe"},
    "cl.generate.heading": {"en": "Generate a grounded letter", "de": "Fundiertes Anschreiben erzeugen"},
    "cl.generate.intro": {
        "en": (
            "Pick one of your CVs and paste the job description. The letter is "
            "written only from facts in your CV — invented employers or metrics "
            "are blocked before saving."
        ),
        "de": (
            "Wähle einen deiner Lebensläufe und füge die Stellenbeschreibung ein. "
            "Das Anschreiben basiert nur auf Fakten aus deinem Lebenslauf — "
            "erfundene Arbeitgeber oder Kennzahlen werden vor dem Speichern blockiert."
        ),
    },
    "cl.field.source_cv": {"en": "Source CV", "de": "Quell-Lebenslauf"},
    "cl.source.empty": {
        "en": "No CVs available",
        "de": "Keine Lebensläufe verfügbar",
    },
    "cl.source.empty_note": {
        "en": "Create or import a CV before generating a cover letter.",
        "de": "Erstelle oder importiere zuerst einen Lebenslauf.",
    },
    "cl.field.tone": {"en": "Tone", "de": "Ton"},
    "cl.field.recipient": {"en": "Hiring contact (optional)", "de": "Ansprechpartner (optional)"},
    "cl.field.recipient_placeholder": {
        "en": "e.g. Dr. Jane Doe, or the Data Platform team",
        "de": "z. B. Dr. Jane Doe oder das Data-Platform-Team",
    },
    "cl.field.saved_job": {"en": "Saved job", "de": "Gespeicherter Job"},
    "cl.field.saved_job_none": {"en": "Manual job description", "de": "Manuelle Stellenbeschreibung"},
    "cl.field.jd": {"en": "Job description", "de": "Stellenbeschreibung"},
    "cl.field.jd_placeholder": {
        "en": "Paste the full job posting here...",
        "de": "Vollständige Stellenanzeige hier einfügen...",
    },
    "cl.tone.auto": {"en": "Auto (match my CV's voice)", "de": "Auto (Stimme meines Lebenslaufs)"},
    "cl.tone.formal": {"en": "Formal", "de": "Förmlich"},
    "cl.tone.conversational": {"en": "Conversational", "de": "Locker"},
    "cl.tone.enthusiastic": {"en": "Enthusiastic", "de": "Begeistert"},
    "cl.tone.concise": {"en": "Concise & direct", "de": "Knapp & direkt"},
    "cl.generate.button": {"en": "Generate letter", "de": "Anschreiben erzeugen"},
    "cl.generate.busy": {"en": "Generating…", "de": "Erzeuge…"},
    "cl.generate.busy_note": {
        "en": "Writing your letter and checking every fact against your CV…",
        "de": "Dein Anschreiben wird geschrieben und jede Angabe mit deinem Lebenslauf geprüft…",
    },
    "cl.save_edits": {"en": "Save edits", "de": "Änderungen speichern"},
    "cl.feedback.pick_cv": {"en": "Pick a source CV first.", "de": "Wähle zuerst einen Quell-Lebenslauf."},
    "cl.feedback.paste_jd": {"en": "Paste the job description.", "de": "Füge die Stellenbeschreibung ein."},
    "cl.feedback.verified": {
        "en": "Every name and number was verified against your CV.",
        "de": "Jeder Name und jede Zahl wurde gegen deinen Lebenslauf geprüft.",
    },
    "cl.feedback.removed_cliches": {"en": "Removed {n} cliché", "de": "{n} Floskel entfernt"},
    "cl.feedback.removed_cliches_plural": {"en": "Removed {n} clichés", "de": "{n} Floskeln entfernt"},
    "cl.feedback.unverified": {
        "en": "Blocked - couldn't verify these claims against your CV:",
        "de": "Blockiert - diese Aussagen konnten nicht gegen deinen Lebenslauf geprüft werden:",
    },
    "cl.feedback.not_saved": {"en": "The letter was not saved.", "de": "Das Anschreiben wurde nicht gespeichert."},
    "cl.feedback.adjust_cv": {
        "en": "These look invented; regenerate or adjust your CV so they're grounded.",
        "de": "Diese Angaben wirken erfunden; neu erzeugen oder den Lebenslauf so ergänzen, dass sie belegt sind.",
    },
    "cl.feedback.needs_key": {
        "en": "Connect the selected provider key in Account to continue.",
        "de": "Verbinde den ausgewählten Provider-Schlüssel im Konto, um fortzufahren.",
    },
    "cl.feedback.ai_unavailable": {
        "en": "AI is temporarily unavailable. Try again shortly.",
        "de": "KI ist vorübergehend nicht verfügbar. Versuche es in Kürze erneut.",
    },
    "cl.feedback.generation_failed": {"en": "Generation failed", "de": "Erzeugung fehlgeschlagen"},
    "cl.feedback.network": {"en": "Network error - please try again.", "de": "Netzwerkfehler - bitte erneut versuchen."},
    "cl.feedback.preview_failed": {"en": "Couldn't render the preview.", "de": "Vorschau konnte nicht gerendert werden."},
    "cl.feedback.load_cvs_failed": {"en": "Couldn't load your CVs.", "de": "Deine Lebensläufe konnten nicht geladen werden."},
    "cl.feedback.open_failed": {"en": "Couldn't open that letter.", "de": "Dieses Anschreiben konnte nicht geöffnet werden."},
    "cl.feedback.saved": {"en": "Saved.", "de": "Gespeichert."},
    "cl.feedback.save_failed": {"en": "Save failed.", "de": "Speichern fehlgeschlagen."},
    "cl.feedback.save_network": {"en": "Save failed - network error.", "de": "Speichern fehlgeschlagen - Netzwerkfehler."},
    "cl.feedback.pdf_failed": {"en": "Couldn't render the PDF.", "de": "PDF konnte nicht gerendert werden."},
    "cl.confirm.delete_cv": {"en": "Delete \"{name}\"? This can't be undone.", "de": "\"{name}\" löschen? Das kann nicht rückgängig gemacht werden."},
    "cl.confirm.delete_letter": {"en": "Delete \"{name}\"?", "de": "\"{name}\" löschen?"},
    "cl.feedback.delete_cv_failed": {"en": "Couldn't delete that CV.", "de": "Dieser Lebenslauf konnte nicht gelöscht werden."},
    "cl.feedback.delete_letter_failed": {"en": "Couldn't delete that letter.", "de": "Dieses Anschreiben konnte nicht gelöscht werden."},
    "cl.pdf": {"en": "PDF", "de": "PDF"},
    "cl.preview": {"en": "Preview", "de": "Vorschau"},
    "cl.preview_title": {"en": "Cover letter preview", "de": "Anschreiben-Vorschau"},
    "cl.result_aria": {"en": "Cover letter", "de": "Anschreiben"},
    "cl.body.aria": {"en": "Cover letter body", "de": "Anschreiben-Text"},
    "cl.body.placeholder": {
        "en": "Your generated letter appears here and stays editable.",
        "de": "Dein erzeugtes Anschreiben erscheint hier und bleibt editierbar.",
    },
    "cl.word.one": {"en": "{n} word", "de": "{n} Wort"},
    "cl.word.many": {"en": "{n} words", "de": "{n} Wörter"},
    "cl.packet.lineage_aria": {"en": "Application packet lineage", "de": "Bewerbungspaket-Herkunft"},
    "cl.packet.hashes": {"en": "Hashes", "de": "Hashes"},
    "cl.packet.hash_source": {"en": "source", "de": "Quelle"},
    "cl.packet.hash_jd": {"en": "JD", "de": "JD"},  # hash-chip abbreviation (plan 107, F8)
    "cl.packet.hash_cv": {"en": "cv", "de": "Lebenslauf"},
    "cl.packet.hash_letter": {"en": "letter", "de": "Anschreiben"},
    "cl.packet.review": {"en": "Review", "de": "Prüfung"},
    "cl.packet.packet": {"en": "Packet", "de": "Paket"},
    "cl.packet.linked_job": {"en": "Linked job", "de": "Verknüpfter Job"},
    "cl.packet.state.packet_created": {"en": "packet created", "de": "Paket erstellt"},
    "cl.packet.state.stale": {"en": "stale", "de": "veraltet"},
    "cl.packet.state.archived": {"en": "archived", "de": "archiviert"},
    "lib.heading": {"en": "Saved cover letters", "de": "Gespeicherte Anschreiben"},
    "lib.filter.all": {"en": "All", "de": "Alle"},
    "lib.filter.cv": {"en": "CVs", "de": "Lebensläufe"},
    "lib.filter.cover": {"en": "Cover letters", "de": "Anschreiben"},
    "lib.badge.cv": {"en": "CV", "de": "Lebenslauf"},
    "lib.badge.cover": {"en": "Cover letter", "de": "Anschreiben"},
    "lib.storage.saved": {"en": "Saved in Documents", "de": "In Dokumenten gespeichert"},
    "lib.storage.linked_job": {"en": "Linked to this job", "de": "Mit diesem Job verknüpft"},
    "lib.storage.standalone_letter": {"en": "Standalone letter", "de": "Eigenständiges Anschreiben"},
    "lib.empty.all": {"en": "No documents yet.", "de": "Noch keine Dokumente."},
    "lib.empty.cv": {"en": "No CVs yet.", "de": "Noch keine Lebensläufe."},
    "lib.empty.cover": {"en": "No cover letters yet.", "de": "Noch keine Anschreiben."},
    "lib.result.none": {"en": "No letter selected", "de": "Kein Anschreiben ausgewählt"},
    "lib.untitled.cv": {"en": "Untitled CV", "de": "Unbenannter Lebenslauf"},
    "lib.action.download_pdf": {"en": "Download PDF", "de": "PDF herunterladen"},
    "lib.action.delete": {"en": "Delete", "de": "Löschen"},
    "account.mobile.utilities": {"en": "Account utilities", "de": "Kontodienste"},
    "app.accent_picker_base_aria": {"en": "Choose accent colour", "de": "Akzentfarbe wählen"},
    "app.accent_picker_aria": {"en": "Choose accent colour {value}", "de": "Akzentfarbe {value} wählen"},
    "mobile.language": {"en": "Language", "de": "Sprache"},
    "palette.jobs.add": {"en": "Add job", "de": "Job hinzufügen"},
    "palette.jobs.import_preview": {"en": "Preview job import", "de": "Jobimport prüfen"},
    "palette.jobs.generate": {"en": "Generate reviewed packet", "de": "Geprüftes Paket erzeugen"},
    "palette.letters.generate": {"en": "Generate cover letter", "de": "Anschreiben erzeugen"},
    "palette.letters.save": {"en": "Save cover letter", "de": "Anschreiben speichern"},
    "palette.letters.preview": {"en": "Toggle cover letter preview", "de": "Anschreiben-Vorschau umschalten"},
    "palette.letters.download": {"en": "Download cover letter PDF", "de": "Anschreiben-PDF herunterladen"},
    "palette.tailor.score": {"en": "Score match", "de": "Passung bewerten"},
    "palette.tailor.review": {"en": "Review changes", "de": "Änderungen prüfen"},
    "palette.tailor.apply": {"en": "Apply as new copy", "de": "Als neue Kopie anwenden"},
    "palette.tailor.optimize": {"en": "Optimize against this job", "de": "Auf diesen Job optimieren"},
    "palette.tailor.pdf": {"en": "Build tailored PDF", "de": "Tailored-PDF erstellen"},
    "tailor.form_aria": {"en": "Score and review CV against a job", "de": "Lebenslauf gegen einen Job bewerten und prüfen"},
    "tailor.form.heading": {"en": "Score and review a CV", "de": "Lebenslauf bewerten und prüfen"},
    "tailor.form.intro": {
        "en": (
            "Score how well one of your CVs already covers a job, then review a reviewed copy that surfaces "
            "your most relevant, real experience. Your original CV is never changed - applying reviewed output creates "
            "a new copy. Invented skills, employers, or metrics are blocked, and the tailored CV is held to one page."
        ),
        "de": (
            "Bewerte, wie gut einer deiner Lebensläufe einen Job bereits abdeckt, und prüfe dann eine Kopie, die "
            "deine relevanteste echte Erfahrung hervorhebt. Dein Original wird nie verändert - Anwenden erzeugt eine "
            "neue Kopie. Erfundenes zu Skills, Arbeitgebern oder Kennzahlen wird blockiert, und der angepasste Lebenslauf "
            "muss auf einer Seite bleiben."
        ),
    },
    "tailor.field.source_cv": {"en": "Source CV", "de": "Quell-Lebenslauf"},
    "tailor.field.profile": {"en": "ATS score system", "de": "ATS-Bewertungssystem"},
    "tailor.profile.keyword_screen": {"en": "Keyword screen", "de": "Keyword-Prüfung"},
    "tailor.profile.strict_requirements": {"en": "Strict requirements", "de": "Strikte Anforderungen"},
    "tailor.profile.semantic_fit": {"en": "Semantic fit", "de": "Semantische Passung"},
    "tailor.profile.disclaimer": {
        "en": "CV Copilot score-system simulations only. No vendor-backed ATS certification.",
        "de": "Nur CV-Copilot-Simulationen von Bewertungssystemen. Keine ATS-Zertifizierung durch Anbieter.",
    },
    "tailor.field.saved_job": {"en": "Saved job", "de": "Gespeicherter Job"},
    "tailor.field.saved_job_none": {"en": "Manual job description", "de": "Manuelle Stellenbeschreibung"},
    "tailor.field.jd": {"en": "Job description", "de": "Stellenbeschreibung"},
    "tailor.field.jd_placeholder": {
        "en": "Paste the full job posting here...",
        "de": "Vollständige Stellenanzeige hier einfügen...",
    },
    "tailor.field.url": {"en": "...or fetch from a URL (optional)", "de": "...oder von einer URL abrufen (optional)"},
    "tailor.field.url_disabled": {
        "en": "URL fetch is disabled on this server — paste the posting text or use a saved job.",
        "de": "URL-Abruf ist auf diesem Server deaktiviert — füge den Stellentext ein oder nutze einen gespeicherten Job.",
    },
    "tailor.field.url_placeholder": {
        "en": "https://... (public job posting; paste text above wins)",
        "de": "https://... (öffentliche Stellenanzeige; eingefügter Text oben hat Vorrang)",
    },
    "tailor.score.button": {"en": "Score match (free)", "de": "Passung bewerten (frei)"},
    "tailor.score.busy": {"en": "Scoring...", "de": "Bewerte..."},
    "tailor.review.button": {"en": "Review changes", "de": "Änderungen prüfen"},
    "tailor.review.busy": {"en": "Reviewing...", "de": "Prüfe..."},
    "tailor.optimize.button": {"en": "Optimize against this job", "de": "Für diesen Job optimieren"},
    "tailor.result_aria": {"en": "ATS match and gaps", "de": "ATS-Passung und Lücken"},
    "tailor.empty": {
        "en": "Score a match or tailor a copy to see your ATS keyword coverage and the requirements your CV doesn't yet evidence.",
        "de": "Bewerte eine Passung oder erstelle eine geprüfte Kopie, um ATS-Keyword-Abdeckung und noch nicht belegte Anforderungen zu sehen.",
    },
    "tailor.loading.score": {"en": "Scoring your match...", "de": "Passung wird bewertet..."},
    "tailor.loading.matching": {"en": "Matching the job against your CV...", "de": "Job wird mit deinem Lebenslauf abgeglichen..."},
    "tailor.loading.review": {
        "en": "Preparing reviewed changes - reordering and rephrasing your real experience, then checking every claim and holding it to one page...",
        "de": "Geprüfte Änderungen werden vorbereitet - echte Erfahrung wird umgeordnet und umformuliert, jede Aussage geprüft und die Ein-Seiten-Grenze gehalten...",
    },
    "tailor.gauge_aria": {"en": "ATS match score", "de": "ATS-Passungswert"},
    "tailor.gauge.caption": {"en": "ATS match", "de": "ATS-Passung"},
    "tailor.open_tailored": {"en": "Open tailored CV", "de": "Angepassten Lebenslauf öffnen"},
    "tailor.review_aria": {"en": "Reviewed CV changes", "de": "Geprüfte Lebenslaufänderungen"},
    "tailor.review.heading": {"en": "Review before applying", "de": "Vor dem Anwenden prüfen"},
    "tailor.profile.compare_aria": {"en": "ATS score-system comparison", "de": "Vergleich der ATS-Bewertungssysteme"},
    "tailor.categories.heading": {"en": "How this job reads your CV", "de": "Wie dieser Job deinen Lebenslauf liest"},
    "tailor.strengths.heading": {"en": "Strengths for this role", "de": "Stärken für diese Rolle"},
    "tailor.suggestions.heading": {"en": "Ways to strengthen your application", "de": "Möglichkeiten zur stärkeren Bewerbung"},
    "tailor.suggestions.hint": {
        "en": "Reframing your real experience only - click any tip to copy it.",
        "de": "Nur echte Erfahrung umformulieren - Tipp anklicken, um ihn zu kopieren.",
    },
    "tailor.gaps.heading": {"en": "Gaps - no evidence in your CV", "de": "Lücken - kein Nachweis im Lebenslauf"},
    "tailor.gaps.hint": {
        "en": "These appear in the job but not in your CV. Decide whether to apply anyway or skill up - never add a skill you can't back. Click a gap to copy it.",
        "de": "Diese Punkte stehen im Job, aber nicht in deinem Lebenslauf. Entscheide, ob du dich trotzdem bewirbst oder zuerst lernst - füge nie einen Skill hinzu, den du nicht belegen kannst. Lücke anklicken, um sie zu kopieren.",
    },
    "tailor.matched.heading": {"en": "Matched - backed by your CV", "de": "Treffer - durch deinen Lebenslauf belegt"},
    "tailor.feedback.finish_review": {
        "en": "Finish the reviewed copy before starting a new score.",
        "de": "Schließe die geprüfte Kopie ab, bevor du neu bewertest.",
    },
    "tailor.feedback.pick_cv": {"en": "Pick a source CV first.", "de": "Wähle zuerst einen Quell-Lebenslauf."},
    "tailor.feedback.paste_jd": {
        "en": "Paste a job description (or a URL).",
        "de": "Füge eine Stellenbeschreibung ein (oder eine URL).",
    },
    "tailor.feedback.scored_ai": {
        "en": "Match scored by the AI rubric - no copy was created.",
        "de": "Passung per KI-Rubrik bewertet - es wurde keine Kopie erstellt.",
    },
    "tailor.feedback.scored_cv": {
        "en": "Match scored from your CV - no copy was created.",
        "de": "Passung aus deinem Lebenslauf bewertet - es wurde keine Kopie erstellt.",
    },
    "tailor.feedback.reviewing": {
        "en": "Preparing reviewed changes. Nothing is saved until you apply the reviewed copy.",
        "de": "Geprüfte Änderungen werden vorbereitet. Gespeichert wird erst beim Anwenden der geprüften Kopie.",
    },
    "tailor.feedback.review_ready": {
        "en": "Review ready - inspect the diff, then apply it as a new copy. Your original CV is unchanged.",
        "de": "Prüfung bereit - prüfe den Diff und wende ihn dann als neue Kopie an. Dein Original bleibt unverändert.",
    },
    "tailor.feedback.notes": {"en": "{n} note below.", "de": "{n} Hinweis unten."},
    "tailor.feedback.notes_plural": {"en": "{n} notes below.", "de": "{n} Hinweise unten."},
    "tailor.feedback.applying": {"en": "Applying reviewed changes as a new CV copy...", "de": "Geprüfte Änderungen werden als neue Lebenslaufkopie angewendet..."},
    "tailor.feedback.applied": {
        "en": "Applied as a new reviewed copy - your original CV is unchanged.",
        "de": "Als neue geprüfte Kopie angewendet - dein Original bleibt unverändert.",
    },
    "tailor.feedback.network": {"en": "Network error - please try again.", "de": "Netzwerkfehler - bitte erneut versuchen."},
    "tailor.feedback.unverified": {
        "en": "Blocked - the tailored CV introduced claims not in your original:",
        "de": "Blockiert - der angepasste Lebenslauf enthält Aussagen, die nicht im Original stehen:",
    },
    "tailor.feedback.overflow": {
        "en": "Blocked - the tailored CV ran onto a second page",
        "de": "Blockiert - der angepasste Lebenslauf läuft auf eine zweite Seite",
    },
    "tailor.feedback.first_overflow": {"en": "first overflow section", "de": "erster überlaufender Abschnitt"},
    "tailor.feedback.nothing_saved": {"en": "Nothing was saved.", "de": "Nichts wurde gespeichert."},
    "tailor.feedback.source_changed": {
        "en": "The source CV changed after this review was prepared. Review the current CV before applying.",
        "de": "Der Quell-Lebenslauf wurde nach dieser Prüfung geändert. Prüfe den aktuellen Lebenslauf vor dem Anwenden.",
    },
    "tailor.feedback.duplicate": {"en": "This reviewed copy was already applied.", "de": "Diese geprüfte Kopie wurde bereits angewendet."},
    "tailor.feedback.expired": {"en": "This review expired. Prepare the review again before applying.", "de": "Diese Prüfung ist abgelaufen. Bereite sie vor dem Anwenden erneut vor."},
    "tailor.feedback.invalid_review": {"en": "This review could not be verified. Prepare the review again before applying.", "de": "Diese Prüfung konnte nicht verifiziert werden. Bereite sie vor dem Anwenden erneut vor."},
    "tailor.feedback.needs_key": {
        "en": "Connect the selected provider key in Account to continue.",
        "de": "Verbinde den ausgewählten Provider-Schlüssel im Konto, um fortzufahren.",
    },
    "tailor.feedback.ai_unavailable": {"en": "AI is temporarily unavailable. Try again shortly.", "de": "KI ist vorübergehend nicht verfügbar. Versuche es in Kürze erneut."},
    "tailor.feedback.request_failed": {"en": "Request failed", "de": "Anfrage fehlgeschlagen"},
    "tailor.report.no_keywords": {"en": "Not enough keywords in the job to score reliably.", "de": "Nicht genug Keywords im Job für eine verlässliche Bewertung."},
    "tailor.report.must_have": {"en": "Must-have coverage", "de": "Muss-Abdeckung"},
    "tailor.report.target": {"en": "target", "de": "Ziel"},
    "tailor.report.notes": {"en": "Notes", "de": "Hinweise"},
    "tailor.profile.best_for": {"en": "Best for", "de": "Am besten für"},
    "tailor.profile.method": {"en": "Method", "de": "Methode"},
    "tailor.profile.proof": {"en": "Proof", "de": "Nachweis"},
    "tailor.profile.vendor": {"en": "Vendor", "de": "Anbieter"},
    "tailor.profile.external": {"en": "External profile", "de": "Externes Profil"},
    "tailor.profile.simulation": {"en": "CV Copilot simulation", "de": "CV-Copilot-Simulation"},
    "tailor.profile.vendor_backed": {"en": "Vendor-backed", "de": "Anbieter-gestützt"},
    "tailor.profile.not_vendor_backed": {"en": "Not vendor-backed", "de": "Nicht anbieter-gestützt"},
    "tailor.profile.transparent": {"en": "Transparent CV Copilot ATS simulation.", "de": "Transparente CV-Copilot-ATS-Simulation."},
    "tailor.profile.details_aria": {"en": "Selected score-system details", "de": "Details zum gewählten Bewertungssystem"},
    "tailor.profile.transparent_disclaimer": {
        "en": "Transparent CV Copilot simulation; not certified by or backed by an ATS vendor.",
        "de": "Transparente CV-Copilot-Simulation; nicht durch einen ATS-Anbieter zertifiziert oder gestützt.",
    },
    "tailor.profile.systems": {"en": "Score systems", "de": "Bewertungssysteme"},
    "tailor.profile.simulations": {"en": "Simulations, not vendor-backed", "de": "Simulationen, nicht anbieter-gestützt"},
    "tailor.profile.source.ai": {"en": "AI rubric", "de": "KI-Rubrik"},
    "tailor.profile.source.must_have": {"en": "Must-have", "de": "Muss-Anforderungen"},
    "tailor.profile.source.keyword_fallback": {"en": "Keyword fallback", "de": "Keyword-Fallback"},
    "tailor.profile.source.keyword": {"en": "Keyword", "de": "Keyword"},
    "tailor.review.changed": {
        "en": "Review token verified against the current source CV. +{added} / -{removed}.{suffix}",
        "de": "Prüftoken gegen den aktuellen Quell-Lebenslauf verifiziert. +{added} / -{removed}.{suffix}",
    },
    "tailor.review.truncated": {"en": " Diff is truncated for display.", "de": " Diff wird gekürzt angezeigt."},
    "tailor.review.no_changes": {
        "en": "No text changes were proposed. Applying still creates a separate copy.",
        "de": "Es wurden keine Textänderungen vorgeschlagen. Anwenden erstellt trotzdem eine separate Kopie.",
    },
    "tailor.review.no_diff": {"en": "No textual diff.", "de": "Kein Text-Diff."},
    "tailor.lineage.review": {"en": "Review", "de": "Prüfung"},
    "tailor.lineage.source_cv": {"en": "Source CV", "de": "Quell-Lebenslauf"},
    "tailor.lineage.source": {"en": "source", "de": "Quelle"},
    "tailor.lineage.tailored": {"en": "tailored", "de": "angepasst"},
    "tailor.lineage.jd": {"en": "JD", "de": "JD"},  # lineage-chip abbreviation (plan 107, F8)
    "tailor.lineage.job": {"en": "Job", "de": "Job"},
    "tailor.lineage.years": {"en": "Years", "de": "Jahre"},
    "tailor.pdf.building": {"en": "Building...", "de": "Erstelle..."},
    "tailor.pdf.failed": {"en": "Couldn't render the PDF.", "de": "PDF konnte nicht gerendert werden."},
    "tailor.selected_job": {"en": "Selected job", "de": "Ausgewählter Job"},
    "ats.no_linked_job": {"en": "No linked job", "de": "Kein verknüpfter Job"},
    "ats.not_scored": {"en": "Not scored", "de": "Nicht bewertet"},
    "ats.coverage_unavailable": {"en": "Must-have coverage unavailable", "de": "Muss-Abdeckung nicht verfügbar"},
    "ats.no_linked_job_hint": {
        "en": "No linked job for this CV. The scorer needs a job description.",
        "de": "Kein Job mit diesem Lebenslauf verknüpft. Die Bewertung braucht eine Stellenbeschreibung.",
    },
    "ats.lineage_not_ready": {"en": "Packet lineage is not apply-ready.", "de": "Paket-Herkunft ist nicht anwendungsbereit."},
    "ats.onepager_fit": {"en": "CV Copilot ATS fit", "de": "CV-Copilot-ATS-Passung"},
    "ats.source": {"en": "Source", "de": "Quelle"},
    "ats.source_rubric": {"en": "AI rubric with deterministic signal", "de": "KI-Rubrik mit deterministischem Signal"},
    "ats.source_deterministic": {"en": "Deterministic keyword scorer", "de": "Deterministische Keyword-Bewertung"},
    "ats.coverage": {"en": "Coverage", "de": "Abdeckung"},
    "ats.status": {"en": "Status", "de": "Status"},
    "ats.unsaved_edits": {"en": "Based on saved CV; unsaved edits exist", "de": "Basierend auf gespeichertem Lebenslauf; ungespeicherte Änderungen vorhanden"},
    "ats.ready": {"en": "Ready", "de": "Bereit"},
    "ats.gaps": {"en": "Gaps", "de": "Lücken"},
    "ats.no_gaps": {"en": "No gaps returned", "de": "Keine Lücken zurückgegeben"},
    "ats.matched": {"en": "Matched", "de": "Treffer"},
    "ats.no_matched": {"en": "No matched terms returned", "de": "Keine Treffer zurückgegeben"},
    "tailor.apply.copy": {"en": "Apply as new copy", "de": "Als neue Kopie anwenden"},
    "tailor.apply.busy": {"en": "Applying…", "de": "Wende an…"},
    "catalog.trigger": {"en": "Document catalog", "de": "Dokumentkatalog"},
    "catalog.trigger_aria": {
        "en": "Open document catalog: {name}",
        "de": "Dokumentkatalog öffnen: {name}",
    },
    "catalog.kicker": {"en": "Documents", "de": "Dokumente"},
    "catalog.title": {"en": "Document catalog", "de": "Dokumentkatalog"},
    "catalog.close": {"en": "Close document catalog", "de": "Dokumentkatalog schließen"},
    "catalog.filters": {"en": "Document filters", "de": "Dokumentfilter"},
    "catalog.search": {"en": "Search documents", "de": "Dokumente suchen"},
    "catalog.footer": {
        "en": "CVs open in the editor. Cover letters open in the cover-letter workspace.",
        "de": "Lebensläufe öffnen im Editor. Anschreiben öffnen im Anschreiben-Arbeitsbereich.",
    },
    "catalog.new_cv": {"en": "New CV", "de": "Neuer Lebenslauf"},
    "catalog.updated": {"en": "Updated {when}", "de": "Aktualisiert {when}"},
    "catalog.no_timestamp": {"en": "No update timestamp", "de": "Kein Aktualisierungszeitpunkt"},
    "catalog.just_now": {"en": "just now", "de": "gerade eben"},
    "catalog.standalone": {"en": "Standalone document", "de": "Eigenständiges Dokument"},
    "catalog.stale_job_artifact": {
        "en": "Stale job artifact: {job}",
        "de": "Veraltetes Job-Artefakt: {job}",
    },
    "catalog.archived_job_artifact": {
        "en": "Archived job artifact: {job}",
        "de": "Archiviertes Job-Artefakt: {job}",
    },
    "catalog.linked_job": {"en": "Linked to job: {job}", "de": "Mit Job verknüpft: {job}"},
    "catalog.job_artifact": {"en": "Job artifact", "de": "Job-Artefakt"},
    "catalog.section.job_artifacts": {"en": "Job artifacts", "de": "Job-Artefakte"},
    "catalog.open_cv": {"en": "Open CV", "de": "Lebenslauf öffnen"},
    "catalog.current_cv": {"en": "Current CV", "de": "Aktueller Lebenslauf"},
    "catalog.open_letter": {"en": "Open letter", "de": "Anschreiben öffnen"},
    "catalog.pdf": {"en": "PDF", "de": "PDF"},
    "catalog.rename": {"en": "Rename", "de": "Umbenennen"},
    "catalog.rename_label": {"en": "Document name", "de": "Dokumentname"},
    "catalog.duplicate": {"en": "Duplicate", "de": "Duplizieren"},
    "catalog.yaml": {"en": "YAML", "de": "YAML"},
    "catalog.delete": {"en": "Delete", "de": "Löschen"},
    "catalog.job": {"en": "Job", "de": "Job"},
    "catalog.empty_search": {"en": "No documents match this search.", "de": "Keine Dokumente passen zu dieser Suche."},
    "catalog.empty_filter": {"en": "No documents match this filter.", "de": "Keine Dokumente passen zu diesem Filter."},
    "catalog.rename_doc": {"en": "Rename {type}", "de": "{type} umbenennen"},
    "common.cancel": {"en": "Cancel", "de": "Abbrechen"},
    "catalog.delete_cv": {"en": "Delete this CV?", "de": "Diesen Lebenslauf löschen?"},
    "catalog.delete_cover": {"en": "Delete this cover letter?", "de": "Dieses Anschreiben löschen?"},
    "catalog.rename_active_cv": {"en": "Rename active CV", "de": "Aktiven Lebenslauf umbenennen"},
    "sidebar.open_catalog": {"en": "Open document catalog", "de": "Dokumentkatalog öffnen"},
    "sidebar.hidden_count": {"en": "{count} more", "de": "{count} weitere"},
}


def normalize_lang(lang: str | None) -> str:
    """Map an arbitrary ``lang`` to a supported one (default EN)."""
    code = (lang or "").strip().lower()[:2]
    return code if code in SUPPORTED else DEFAULT_LANG


def resolve(lang: str | None) -> dict[str, str]:
    """Return a flat ``{key: text}`` dict for ``lang``, EN-filling any gaps.

    A key without a translation for the requested language falls back to its
    English string so the UI never renders an empty label.
    """
    code = normalize_lang(lang)
    out: dict[str, str] = {}
    for key, by_lang in STRINGS.items():
        out[key] = by_lang.get(code) or by_lang.get(DEFAULT_LANG) or key
    return out
