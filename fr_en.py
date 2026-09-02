"""French -> English for VPauto lot data.

Labels, enum values and colours are deterministic dictionaries (a fixed vocabulary,
so a lookup beats a translation call). Free-prose observations and the equipment
list are translated once via the Gemini pool and cached in data/translations.json
and data/equipment_en.json - see translate_observations.py.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

# ---------------------------------------------------------------- labels
LABELS = {
    "Genre": "Vehicle class",
    "Couleur": "Colour",
    "TVA": "VAT recoverable",
    "Carrosserie": "Body type",
    "Carnet d'Entretien": "Service book",
    "Suivi d'Entretien": "Service history",
    "CO2 (g/km)": "CO₂ (g/km)",
    "Mise en circulation": "First registered",
    "Norme Euro": "Euro emissions standard",
    "Kilométrage": "Mileage",
    "Energie": "Fuel",
    "CV (fiscaux)": "Fiscal horsepower (FR tax rating)",
    "Puissance (ch)": "Power (hp)",
    "Cylindrée": "Displacement (cc)",
    "Localisation": "Location in France",
    "Type de boite": "Gearbox",
    "Type de boîte": "Gearbox",
    "Nombre de portes": "Doors",
    "Nbre de vitesses": "Gears",
    "Largeur": "Width (cm)",
    "Longueur (cm)": "Length (cm)",
    "Hauteur (cm)": "Height (cm)",
    "Nombre de places carte grise": "Seats (per registration doc)",
    "Crit'Air": "Crit'Air sticker (FR city-emissions class)",
    "Crit’Air": "Crit'Air sticker (FR city-emissions class)",
}

# ---------------------------------------------------------------- values
VALUES = {
    "Oui": "Yes", "Non": "No", "NC": "not stated",
    "Pas à jour": "Not up to date",
    # body types
    "VP": "Passenger car",
    "BREAK": "Estate / wagon",
    "CI": "Saloon (conduite intérieure)",
    "FOURGON": "Panel van",
    "Deriv VP": "Car-derived van",
    "PLATEAU": "Flatbed truck",
    "DEPANNAG": "Breakdown / tow truck",
    # gearbox
    "Automatique": "Automatic", "Manuelle": "Manual",
    # fuel
    "Diesel": "Diesel",
    "Essence": "Petrol",
    "Essence Hybride": "Petrol hybrid",
    "Diesel hybride": "Diesel hybrid",
    "Electricite": "Electric",
    "FH": "Full hybrid",
    "Essence / GPL": "Petrol / LPG",
    "Essence / GNV": "Petrol / CNG",
    "Electricite / Gazole": "Diesel plug-in hybrid",
}

# colour words, longest first so "bleu nuit" beats "bleu"
COLOURS = [
    ("bleu nuit", "midnight blue"), ("gris fonce", "dark grey"), ("gris clair", "light grey"),
    ("gris medium", "medium grey"), ("bleu fonce", "dark blue"), ("bleu clair", "light blue"),
    ("vert fonce", "dark green"), ("rouge fonce", "dark red"),
    ("blanc", "white"), ("noir", "black"), ("gris", "grey"), ("bleu", "blue"),
    ("rouge", "red"), ("vert", "green"), ("jaune", "yellow"), ("marron", "brown"),
    ("beige", "beige"), ("orange", "orange"), ("argent", "silver"), ("violet", "purple"),
    ("metallise", "metallic"), ("metal", "metallic"), ("nacre", "pearl"),
    ("fonce", "dark"), ("clair", "light"), ("medium", "medium"), ("nuit", "night"),
]

_ACCENTS = str.maketrans("àâäéèêëîïôöùûüçÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ", "aaaeeeeiioouuucAAAEEEEIIOOUUUC")


def _flat(s: str) -> str:
    return s.translate(_ACCENTS).lower()


def colour_en(fr: str | None) -> str | None:
    """'Bleu nuit métal' -> 'midnight blue metallic'. Unknown words are kept."""
    if not fr:
        return fr
    flat = _flat(fr)
    out, used = [], []
    for token, eng in COLOURS:
        if token in flat:
            if any(token in u for u in used):
                continue
            used.append(token)
            out.append(eng)
    return " ".join(out) if out else fr


def label_en(fr: str) -> str:
    return LABELS.get(fr.strip(), fr)


def value_en(label: str, fr: str) -> str:
    fr = (fr or "").strip()
    if _flat(label).startswith("couleur"):
        return colour_en(fr) or fr
    if fr in VALUES:
        return VALUES[fr]
    # "102 (Norme Inconnu)" -> "102 (standard unknown)"
    out = re.sub(r"Norme Inconnu", "standard unknown", fr)
    return out


# ---------------------------------------------------------------- cached
def _load(name: str) -> dict:
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            return {}
    return {}


OBS_EN = _load("translations.json")
EQUIP_EN = _load("equipment_en.json")


def obs_en(fr: str | None) -> str | None:
    if not fr:
        return fr
    import html as ihtml
    return OBS_EN.get(ihtml.unescape(fr).strip(), fr)


def equip_en(fr: str) -> str:
    return EQUIP_EN.get(fr.strip(), fr)


# ---------------------------------------------------------------- warnings
# Things that change whether a car can be imported at all, or is worth bidding on.
# Keyed on the French source so it works before translation.
WARNINGS = [
    (r"export impossible", "EXPORT NOT PERMITTED", "block"),
    (r"fiche d.{0,3}identification", "No registration document (identification sheet only)", "block"),
    (r"\bgag[ée]\b", "Vehicle under a financial lien", "block"),
    (r"carte grise absente|sans carte grise", "Registration document missing", "block"),
    (r"non roulant|ne d[ée]marre pas", "NON-RUNNER", "severe"),
    (r"moteur hs|moteur cass", "Engine failed", "severe"),
    (r"embrayage hs|bo[iî]te.{0,12}hs", "Clutch / gearbox failed", "severe"),
    (r"vendu en l.{0,3}[ée]tat", "Sold as-is, no recourse", "severe"),
    (r"v[ée]hicule accident|accident[ée]", "Accident-repaired", "severe"),
    (r"gr[êe]l[ée]", "Hail damage", "warn"),
    (r"d[ée]faut critique|panne critique", "Critical fault flagged", "severe"),
    (r"probl[èe]me batterie|d[ée]faut batterie", "Battery fault", "warn"),
    (r"perte de puissance", "Power loss", "warn"),
    (r"distribution [àa] (faire|remplacer)", "Timing belt due", "warn"),
    (r"courroie.{0,20}remplacer", "Belt replacement due", "warn"),
    (r"fap\b|d[ée]faut fap", "DPF advisory", "warn"),
    (r"[ée]cole de conduite|auto-?[ée]cole", "Ex-driving-school car", "warn"),
    (r"import\b", "Imported into France (not a domestic car)", "warn"),
    (r"r[ée]vision [àa] faire|entretien [àa] faire", "Service due", "info"),
]


def warnings_for(observations: str | None) -> list[tuple[str, str]]:
    """[(text, severity)] - severity is block | severe | warn | info."""
    if not observations:
        return []
    flat = _flat(observations)
    seen, out = set(), []
    for pattern, text, sev in WARNINGS:
        if re.search(pattern, flat) and text not in seen:
            seen.add(text)
            out.append((text, sev))
    return out


# --------------------------------------------------------------- Alcopa terms
# Alcopa labels its damage zones and gearboxes in French only, with no /en/
# page to borrow from. Translate word by word: the 49 zone labels are all
# built from the same short vocabulary, so this also covers zones we have not
# seen yet instead of needing a 49-row table that goes stale.
_ALC_WORDS = {
    "aile": "wing", "ailes": "wings", "antibrouillard": "fog light",
    "bas": "lower", "caisse": "sill", "calandre": "grille", "capot": "bonnet",
    "coffre": "boot", "feu": "light", "feux": "lights", "jante": "wheel rim",
    "lunette": "rear window", "panneau": "panel", "pare": "", "brise": "windscreen",
    "choc": "bumper", "pavillon": "roof", "pneu": "tyre", "poignee": "handle",
    "porte": "door", "battante": "hinged", "laterale": "side",
    "retroviseur": "mirror", "vitre": "window", "custode": "quarter glass",
    "hayon": "tailgate", "malle": "boot lid", "marchepied": "step",
    "seuil": "sill", "toit": "roof", "avant": "front", "arriere": "rear",
    "droit": "right", "droite": "right", "gauche": "left",
    "de": "", "du": "", "la": "", "le": "", "les": "", "l": "",
}
_ALC_PHRASES = {
    "pare brise": "Windscreen", "pare-brise": "Windscreen",
    "bas de caisse droit": "Right sill", "bas de caisse gauche": "Left sill",
    "poignee porte battante": "Hinged door handle",
    "debosselage": "Dent removal", "peinture": "Painting",
    "remplacement": "Replacement", "tolerie + peinture": "Bodywork + painting",
    "tolerie": "Bodywork",
    "automatique": "Automatic", "manuelle": "Manual",
    "sequentielle": "Sequential",
}


def alcopa_en(fr: str | None) -> str | None:
    """French Alcopa label -> English. Unknown words are kept as-is."""
    if not fr:
        return fr
    # values arrive with literal backslash-u escapes when they came from JSON
    if chr(92) + "u" in fr:
        try:
            fr = fr.encode().decode("unicode_escape")
        except Exception:
            pass
    flat = _flat(fr).strip()
    if flat in _ALC_PHRASES:
        return _ALC_PHRASES[flat]
    out = []
    for w in re.split(r"[\s-]+", flat):
        if not w:
            continue
        t = _ALC_WORDS.get(w, w)
        if t:
            out.append(t)
    if not out:
        return fr
    s = " ".join(out)
    # "wing rear right" reads better as "Right rear wing"
    mods = [w for w in ("right", "left") if w in out]
    pos = [w for w in ("front", "rear") if w in out]
    head = [w for w in out if w not in ("right", "left", "front", "rear")]
    if head and (mods or pos):
        s = " ".join(mods + pos + head)
    return s[:1].upper() + s[1:]
