"""English or French at the house speaker (V3.4, gateway/speech_lang.py)."""
import pytest

from gateway import speech_lang

FRENCH = ["Quelle heure est-il ?", "Allume la lumière de la cuisine.", "Éteins tout", "Oui.", "Merci",
          "D'accord.", "C'est fait.", "Il est dix heures.", "Rappelle-moi d'appeler ma mère à 18h.",
          "Qu'est-ce qu'on a demain ?", "Tu peux allumer le salon ?", "Ajoute du lait à la liste de courses",
          "Est-ce que Greg rentre tard ce soir ?", "Bonjour, comment ça va ?"]
ENGLISH = ["Done.", "OK.", "Yes.", "What time is it?", "Turn the kitchen lights off.", "Is Béré home?",
           "Remind me to call Mum at six.", "Hey Home, what's on tomorrow?", "Add croissants to the list.",
           "Set a timer for ten minutes", "", "Bonjour is French for hello."]


@pytest.mark.parametrize("text", FRENCH)
def test_french(text):
    assert speech_lang.detect(text) == "fr"


@pytest.mark.parametrize("text", ENGLISH)
def test_english_and_anything_unclear(text):
    assert speech_lang.detect(text) == "en"
