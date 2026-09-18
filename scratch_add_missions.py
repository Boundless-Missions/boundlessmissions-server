# STALE — DO NOT RUN. This is a one-off scratch script from when a template in
# data/mission_templates.py was a 7-tuple. Templates are 8-tuples now (the eighth
# field is `requires`, the install tags that gate the mission — see
# docs/design/bot/weekly-missions.md), so running this would append entries the
# pool cannot read and break every weekly board. Kept only as a record of the edit
# it made.

import os
import ast

with open('data/mission_templates.py', 'r') as f:
    content = f.read()

# We'll just define the new missions and append them to the file before the final bracket, 
# or just redefine the list by parsing and extending, then writing back.

new_missions = [
    # ── Mod: Outer Planets Mod (OPM) ──────────────────────────────────────────
    ("Perform a flyby of Sarnus", "Sarnus yakın geçişi yapın", 5, "exploration"),
    ("Land on Tekto and return", "Tekto'ya iniş yapın ve dönün", 7, "return"),
    ("Deploy a relay network around Urlum", "Urlum etrafında röle ağı kurun", 6, "construction"),
    ("Perform a crewed landing on Slate", "Slate'e mürettebatlı iniş yapın", 8, "landing"),
    ("Orbit Neidon and return to Kerbin", "Neidon yörüngesine girin ve Kerbin'e dönün", 7, "return"),
    ("Land a rover on Plock", "Plock'a gezici indirin", 7, "landing"),
    ("Perform a grand tour of all OPM planets", "Tüm OPM gezegenlerini kapsayan büyük bir tur yapın", 10, "extreme"),
    ("Build a refueling station in Sarnus orbit", "Sarnus yörüngesinde yakıt istasyonu kurun", 7, "construction"),
    ("Send a probe into Jool's lower atmosphere and survive", "Jool'un alt atmosferine sonda gönderin ve hayatta kalın", 8, "extreme"),
    ("Return a surface sample from Hale", "Hale'den yüzey örneği getirip dönün", 6, "return"),
    
    # ── Mod: Kcalbeloh System ─────────────────────────────────────────────────
    ("Travel through the Kcalbeloh wormhole", "Kcalbeloh solucan deliğinden geçin", 7, "exploration"),
    ("Orbit the Kcalbeloh black hole safely", "Kcalbeloh kara deliği etrafında güvenli yörüngeye girin", 8, "exploration"),
    ("Land on Rouqea and establish a base", "Rouqea'ya iniş yapıp üs kurun", 9, "construction"),
    ("Perform a flyby of the binary stars", "İkili yıldızların yakın geçişini yapın", 8, "exploration"),
    ("Return a sample from Ater", "Ater'den örnek getirip dönün", 9, "return"),
    ("Deploy a science station in Kcalbeloh orbit", "Kcalbeloh yörüngesine bilim istasyonu kurun", 8, "construction"),
    ("Land on Sunorc", "Sunorc'a iniş yapın", 8, "landing"),
    ("Send a crewed mission to the Kcalbeloh system and return to Kerbin", "Kcalbeloh sistemine mürettebatlı görev gönderip Kerbin'e dönün", 10, "extreme"),
    ("Establish a permanent colony on a Kcalbeloh planet", "Bir Kcalbeloh gezegeninde kalıcı koloni kurun", 10, "extreme"),
    
    # ── Mod: Far Future Technologies ──────────────────────────────────────────
    ("Build an Antimatter factory in orbit", "Yörüngede Antimadde fabrikası kurun", 8, "construction"),
    ("Reach another star using a Fusion Drive", "Füzyon Motoru kullanarak başka bir yıldıza ulaşın", 9, "extreme"),
    ("Construct a massive interstellar generation ship", "Devasa bir yıldızlararası nesil gemisi inşa edin", 9, "construction"),
    ("Harvest antimatter from Jool's magnetosphere", "Jool'un manyetosferinden antimadde toplayın", 8, "exploration"),
    ("Perform a high-speed interstellar flyby", "Yüksek hızlı yıldızlararası yakın geçiş yapın", 9, "exploration"),
    ("Deploy a laser-pumped propulsion network", "Lazer pompalı itki ağı kurun", 8, "construction"),
    
    # ── Mod: Near Future Technologies ─────────────────────────────────────────
    ("Build a nuclear-powered tug for orbital construction", "Yörünge inşası için nükleer güçle çalışan römorkör yapın", 5, "construction"),
    ("Deploy a large solar array station in low Kerbol orbit", "Alçak Kerbol yörüngesine büyük güneş paneli istasyonu kurun", 6, "construction"),
    ("Perform an ion-drive only mission to Eeloo", "Sadece iyon motoru ile Eeloo'ya görev yapın", 6, "exploration"),
    ("Construct a base using Near Future Construction parts", "Near Future Construction parçalarıyla üs kurun", 4, "construction"),
    ("Use Argon gas propulsion for a Duna transfer", "Duna transferi için Argon gazı itkisi kullanın", 5, "exploration"),
    
    # ── Mod: Kerbalism / USI Life Support ─────────────────────────────────────
    ("Keep a Kerbal alive in space for 10 years continuously", "Bir Kerbal'ı uzayda 10 yıl boyunca kesintisiz hayatta tutun", 7, "extreme"),
    ("Build a fully self-sufficient greenhouse base on Duna", "Duna'da tamamen kendi kendine yeten sera üssü kurun", 8, "construction"),
    ("Survive a severe solar storm in interplanetary space", "Gezegenlerarası uzayda şiddetli bir güneş fırtınasından sağ kurtulun", 6, "extreme"),
    ("Establish a USI MKS logistics hub in Mun orbit", "Mun yörüngesinde USI MKS lojistik merkezi kurun", 7, "construction"),
    ("Set up a planetary resource extraction chain", "Gezegensel kaynak çıkarma zinciri kurun", 7, "construction"),
    
    # ── Real Solar System (RSS) / RO (If playing RSS, these fit the theme) ────
    ("Reach Earth Orbit in RSS", "RSS'te Dünya Yörüngesine ulaşın", 5, "orbital"),
    ("Perform a Moon landing in RSS", "RSS'te Ay inişi yapın", 7, "landing"),
    ("Land a rover on Mars in RSS", "RSS'te Mars'a gezici indirin", 8, "landing"),
    ("Perform a crewed Apollo-style mission in RSS", "RSS'te Apollo tarzı insanlı görev yapın", 8, "return"),
    ("Send a probe to Jupiter in RSS", "RSS'te Jüpiter'e sonda gönderin", 7, "exploration"),
    ("Land on Venus in RSS", "RSS'te Venüs'e iniş yapın", 8, "landing"),
    ("Send a Voyager-style probe out of the solar system in RSS", "RSS'te güneş sistemi dışına Voyager tarzı sonda gönderin", 9, "extreme"),
    ("Perform a crewed Mars landing and return in RSS", "RSS'te insanlı Mars inişi ve dönüşü yapın", 10, "extreme"),
    ("Build the ISS in Earth orbit in RSS", "RSS'te Dünya yörüngesinde ISS'i inşa edin", 8, "construction"),
    
    # ── More Base Game & Creative Scenarios ───────────────────────────────────
    ("Rescue a Kerbal from Eve's surface", "Eve yüzeyinden bir Kerbal'ı kurtarın", 10, "extreme"),
    ("Capture a Class E asteroid and land it on Kerbin safely", "E Sınıfı bir asteroidi yakalayıp Kerbin'e güvenle indirin", 9, "extreme"),
    ("Build a submarine and explore Laythe's oceans", "Bir denizaltı yapıp Laythe okyanuslarını keşfedin", 7, "exploration"),
    ("Build a helicopter and fly it on Duna", "Bir helikopter yapıp Duna'da uçurun", 7, "exploration"),
    ("Perform a precision landing on the VAB helipad from orbit", "Yörüngeden VAB helikopter pistine hassas iniş yapın", 6, "landing"),
    ("Construct a Mun arch research base", "Mun kemeri araştırma üssü kurun", 5, "construction"),
    ("Fly an SSTO to Minmus, refuel, and go to Duna", "Minmus'a SSTO uçurun, yakıt alıp Duna'ya gidin", 8, "extreme"),
    ("Use a gravity assist from Eve to reach Jool", "Jool'a ulaşmak için Eve'den kütleçekim sapması (gravity assist) kullanın", 6, "exploration"),
    ("Perform a Kerbol (Sun) dive under 1,000,000 km", "Kerbol'a (Güneş) 1,000,000 km altına dalış yapın", 7, "extreme"),
    ("Land on the Mohole on Moho", "Moho'daki Mohole'a (kutuplardaki dev çukur) iniş yapın", 8, "landing"),
    ("Build a rover that can drive upside down", "Ters dönebilen bir gezici araç yapın", 3, "exploration"),
    ("Recover a splashed down capsule from Kerbin's ocean using a boat", "Kerbin okyanusuna düşmüş bir kapsülü tekne ile kurtarın", 4, "exploration"),
    ("Deploy a constellation of 10 satellites in a single launch", "Tek fırlatmada 10 uyduluk bir ağ kurun", 5, "orbital"),
    ("Build a functional space tether/elevator concept", "Çalışan bir uzay asansörü/bağlantısı konsepti inşa edin", 9, "extreme"),
    ("Create an orbital ring around Minmus", "Minmus etrafında yörüngesel bir halka inşa edin", 8, "construction"),
    ("Perform a lithobraking (crash-landing) survival mission", "Litofrenleme (çarpma ile yavaşlama) yaparak hayatta kalın", 5, "landing"),
    ("Fly through the R&D bridge with a jet", "Bir jet ile Ar-Ge köprüsünün altından uçun", 4, "exploration"),
    ("Build a mech/walker and walk 1km on Kerbin", "Bir mecha/yürüyen robot yapıp Kerbin'de 1km yürütün", 6, "exploration"),
]

# We will just format these as python tuples and insert them into the file
formatted_missions = ",\n".join(
    f'    ({repr(m[0])}, {repr(m[1])}, {m[2]}, {repr(m[3])})'
    for m in new_missions
)

# Find the closing bracket of the TEMPLATES list
with open('data/mission_templates.py', 'w') as f:
    if "]" in content:
        # Just insert right before the last closing bracket
        last_bracket_idx = content.rfind("]")
        new_content = content[:last_bracket_idx] + ",\n" + formatted_missions + "\n" + content[last_bracket_idx:]
        f.write(new_content)
        print("Successfully added missions")
    else:
        print("Could not find list closing bracket.")
