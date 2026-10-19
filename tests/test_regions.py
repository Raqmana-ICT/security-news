"""Region tagging: regions.json keywords, English whole words, Arabic prefixes/suffixes, Libya before MENA."""
import json
from pathlib import Path

import pytest

from fetch_news import RegionTagger

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def tag():
    return RegionTagger(json.loads((ROOT / "regions.json").read_text(encoding="utf-8"))).tag


@pytest.mark.parametrize("text, region", [
    # the required cases
    ("Hurricane season threatens refineries on the Gulf of Mexico", "Global"),
    ("Gulf banks hit by a wave of phishing", "MENA"),
    ("اختراق بيانات شركة الاتصالات الليبية", "Libya"),
    ("هجمات تستهدف مصر وليبيا", "Libya"),
    ("Romania arrests ransomware affiliate", "Global"),
    # extra English
    ("Omani telecom confirms breach", "MENA"),
    ("Gulf Coast hospital ransomware", "Global"),
    ("Gulf Stream and the Gulf of America", "Global"),
    ("Persian Gulf shipping firms targeted", "MENA"),
    ("Libya and Egypt targeted by the same APT", "Libya"),
    ("Libyan banks adopt new controls", "Libya"),
    ("Emiratis warned about SMS scams", "MENA"),
    ("Saudis targeted in fake job offers", "MENA"),
    ("Woman charged over SIM swapping", "Global"),
    ("Abu   Dhabi data centre opens", "MENA"),
    ("BENGHAZI port operator hit by ransomware", "Libya"),
    ("Microsoft patches 60 flaws", "Global"),
    # extra Arabic
    ("ارتفاع الهجمات على المصارف في المنطقة", "Global"),          # مصرف is not مصر
    ("قطرة في بحر الهجمات", "Global"),                          # قطرة is not قطر
    ("يمنع التطبيق الجديد التصيد", "Global"),                    # يمنع is not يمن
    ("تحذير للسعودية من حملة تصيد", "MENA"),                     # لل + سعودية
    ("البنوك السعودية تعزز الحماية", "MENA"),
    ("الهيئة الوطنية في عُمان", "MENA"),                          # diacritics removed
    ("اختراق مؤسسات في إيران", "MENA"),
    ("اختراق مؤسسات في ايران", "MENA"),                          # alef without hamza
    ("هجوم على شركات الشرق الأوسط", "MENA"),
    ("إعصار في خليج المكسيك", "Global"),
    ("فريق الاستجابة الليبي يصدر تحذيرًا", "Libya"),
    ("المواطنون الليبيون مستهدفون", "Libya"),
    ("بنغازي تستضيف مؤتمرًا للأمن السيبراني", "Libya"),
    ("سورة قصيرة", "Global"),                                   # سورة is not سوري
    ("لاجئون سوريون مستهدفون ببرمجيات تجسس", "MENA"),
    ("", "Global"),
])
def test_region(tag, text, region):
    assert tag(text) == region


def test_jordan_inside_url_like_string_does_not_break(tag):
    # Must not raise, and must still return a valid region
    for text in ("see https://example.com/reports/jordan-telecom?id=1",
                 "https://jordan.example/~x/(unbalanced[regex*chars+",
                 "Jordan"):
        assert tag(text) in {"Libya", "MENA", "Global"}
    assert tag("Jordan") == "MENA"


def test_libya_wins_over_mena(tag):
    assert tag("Tripoli and Cairo agree on cyber cooperation, says Egypt") == "Libya"
    assert tag("مصر وليبيا توقعان اتفاقية") == "Libya"
