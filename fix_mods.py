# STALE — DO NOT RUN. This is a one-off scratch script from when a template in
# data/mission_templates.py was a 7-tuple. Templates are 8-tuples now (the eighth
# field is `requires`, the install tags that gate the mission — see
# docs/design/bot/weekly-missions.md), so running this would append entries the
# pool cannot read and break every weekly board. Kept only as a record of the edit
# it made.

import re

with open('data/mission_templates.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
current_mod = ""

for line in lines:
    if "# ── Mod: Outer Planets Mod (OPM)" in line:
        current_mod = "[OPM]"
    elif "# ── Mod: Kcalbeloh System" in line:
        current_mod = "[Kcalbeloh]"
    elif "# ── Mod: Far Future Technologies" in line:
        current_mod = "[FFT]"
    elif "# ── Mod: Near Future Technologies" in line:
        current_mod = "[NFT]"
    elif "# ── Mod: Kerbalism / USI Life Support" in line:
        current_mod = "[LifeSupport]"
    elif "# ── Real Solar System (RSS) / RO" in line:
        current_mod = "[RSS]"
    elif "# ── More Base Game & Creative Scenarios" in line:
        current_mod = ""
        
    if current_mod and line.strip().startswith("('"):
        # line is like: ('Perform a flyby...', 'Sarnus yakın...', 5, 'exploration'),
        # we want to insert current_mod after the first quote, and after the third quote
        # Let's use eval or just simple string replacement
        parts = line.split("', '")
        if len(parts) >= 2:
            first_part = parts[0].replace("('", f"('{current_mod} ")
            second_part = parts[1].replace("'", f"'{current_mod} ", 1)
            # wait, it's separated by ', ' so the second string starts right after ', '
            # Actually, using literal_eval is safer to modify tuples
            try:
                import ast
                tup_str = line.strip().rstrip(",")
                tup = ast.literal_eval(tup_str)
                new_tup = (f"{current_mod} {tup[0]}", f"{current_mod} {tup[1]}", tup[2], tup[3])
                # format back
                line = f"    ({repr(new_tup[0])}, {repr(new_tup[1])}, {new_tup[2]}, {repr(new_tup[3])}),\n"
            except Exception as e:
                print("Error parsing line:", line, e)
                pass
                
    new_lines.append(line)

with open('data/mission_templates.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print("Done")
