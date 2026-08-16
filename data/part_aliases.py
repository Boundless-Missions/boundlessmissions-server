"""Interchangeable KSP part names — GENERATED, do not edit by hand.

Regenerate with `python tools/gen_part_aliases.py` after editing the source of
truth, "KSP Mod Side/GeneKerman/PartAliases.cs". See that file for how the pairs
were derived and why some look-alikes are deliberately absent.
"""

# part name -> every other name that is the same part
ALIASES: dict[str, tuple[str, ...]] = {
    "InflatableAirlock": ("restock-airlock-1",),  # inflatable airlock
    "restock-airlock-1": ("InflatableAirlock",),
    "Decoupler_1p5": ("restock-decoupler-1875-1",),  # 1.875 m decoupler
    "restock-decoupler-1875-1": ("Decoupler_1p5",),
    "Size1p5_Strut_Decoupler": ("restock-decoupler-1875-truss-1",),  # 1.875 m truss decoupler
    "restock-decoupler-1875-truss-1": ("Size1p5_Strut_Decoupler",),
    "Decoupler_4": ("restock-decoupler-5-1",),  # 5 m decoupler
    "restock-decoupler-5-1": ("Decoupler_4",),
    "Separator_1p5": ("restock-separator-1875-1",),  # 1.875 m separator
    "restock-separator-1875-1": ("Separator_1p5",),
    "Separator_4": ("restock-separator-5-1",),  # 5 m separator
    "restock-separator-5-1": ("Separator_4",),
    "LiquidEngineKE-1": ("restock-engine-galleon-1",),  # KE-1 'Mastodon' engine
    "restock-engine-galleon-1": ("LiquidEngineKE-1",),
    "LiquidEngineRV-1": ("restock-engine-panda-1",),  # RV-1 vernier engine
    "restock-engine-panda-1": ("LiquidEngineRV-1",),
    "LiquidEngineRK-7": ("restock-engine-ursa-1",),  # RK-7 'Kodiak' engine
    "restock-engine-ursa-1": ("LiquidEngineRK-7",),
    "Pollux": ("restock-srb-castor-1",),  # THK 'Pollux' booster
    "restock-srb-castor-1": ("Pollux",),
    "EnginePlate5": ("restock-engineplate-125-2",),  # 1.25 m engine plate
    "restock-engineplate-125-2": ("EnginePlate5",),
    "EnginePlate1p5": ("restock-engineplate-1875-1",),  # 1.875 m engine plate
    "restock-engineplate-1875-1": ("EnginePlate1p5",),
    "EnginePlate2": ("restock-engineplate-25-1",),  # 2.5 m engine plate
    "restock-engineplate-25-1": ("EnginePlate2",),
    "EnginePlate3": ("restock-engineplate-375-1",),  # 3.75 m engine plate
    "restock-engineplate-375-1": ("EnginePlate3",),
    "EnginePlate4": ("restock-engineplate-5-1",),  # 5 m engine plate
    "restock-engineplate-5-1": ("EnginePlate4",),
    "Size1p5_Tank_04": ("restock-fueltank-1875-1",),  # 1.875 m LFO tank (long)
    "restock-fueltank-1875-1": ("Size1p5_Tank_04",),
    "Size1p5_Tank_03": ("restock-fueltank-1875-2",),  # 1.875 m LFO tank (medium)
    "restock-fueltank-1875-2": ("Size1p5_Tank_03",),
    "Size1p5_Tank_02": ("restock-fueltank-1875-3",),  # 1.875 m LFO tank (small)
    "restock-fueltank-1875-3": ("Size1p5_Tank_02",),
    "Size1p5_Tank_01": ("restock-fueltank-1875-4",),  # 1.875 m LFO tank (tiny)
    "restock-fueltank-1875-4": ("Size1p5_Tank_01",),
    "Size1p5_Tank_05": ("restock-fueltank-1875-soyuz-1",),  # 1.875 m Soyuz LFO tank
    "restock-fueltank-1875-soyuz-1": ("Size1p5_Tank_05",),
    "Size4_Tank_04": ("restock-fueltank-5-1",),  # 5 m LFO tank (long)
    "restock-fueltank-5-1": ("Size4_Tank_04",),
    "Size4_Tank_03": ("restock-fueltank-5-2",),  # 5 m LFO tank (medium)
    "restock-fueltank-5-2": ("Size4_Tank_03",),
    "Size4_Tank_02": ("restock-fueltank-5-3",),  # 5 m LFO tank (short)
    "restock-fueltank-5-3": ("Size4_Tank_02",),
    "Size4_Tank_01": ("restock-fueltank-5-4",),  # 5 m LFO tank (mini)
    "restock-fueltank-5-4": ("Size4_Tank_01",),
    "Size1p5_Monoprop": ("restock-fuel-tank-rcs-1875-1",),  # 1.875 m monopropellant tank
    "restock-fuel-tank-rcs-1875-1": ("Size1p5_Monoprop",),
    "monopropMiniSphere": ("restock-fuel-tank-rcs-radial-tiny-1",),  # tiny radial monoprop tank
    "restock-fuel-tank-rcs-radial-tiny-1": ("monopropMiniSphere",),
    "Size1p5_Size0_Adapter_01": ("restock-fueltank-adapter-1875-0625-1",),  # 1.875 m → 0.625 m adapter
    "restock-fueltank-adapter-1875-0625-1": ("Size1p5_Size0_Adapter_01",),
    "Size1p5_Size1_Adapter_01": ("restock-fueltank-adapter-1875-125-1",),  # 1.875 m → 1.25 m adapter (long)
    "restock-fueltank-adapter-1875-125-1": ("Size1p5_Size1_Adapter_01",),
    "Size1p5_Size1_Adapter_02": ("restock-fueltank-adapter-1875-125-2",),  # 1.875 m → 1.25 m adapter
    "restock-fueltank-adapter-1875-125-2": ("Size1p5_Size1_Adapter_02",),
    "Size1p5_Size2_Adapter_01": ("restock-fueltank-adapter-25-1875-1",),  # 2.5 m → 1.875 m adapter
    "restock-fueltank-adapter-25-1875-1": ("Size1p5_Size2_Adapter_01",),
    "Size3_Size4_Adapter_01": ("restock-fueltank-adapter-375-5-1",),  # 5 m → 3.75 m adapter
    "restock-fueltank-adapter-375-5-1": ("Size3_Size4_Adapter_01",),
    "Size4_EngineAdapter_01": ("restock-fueltank-saturn-engine-1",),  # 5 m engine mount
    "restock-fueltank-saturn-engine-1": ("Size4_EngineAdapter_01",),
    "Mk2Pod": ("restock-mk2-pod",),  # Mk2 command pod
    "restock-mk2-pod": ("Mk2Pod",),
    "kv1Pod": ("restock-pod-sphere-1",),  # KV-1 'Onion' pod
    "restock-pod-sphere-1": ("kv1Pod",),
    "kv2Pod": ("restock-pod-sphere-2",),  # KV-2 'Pea' pod
    "restock-pod-sphere-2": ("kv2Pod",),
    "kv3Pod": ("restock-pod-sphere-3",),  # KV-3 'Pomegranate' pod
    "restock-pod-sphere-3": ("kv3Pod",),
    "HeatShield1p5": ("restock-heatshield-1875-1",),  # 1.875 m heat shield
    "restock-heatshield-1875-1": ("HeatShield1p5",),
    "Size_1_5_Cone": ("restock-nosecone-1875-2",),  # 1.875 m nose cone
    "restock-nosecone-1875-2": ("Size_1_5_Cone",),
    "rocketNoseConeSize4": ("restock-nosecone-5-1",),  # 5 m nose cone
    "restock-nosecone-5-1": ("rocketNoseConeSize4",),
    "fairingSize1p5": ("restock-fairing-base-1875-1",),  # 1.875 m fairing base
    "restock-fairing-base-1875-1": ("fairingSize1p5",),
    "fairingSize4": ("restock-fairing-base-5-1",),  # 5 m fairing base
    "restock-fairing-base-5-1": ("fairingSize4",),
    "Size1to0ServiceModule": ("restock-service-module-125-625-1",),  # 1.25 m → 0.625 m service module
    "restock-service-module-125-625-1": ("Size1to0ServiceModule",),
    "ServiceModule18": ("restock-service-module-1875-1",),  # 1.875 m service module
    "restock-service-module-1875-1": ("ServiceModule18",),
    "Tube1": ("restock-structural-tube-125-1",),  # 1.25 m structural tube
    "restock-structural-tube-125-1": ("Tube1",),
    "Tube1p5": ("restock-structural-tube-1875-1",),  # 1.875 m structural tube
    "restock-structural-tube-1875-1": ("Tube1p5",),
    "Tube2": ("restock-structural-tube-25-1",),  # 2.5 m structural tube
    "restock-structural-tube-25-1": ("Tube2",),
    "Tube3": ("restock-structural-tube-375-1",),  # 3.75 m structural tube
    "restock-structural-tube-375-1": ("Tube3",),
    "Tube4": ("restock-structural-tube-5-1",),  # 5 m structural tube
    "restock-structural-tube-5-1": ("Tube4",),
    "roverWheelM1-F": ("restock-wheel-4",),  # folding rover wheel
    "restock-wheel-4": ("roverWheelM1-F",),
}


def equivalents(part_name: str) -> tuple[str, ...]:
    """Names that can stand in for `part_name`; empty when it has none."""
    return ALIASES.get(part_name, ())
