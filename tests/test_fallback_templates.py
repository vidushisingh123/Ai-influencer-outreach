"""Fallback templates must stay natural, factual and inside the Phase 2 word limits (no Groq involved)."""
from outreach.messaging import DM_WORDS, EMAIL_WORDS, Sender, build_fallback, count_words, validate_messages

SENDER = Sender(brand="StyleNest", sender_name="Vidushi", collab_type="UGC content creation")
NAMES = ["CathyChen", "Ami", "Devendra Singh", "Ami Glow Beauty Studio Official", "The Very Long Channel Name Of Style"]
THEMES = [[], ["Skincare"], ["Haircare & styling", "Skincare"], ["Fashion & outfits", "Hauls & thrifting", "Nails"],
          ["Accessories & jewelry", "Makeup"]]


def ctx(name, themes):
    return {"creator_name": name, "platform": "YouTube", "niche": "Fashion & Beauty",
            "content_themes": themes, "recent_video_titles": []}


def test_every_fallback_respects_word_limits_and_validation():
    for name in NAMES:
        for themes in THEMES:
            subject, email, dm = build_fallback(ctx(name, themes), SENDER)
            assert EMAIL_WORDS[0] <= count_words(email) <= EMAIL_WORDS[1], (name, themes, count_words(email))
            assert DM_WORDS[0] <= count_words(dm) <= DM_WORDS[1], (name, themes, count_words(dm))
            assert validate_messages(subject, email, dm, []) == []


def test_fallback_uses_only_known_facts_and_reads_naturally():
    _, email, dm = build_fallback(ctx("Ami Glow", ["Haircare & styling", "Skincare"]), SENDER)
    assert "haircare and styling plus skincare" in email and "&" not in email + dm
    for fact in ("Ami Glow", "Vidushi", "StyleNest", "UGC content creation", "YouTube"):
        assert fact in email
    for claim in ("loved", "watched", "followers", "free", "paid", "$"):
        assert claim not in (email + dm).lower()
    assert "Ami Glow" in dm and "StyleNest" in dm
