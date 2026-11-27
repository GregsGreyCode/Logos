"""English or French? For the house speaker (V3.4): Béré speaks French to it, Greg English.

The speech to text (Parakeet v3, gateway/stt.py) writes down whichever of the two is spoken but does
not say which, and Home Assistant's pipeline is set to English whatever is said. So the words
themselves decide: what the person said (the agent is asked to answer in that language) and what
the agent answered (read out by the agent's French voice when it is French).

A count of the commonest short words of each language, with accents as a tie-breaker. Spoken
commands are short ("Quelle heure est-il ?", "Allume la cuisine"), so the lists favour the words
they are made of. English wins a tie: the pipeline's language, and Home Assistant's own words.
Stdlib only.
"""
from __future__ import annotations

import re

FRENCH = frozenset("""
le la les l un une des du de d au aux et ou mais donc car ni que qu qui quoi quel quelle quels quelles
est sont suis es êtes sommes être ai as a avons avez ont avoir fait faire fais va vais vas allons allez
vont peux peut pouvez pouvons veux veut voulez voudrais je j tu il elle on nous vous ils elles me m te t
se s lui leur leurs mon ma mes ton ta tes son sa ses notre nos votre vos ce cet cette ces ça cela ceci
c y en ne n pas plus rien jamais toujours très bien aussi avec sans pour par sur sous dans chez entre
vers comme quand comment combien pourquoi où oui non merci bonjour bonsoir salut au revoir s'il
plaît demain aujourd hier ce soir matin heure heures quelle maintenant encore déjà tout tous toute
toutes allume allumer éteins éteindre mets mettre ajoute ajouter enlève enlever rappelle rappeler
dis dire lumière lumières cuisine salon chambre liste courses temps météo semaine rendez-vous
""".split())

ENGLISH = frozenset("""
the a an and or but so of to in on at for with without from by about into over under is are was were
be been being am do does did done have has had will would can could should shall may might must
i you he she it we they me him her us them my your his its our their this that these those there here
what which who whom whose when where why how not no yes please thanks thank hello hi hey okay ok
all any some every much many more most very just also too again now then today tomorrow tonight
yesterday morning evening time turn off lights light kitchen living room bedroom add list shopping
remind me tell say what's it's i'm don't can't let's weather week calendar
""".split())

_WORD = re.compile(r"[a-zàâäçéèêëîïôöùûüÿœæ]+(?:['’][a-zàâäçéèêëîïôöùûüÿœæ]+)?")
_ACCENT = re.compile(r"[àâçéèêëîïôùûüÿœ]")


def detect(text: str) -> str:
    """"fr" or "en" (English when it cannot be told)."""
    t = str(text or "").casefold()
    fr = en = 0.0
    for w in _WORD.findall(t):
        parts = re.split(r"['’]", w)
        if len(parts) == 2 and parts[0] in ("l", "d", "j", "m", "t", "s", "c", "n", "qu"):
            fr += 1.5                                    # l'heure, d'appeler, qu'est-ce
            w = parts[1]
        elif len(parts) == 2 and parts[1] in ("s", "t", "re", "ll", "ve", "d", "m"):
            en += 1.5                                    # what's, don't, I'll
            w = parts[0]
        fr += w in FRENCH
        en += w in ENGLISH
    fr += 0.5 * min(4, len(_ACCENT.findall(t)))
    return "fr" if fr > en else "en"


def name(lang: str) -> str:
    return {"fr": "French", "en": "English"}.get(lang, "English")
